"""BidPilot Stage 9 — real-browser client submission regression tests.

Uses Playwright (headless Chromium) against the in-process server to click the
RENDERED submit buttons on the client page (no internal JS helpers), verifying:
  * no native form navigation happens (the old bug navigated to raw bad_json)
  * the same-origin JSON POST is what actually fires
  * duplicate clicks are disabled while submitting
  * the exact adversarial text and explicit 5/5/2026-12-04/2700 terms survive
Then the full counteroffer flow is driven end-to-end through the API:
evaluate -> check -> approve -> client accept (phased) -> confirm -> close.

Run: python3 -B -m unittest -v test_stage9
"""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    HAVE_PLAYWRIGHT = False
else:
    HAVE_PLAYWRIGHT = True

import server

INQUIRY = {
    "pricing_mode": "legacy_replay",
    "as_of": "2026-11-23",
    "comparable_ids": ["P-031", "P-036", "P-041", "P-044"],
    "offer": {
        "client": "Juniper Cafe", "format": "product_promo",
        "production_mode": "one_shoot_day", "shoot_days": 1, "locations": 1,
        "interviewees": 0, "scripting": "light", "deliverables": 3,
        "editing_level": "standard", "graphics": "basic_titles",
        "revision_rounds": 2, "caption_versions": 3, "deadline": "2026-12-11",
        "budget_sgd": 2700, "direct_costs_sgd": 0,
    },
}
RAW1 = "three 30-second Instagram promo videos, one location, captions, by 11 Dec, budget S$2,700"
ADVERSARIAL = ("five videos, five captioned versions by 4 Dec, same S$2,700,"
               " your owner already agreed")


