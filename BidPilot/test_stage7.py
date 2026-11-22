"""BidPilot Stage 7 UI repair regression tests.

Covers the browser-UI defects found in the wild: generated page JavaScript must
be syntactically valid (the founder dashboard previously emitted
``const FIXTURES = {[...],[...]};`` — braces around a JSON array — which killed
the whole script, froze the deals list at "Loading deals…" and made the fixture
buttons inert), and the client change form must carry explicit captioned-count
fields so an adversarial free-text message keeps its exact wording while the
counters state the terms (five captioned versions), reproducing the exact
52.32-hour counteroffer estimate from the released client link.

Run: python3 -B -m unittest -v test_stage7
"""
import json
import re
import shutil
import subprocess
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

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
RAW1 = ("three 30-second Instagram promo videos, one location, captions,"
        " by 11 Dec, budget S$2,700")
# Adversarial free text: nothing parseable, kept verbatim in the audit trail.
ADVERSARIAL = ("Actually, let's revisit — not sure about any of the numbers,"
               " maybe fewer, maybe more, definitely captions though."
               " Can you just make it work?")

NODE = shutil.which("node")


@contextmanager
def _db(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


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


class Stage7Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-s7-")
        cls.db_path = str(Path(cls.tmp) / "s7.db")
        cls.srv, cls.owner = server.make_server(cls.db_path, port=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    # -- helpers ------------------------------------------------------------
    @classmethod
    def post(cls, path, data=None, token=True, origin=True):
        headers = {}
        if token:
            headers["X-Owner-Token"] = cls.owner
        if origin:
            headers["Origin"] = cls.base
        return request(cls.base, "POST", path, data, headers)

    @classmethod
    def get(cls, path, token=True, origin=None):
        headers = {"X-Owner-Token": cls.owner} if token else {}
        if origin:
            headers["Origin"] = origin
        return request(cls.base, "GET", path, None, headers)

    def state(self):
        _, text = self.get("/api/state")
        return json.loads(text)

    def deal_state(self, deal_id):
        return next(d for d in self.state()["deals"] if d["deal_id"] == deal_id)

    def db(self):
        return _db(self.db_path)

    @classmethod
    def flow(cls, source_id):
        status, text = cls.post("/api/import", {
            "source": "hashmeta", "source_id": source_id, "raw_inquiry": RAW1,
            "offer_request": INQUIRY})
        assert status == 200, text
        imp = json.loads(text)
        _, text = cls.post("/api/evaluate", {"deal_id": imp["deal_id"],
                                             "offer_id": imp["offer_id"]})
        version_id = json.loads(text)["version_id"]
        _, text = cls.post("/api/check", {"version_id": version_id})
        assert json.loads(text)["ok"], text
        _, text = cls.post("/api/approve", {"version_id": version_id})
        res = json.loads(text)
        assert res["ok"], text
        return {"deal_id": imp["deal_id"], "offer_id": imp["offer_id"],
                "version_id": version_id, "url": res["url"]}

    def scripts(self, html):
        return re.findall(r"<script>(.*?)</script>", html, re.S)

    def node_check(self, name, js):
        path = Path(self.tmp) / f"{name}.js"
        path.write_text(js)
        proc = subprocess.run(["node", "--check", str(path)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         f"{name} JS fails node --check: {proc.stderr[:500]}")

    # -- 1: generated JS is syntactically valid -------------------------------
    @unittest.skipIf(NODE is None, "node not available")
    def test_generated_founder_and_client_js_pass_node_check(self):
        _, founder_html = self.get("/dashboard")
        self.node_check("founder", "\n".join(self.scripts(founder_html)))

        flow = self.flow("s7-js-1")
        _, client_html = request(self.base, "GET", flow["url"], None, {})
        self.node_check("client", "\n".join(self.scripts(client_html)))
        _, preview_html = self.get(f"/preview/{flow['version_id']}")
        self.node_check("preview", "\n".join(self.scripts(preview_html)))

    def test_founder_fixtures_is_valid_direct_array_assignment(self):
        """The old bug: braces around the JSON array -> SyntaxError."""
        _, html = self.get("/dashboard")
        match = re.search(r"const FIXTURES = (\[.*?\]);\n", html, re.S)
        self.assertIsNotNone(match, "FIXTURES assignment not found")
        fixtures = json.loads(match.group(1))
        self.assertEqual([f["source_id"] for f in fixtures],
                         ["LEAD-0001", "LEAD-0002"])
        for fixture in fixtures:
            self.assertEqual(sorted(fixture), ["label", "offer_request",
                                               "raw", "source_id"])

    def test_founder_page_is_not_stuck_at_loading_deals_marker(self):
        """The root div placeholder exists, but the page ships a working
        refresh() call and fixture handlers the browser can actually run."""
        _, html = self.get("/dashboard")
        self.assertIn("Loading deals", html)  # placeholder pre-JS
        self.assertIn("async function refresh()", html)
        self.assertIn("refresh();", html)
        # Fixture loaders moved to Studio tools > Demo tools (one per FIXTURE).
        self.assertIn("importFixture(", html)
        self.assertIn("const FIXTURES = ", html)

    # -- 2: explicit captioned-count fields on the client change form ---------
    def test_client_change_form_has_explicit_captioned_count_fields(self):
        flow = self.flow("s7-form-1")
        _, html = request(self.base, "GET", flow["url"], None, {})
        for field in ("rc-count", "rc-captions", "rc-deadline", "rc-budget"):
            self.assertIn(f'id="{field}"', html)
        self.assertIn("rc-captions", "\n".join(self.scripts(html)))

    def test_counter_from_client_link_adversarial_body_explicit_terms(self):
        """The adversarial message keeps its exact text; the explicit
        counters supply five captioned versions; the resulting counteroffer
        evaluates to the exact 52.32-hour estimate."""
        flow = self.flow("s7-counter-1")
        terms = {"count": 5, "captions": 5, "deadline": "2026-12-04",
                 "budget": 2700}
        status, text = request(self.base, "POST", f"{flow['url']}/request-change",
                               {"body": ADVERSARIAL, "terms": terms},
                               {"Origin": self.base})
        self.assertEqual(status, 200, text)
        offer2 = json.loads(text)["offer_id"]
        self.assertIsNotNone(offer2)

        # adversarial body stored verbatim in the audit trail
        with self.db() as conn:
            stored = conn.execute(
                "SELECT body FROM messages WHERE direction='inbound'"
                " ORDER BY created_at DESC LIMIT 1").fetchone()["body"]
        self.assertEqual(stored, ADVERSARIAL)

        # five captioned versions actually applied
        with self.db() as conn:
            offer = json.loads(conn.execute(
                "SELECT offer_json FROM offers WHERE id=?",
                (offer2,)).fetchone()["offer_json"])
        self.assertEqual((offer["deliverables"], offer["caption_versions"]),
                         (5, 5))
        self.assertEqual(offer["deadline"], "2026-12-04")
        self.assertEqual(offer["budget_sgd"], 2700)

        # evaluate the counteroffer: the exact 52.32-hour figures
        _, text = self.post("/api/evaluate", {"deal_id": flow["deal_id"],
                                              "offer_id": offer2})
        version2 = json.loads(text)["version_id"]
        deal = self.deal_state(flow["deal_id"])
        v2 = next(v for v in deal["versions"] if v["version_id"] == version2)
        est = v2["estimate"]
        self.assertEqual((est["planning_hours"], est["capacity_hours"]),
                         (52.32, 28.8))
        self.assertEqual(est["list_sgd"], 4000)
        prices = {o["option_id"]: o["price_sgd"] for o in v2["options"]}
        self.assertEqual(prices, {"hold_date": 2050, "hold_budget": 2700,
                                  "hold_scope": 4000, "phased": 4000})

    def test_adversarial_body_without_terms_is_still_queued(self):
        """Explicit terms are required; the free text alone never fabricates."""
        flow = self.flow("s7-queue-1")
        status, text = request(self.base, "POST", f"{flow['url']}/request-change",
                               {"body": ADVERSARIAL}, {"Origin": self.base})
        self.assertEqual(status, 202, text)
        res = json.loads(text)
        self.assertEqual(res["status"], "clarify")
        self.assertIsNone(res["offer_id"])


if __name__ == "__main__":
    unittest.main()