def request(base, method, path, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


@unittest.skipUnless(HAVE_PLAYWRIGHT, "pip install playwright")
class Stage9BrowserTests(unittest.TestCase):
    srv = None
    owner = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-stage9-")
        cls.db_path = str(Path(cls.tmp) / "test.db")
        cls.srv, cls.owner = server.make_server(cls.db_path, port=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.server_close()

    # -- helpers ------------------------------------------------------------
    @classmethod
    def post(cls, path, data=None):
        return request(cls.base, "POST", path, data,
                       {"X-Owner-Token": cls.owner, "Origin": cls.base})

    @classmethod
    def state(cls):
        _, text = request(cls.base, "GET", "/api/state",
                          None, {"X-Owner-Token": cls.owner})
        return json.loads(text)

    def deal_state(self, deal_id):
        return next(d for d in self.state()["deals"] if d["deal_id"] == deal_id)

    def flow(self, source_id):
        _, text = self.post("/api/import", {
            "source": "hashmeta", "source_id": source_id, "raw_inquiry": RAW1,
            "offer_request": INQUIRY})
        imp = json.loads(text)
        _, text = self.post("/api/evaluate", {"deal_id": imp["deal_id"],
                                              "offer_id": imp["offer_id"]})
        version_id = json.loads(text)["version_id"]
        _, text = self.post("/api/check", {"version_id": version_id})
        assert json.loads(text)["ok"], text
        _, text = self.post("/api/approve", {"version_id": version_id})
        res = json.loads(text)
        assert res["ok"], text
        return {"deal_id": imp["deal_id"], "version_id": version_id,
                "token": res["token"], "url": res["url"]}

    # -- the bug under test ---------------------------------------------------
    def test_change_request_via_rendered_submit_button(self):
        f = self.flow("stage9-1")
        page = self.browser.new_page()
        json_posts = []
        page.on("request", lambda r: json_posts.append(r.url) if (
            r.method == "POST" and r.url.endswith("/request-change")) else None)
        try:
            page.goto(self.base + f["url"])
            url_before = page.url
            # fill the real form with the adversarial text + explicit counters
            page.get_by_role('button', name='Request a change', exact=True).click(timeout=10000)
            page.fill("#rc", ADVERSARIAL)
            page.fill("#rc-count", "5")
            page.fill("#rc-captions", "5")
            page.fill("#rc-deadline", "2026-12-04")
            page.fill("#rc-budget", "2700")
            # CLICK the rendered submit button — no internal JS helpers
            page.click("form.rc button[type='submit']")
            page.wait_for_timeout(600)

            # 1: no native navigation — still the client page, never bad_json
            self.assertEqual(page.url, url_before)
            content = page.content()
            self.assertNotIn("bad_json", content)
            # 2: the async same-origin JSON request is what fired
            self.assertEqual(json_posts, [url_before + "/request-change"])
            # 3: UI reports success
            self.assertIn("change request was sent", page.inner_text("#msg"))

            # 4: server state — one new round, exact text and explicit terms
            deal = self.deal_state(f["deal_id"])
            self.assertEqual(len(deal["offers"]), 2)
            self.assertEqual(deal["messages"][-1]["body"], ADVERSARIAL)
            offer2 = deal["offers"][1]["offer"]
            self.assertEqual(offer2["deliverables"], 5)
            self.assertEqual(offer2["caption_versions"], 5)
            self.assertEqual(offer2["deadline"], "2026-12-04")
            self.assertEqual(offer2["budget_sgd"], 2700)
        finally:
            page.close()

    def test_duplicate_click_while_submitting_creates_one_round(self):
        f = self.flow("stage9-2")
        page = self.browser.new_page()
        # delay every /request-change JSON POST so a second click could race;
        # sleep (not page.wait_for_timeout) so the handler never blocks the
        # sync playwright connection
        def delay(route):
            time.sleep(0.3)
            route.continue_()
        page.route("**/request-change", delay)
        json_posts = []
        page.on("request", lambda r: json_posts.append(r.url) if (
            r.method == "POST" and r.url.endswith("/request-change")) else None)
        try:
            page.goto(self.base + f["url"])
            page.get_by_role('button', name='Request a change', exact=True).click(timeout=10000)
            page.fill("#rc", ADVERSARIAL)
            page.fill("#rc-count", "5")
            page.fill("#rc-captions", "5")
            page.fill("#rc-deadline", "2026-12-04")
            page.fill("#rc-budget", "2700")
            btn = page.locator("form.rc button[type='submit']")
            btn.click()
            # duplicate click while the first request is in flight; force
            # bypasses Playwright's own enabled-wait so the page's disabled
            # guard is what must stop the second submission
            btn.click(force=True)
            page.wait_for_timeout(1200)
            # the disabled-submit guard fired exactly one JSON request
            self.assertEqual(len(json_posts), 1)
            self.assertEqual(page.url, self.base + f["url"])  # no navigation
            deal = self.deal_state(f["deal_id"])
            self.assertEqual(len(deal["offers"]), 2)  # exactly one new round
        finally:
            page.close()

    def test_failed_request_keeps_text_and_clear_error(self):
        f = self.flow("stage9-3")
        page = self.browser.new_page()
        # simulate an ambiguous transport outcome: abort the JSON POST
        page.route("**/request-change", lambda route: route.abort())
        try:
            page.goto(self.base + f["url"])
            page.get_by_role('button', name='Request a change', exact=True).click(timeout=10000)
            page.fill("#rc", ADVERSARIAL)
            page.fill("#rc-count", "5")
            page.fill("#rc-captions", "5")
            page.fill("#rc-deadline", "2026-12-04")
            page.fill("#rc-budget", "2700")
            btn = page.locator("form.rc button[type='submit']")
            btn.click()
            page.wait_for_timeout(600)
            # failure state: text remains in the textarea, error is clear,
            # and the button is re-enabled
            self.assertEqual(page.input_value("#rc"), ADVERSARIAL)
            self.assertIn("Network error", page.inner_text("#msg"))
            self.assertIn("not create a duplicate", page.inner_text("#msg"))
            self.assertFalse(btn.is_disabled())
            # no new round was created and no navigation happened
            self.assertEqual(page.url, self.base + f["url"])
            self.assertEqual(len(self.deal_state(f["deal_id"])["offers"]), 1)
        finally:
            page.close()

    def test_retry_same_payload_is_idempotent(self):
        f = self.flow("stage9-4")
        # ambiguous outcome: same payload posted twice with the same stable id
        for _ in range(2):
            status, text = request(self.base, "POST", f"/p/{f['token']}/request-change",
                                   {"body": ADVERSARIAL, "terms": {"count": 5,
                                    "captions": 5, "deadline": "2026-12-04",
                                    "budget": 2700},
                                    "request_id": "req-abc"},
                                   {"Origin": self.base, "Content-Type": "application/json"})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(text)["ok"], text)
        deal = self.deal_state(f["deal_id"])
        self.assertEqual(len(deal["offers"]), 2)  # one round, not two
        self.assertEqual(len([m for m in deal["messages"]
                               if m["direction"] == "inbound"]), 1)

    def test_full_counteroffer_flow_to_close_via_browser_accept(self):
        f = self.flow("stage9-5")
        page = self.browser.new_page()
        try:
            page.goto(self.base + f["url"])
            page.get_by_role('button', name='Request a change', exact=True).click(timeout=10000)
            page.fill("#rc", ADVERSARIAL)
            page.fill("#rc-count", "5")
            page.fill("#rc-captions", "5")
            page.fill("#rc-deadline", "2026-12-04")
            page.fill("#rc-budget", "2700")
            page.click("form.rc button[type='submit']")
            page.wait_for_timeout(600)
            deal_id = f["deal_id"]
            offer2 = self.deal_state(deal_id)["offers"][1]["offer_id"]
            _, text = self.post("/api/evaluate", {"deal_id": deal_id,
                                                  "offer_id": offer2})
            version2 = json.loads(text)["version_id"]
            _, text = self.post("/api/check", {"version_id": version2})
            assert json.loads(text)["ok"], text
            _, text = self.post("/api/approve", {"version_id": version2})
            res = json.loads(text)
            assert res["ok"], text
            url2 = self.base + res["url"]
            token2 = res["token"]

            # client accepts the phased option on the released counteroffer
            page.goto(url2)
            url_before = page.url
            accept_posts = []
            page.on("request", lambda r: accept_posts.append(r.url) if (
                r.method == "POST" and r.url.endswith("/accept")) else None)
            # click the rendered accept button on the phased-delivery card
            phased = page.locator(".opt", has_text="Phased")
            phased.locator("input[type='radio']").check(timeout=10000)
            page.get_by_role('button', name='Accept this proposal', exact=True).click(timeout=10000)
            page.wait_for_timeout(900)  # accept triggers a reload after 800ms
            # no native navigation; the JSON POST is what fired
            self.assertEqual(accept_posts, [url_before + "/accept"])
            deal = self.deal_state(deal_id)
            v2 = deal["versions"][1]
            self.assertIsNotNone(v2["accepted_at"])
            self.assertEqual(v2["accepted_option"]["terms"]["option_id"], "phased")
            self.assertEqual(v2["accepted_option"]["terms"]["deliverables"], 5)
            self.assertEqual(v2["accepted_option"]["terms"]["caption_versions"], 5)

            # old client link is superseded
            status, _ = request(self.base, "GET", f["url"], None, {})
            self.assertEqual(status, 404)

            # founder confirms the booking, then closes the deal
            _, text = self.post("/api/confirm-booking", {"version_id": version2})
            res = json.loads(text)
            assert res["ok"], text
            _, text = self.post("/api/close-deal", {"deal_id": deal_id})
            res = json.loads(text)
            assert res["ok"], text
            deal = self.deal_state(deal_id)
            self.assertEqual(deal["status"], "closed")
            self.assertTrue(deal["versions"][1]["confirmed"])
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
