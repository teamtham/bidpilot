"""BidPilot Phase 3 HTTP regression tests.

Starts the real server in-process on an ephemeral loopback port with a temp
database, then exercises authentication, CSRF, duplicate intake, change
parsing, tamper/stale blocking, acceptance rules and exact engine figures.
Run: python3 -B -m unittest -v test_http
"""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import server


@contextmanager
def _db(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


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


class HttpTests(unittest.TestCase):
    srv = None
    owner = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-http-")
        cls.db_path = str(Path(cls.tmp) / "test.db")
        cls.srv, cls.owner = server.make_server(cls.db_path, port=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

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
    def get(cls, path, token=True):
        headers = {"X-Owner-Token": cls.owner} if token else {}
        return request(cls.base, "GET", path, None, headers)

    @classmethod
    def import_inquiry(cls, source_id, raw=RAW1):
        status, text = cls.post("/api/import", {
            "source": "hashmeta", "source_id": source_id, "raw_inquiry": raw,
            "offer_request": INQUIRY})
        assert status == 200, text
        return json.loads(text)

    @classmethod
    def flow(cls, source_id, raw=RAW1):
        """import -> evaluate -> check -> approve; returns key ids + client token."""
        imp = cls.import_inquiry(source_id, raw)
        _, text = cls.post("/api/evaluate", {"deal_id": imp["deal_id"],
                                             "offer_id": imp["offer_id"]})
        version_id = json.loads(text)["version_id"]
        _, text = cls.post("/api/check", {"version_id": version_id})
        assert json.loads(text)["ok"], text
        _, text = cls.post("/api/approve", {"version_id": version_id})
        res = json.loads(text)
        assert res["ok"], text
        return {"deal_id": imp["deal_id"], "offer_id": imp["offer_id"],
                "version_id": version_id, "token": res["token"],
                "url": res["url"]}

    def state(self):
        _, text = self.get("/api/state")
        return json.loads(text)

    def deal_state(self, deal_id):
        return next(d for d in self.state()["deals"] if d["deal_id"] == deal_id)

    def db(self):
        return _db(self.db_path)

    # -- auth & CSRF ---------------------------------------------------------
    def test_unauthenticated_state_denied(self):
        status, _ = self.get("/api/state", token=False)
        self.assertEqual(status, 403)
        status, _ = request(self.base, "GET", "/api/state",
                            None, {"X-Owner-Token": "wrong-token"})
        self.assertEqual(status, 403)
        status, _ = self.get("/dashboard", token=False)
        self.assertEqual(status, 403)

    def test_unauthenticated_approve_denied(self):
        status, body = request(self.base, "POST", "/api/approve",
                               {"version_id": "x"}, {"Origin": self.base})
        self.assertEqual(status, 403)
        self.assertIn("forbidden", body)
        status, _ = request(self.base, "POST", "/api/approve", {"version_id": "x"},
                            {"X-Owner-Token": "wrong", "Origin": self.base})
        self.assertEqual(status, 403)

    def test_csrf_rejected_on_state_changes(self):
        # Founder route without Origin header
        status, _ = self.post("/api/import", {"x": 1}, token=True, origin=False)
        self.assertEqual(status, 403)
        # Founder route with a foreign Origin
        status, _ = request(self.base, "POST", "/api/approve", {"version_id": "x"},
                            {"X-Owner-Token": self.owner, "Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        # Client token route without Origin header
        f = self.flow("csrf-1")
        status, _ = request(self.base, "POST", f"/p/{f['token']}/accept",
                            {"option_id": "x"})
        self.assertEqual(status, 403)

    # -- duplicate intake -----------------------------------------------------
    def test_duplicate_import_reuses_deal_and_first_offer(self):
        first = self.import_inquiry("dup-1", RAW1)
        second = self.import_inquiry("dup-1", "DIFFERENT raw text, should not overwrite")
        self.assertTrue(first["fresh"])
        self.assertFalse(second["fresh"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["deal_id"], second["deal_id"])
        self.assertEqual(first["offer_id"], second["offer_id"])
        deal = self.deal_state(second["deal_id"])
        self.assertEqual(len(deal["offers"]), 1)  # no extra offer added
        with self.db() as conn:
            row = conn.execute("SELECT raw_inquiry FROM deals WHERE id=?",
                               (second["deal_id"],)).fetchone()
        self.assertEqual(row["raw_inquiry"], RAW1)  # history not overwritten

    # -- happy path & exact figures -------------------------------------------
    def test_happy_path_exact_figures(self):
        f = self.flow("happy-1")
        deal = self.deal_state(f["deal_id"])
        v = deal["versions"][0]
        self.assertEqual(v["status"], "published")
        self.assertEqual(v["outbound_status"], "passed")
        self.assertEqual(v["verdict"], "accept")
        est = v["estimate"]
        self.assertEqual(est["fixed_hours"], 8)
        self.assertEqual(est["unit_hours"], 7)
        self.assertEqual(est["base_hours"], 30.5)
        self.assertEqual(est["planning_hours"], 35.07)
        self.assertEqual(est["capacity_hours"], 50.4)
        self.assertEqual(est["list_sgd"], 2700)
        self.assertEqual(est["floor_sgd"], 2454.9)
        self.assertEqual(est["effective_rate_sgd"], 76.99)
        self.assertEqual(len(v["options"]), 1)
        opt = v["options"][0]
        self.assertEqual(opt["option_id"], "as_offered")
        self.assertEqual(opt["deliverables"], 3)
        self.assertEqual(opt["caption_versions"], 3)
        self.assertEqual(opt["deadline"], "2026-12-11")
        self.assertEqual(opt["price_sgd"], 2700)
        self.assertEqual(opt["phases"], [])

    def test_client_page_shows_only_public_terms(self):
        f = self.flow("page-1")
        status, html = request(self.base, "GET", f["url"], None, {})
        self.assertEqual(status, 200)
        self.assertIn("S$2,700", html)
        self.assertNotIn("2,454", html)     # floor never shown
        self.assertNotIn("Glow Studio", html)  # other client names never shown
        self.assertNotIn("floor", html.lower())
        status, _ = request(self.base, "GET", "/p/not-a-real-token", None, {})
        self.assertEqual(status, 404)

    # -- founder stage timeline (private) ------------------------------------
    def test_founder_dashboard_renders_stage_timeline(self):
        f = self.flow("timeline-1")
        status, html = self.get("/")
        self.assertEqual(status, 200)
        # The wireframe-v5 dashboard shows a capped, plain-language History
        # per deal (built from run steps) and a full Run log in Studio tools.
        for marker in ("History", "histHtml", "steps_total", "Run log",
                       "Brief received", "Estimate made", "Proposal sent",
                       "You confirmed the booking"):
            self.assertIn(marker, html)
        # state() exposes the backward-compatible trigger field on every step
        d = self.deal_state(f["deal_id"])
        self.assertTrue(d["steps"])
        for s in d["steps"]:
            self.assertIn("trigger", s)
        # founder approve steps record a manual trigger
        approve = [s for s in d["steps"] if s["step_key"] == "approve"]
        self.assertTrue(approve)
        self.assertTrue(all(s["trigger"] == "manual" for s in approve))

    def test_client_page_never_exposes_timeline(self):
        f = self.flow("timeline-2")
        status, html = request(self.base, "GET", f["url"], None, {})
        self.assertEqual(status, 200)
        for banned in ("stage-timeline", "Deal timeline", "stageTimelineHtml",
                       "STAGE_DEFS", "stage-trigger", "run_steps", "private_note",
                       "Inbound message claimed"):
            self.assertNotIn(banned, html)

    # -- C5: what-changed comparison + needs-info (founder-only) -------------
    def test_founder_dashboard_serves_what_changed_and_needs_info(self):
        status, html = self.get("/")
        self.assertEqual(status, 200)
        # Changed terms show inline in the Brief ("was ..."); needs-info deals
        # list exactly what is missing; plain-language warnings replace codes.
        for marker in ("(was ", "changed_fields", "Needs info", "Missing",
                       "needs_information", "I need a bit more before I can price this",
                       "Not enough time before", "Price is under your minimum"):
            self.assertIn(marker, html)

    def test_client_page_never_exposes_comparison_or_needs_info(self):
        f = self.flow("cmp-iso-1")
        status, html = request(self.base, "GET", f["url"], None, {})
        self.assertEqual(status, 200)
        for banned in ("whatChangedHtml", "What changed", "reasonLinkHtml",
                       "reason-links", "needs-info", "Needs info", "diff-old",
                       "diff-new", "changed_fields", "CAPACITY_SHORTFALL",
                       "BELOW_FLOOR", "planning_hours", "capacity_hours", "floor"):
            self.assertNotIn(banned, html)

    # -- unsafe draft ----------------------------------------------------------
    def test_unsafe_draft_blocked(self):
        imp = self.import_inquiry("unsafe-1")
        _, text = self.post("/api/evaluate", {"deal_id": imp["deal_id"],
                                              "offer_id": imp["offer_id"]})
        version_id = json.loads(text)["version_id"]
        self.post("/api/draft", {"version_id": version_id,
                                 "body": "Our price floor is S$2,454.90. Ask Glow Studio."})
        # the saved draft is a NEW version; the check must run against it
        with self.db() as conn:
            version_id = conn.execute(
                "SELECT id FROM proposal_versions WHERE deal_id=? ORDER BY version_no DESC"
                " LIMIT 1", (imp["deal_id"],)).fetchone()["id"]
        status, text = self.post("/api/check", {"version_id": version_id})
        res = json.loads(text)
        self.assertEqual(status, 200)
        self.assertFalse(res["ok"])
        codes = [v["code"] for v in res["result"]["violations"]]
        self.assertIn("PRIVATE_LABEL", codes)
        self.assertIn("OTHER_CLIENT_NAME", codes)
        self.assertIn("UNAPPROVED_AMOUNT", codes)
        # and release is refused for the blocked draft
        status, text = self.post("/api/approve", {"version_id": version_id})
        self.assertEqual(status, 409)
        self.assertIn("outbound_not_passed", json.loads(text)["reasons"])

    # -- tamper & option-set integrity -----------------------------------------
    def test_tampered_option_terms_blocked(self):
        imp = self.import_inquiry("tamper-1")
        _, text = self.post("/api/evaluate", {"deal_id": imp["deal_id"],
                                              "offer_id": imp["offer_id"]})
        version_id = json.loads(text)["version_id"]
        with self.db() as conn:
            conn.execute("UPDATE options SET public_terms_json=json_set(public_terms_json,"
                         " '$.price_sgd', 99) WHERE id IN (SELECT value FROM json_each("
                         " (SELECT public_option_ids_json FROM proposal_versions"
                         "  WHERE id=?)))", (version_id,))
            conn.commit()
        status, text = self.post("/api/check", {"version_id": version_id})
        res = json.loads(text)
        self.assertFalse(res["ok"])
        codes = [v["code"] for v in res["result"]["violations"]]
        self.assertIn("OPTION_SET_INVALID", codes)
        self.assertTrue(any("option_terms_mismatch" in v["text"] for v in
                            res["result"]["violations"]))
        status, text = self.post("/api/approve", {"version_id": version_id})
        self.assertEqual(status, 409)
        self.assertTrue(any(r.startswith("option_terms_mismatch")
                            for r in json.loads(text)["reasons"]))

    def test_option_ids_out_of_order_or_duplicated_blocked(self):
        f = self.flow("order-1")
        # a counteroffer version has 4 options; reorder them in storage
        _, text = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": "ord-chg-1",
            "body": "five videos, five captioned versions by 4 Dec, same S$2,700"})
        offer2 = json.loads(text)["offer_id"]
        _, text = self.post("/api/evaluate", {"deal_id": f["deal_id"], "offer_id": offer2})
        version_id = json.loads(text)["version_id"]
        with self.db() as conn:
            row = conn.execute("SELECT public_option_ids_json FROM proposal_versions"
                               " WHERE id=?", (version_id,)).fetchone()
            ids = json.loads(row["public_option_ids_json"])
            self.assertGreaterEqual(len(ids), 2)
            conn.execute("UPDATE proposal_versions SET public_option_ids_json=? WHERE id=?",
                         (json.dumps(list(reversed(ids))), version_id))
            conn.commit()
        _, text = self.post("/api/check", {"version_id": version_id})
        res = json.loads(text)
        self.assertFalse(res["ok"])
        self.assertIn("option_ids_out_of_order",
                      [v["text"] for v in res["result"]["violations"]])

    def test_stale_draft_approve_blocked(self):
        f = self.flow("stale-1")
        _, text = self.post("/api/draft", {"version_id": f["version_id"],
                                           "body": "Thanks Juniper Cafe — we can deliver"
                                                   " three videos by 2026-12-11 for"
                                                   " S$2,700."})
        new_version = json.loads(text)["version_id"]
        status, text = self.post("/api/approve", {"version_id": f["version_id"]})
        self.assertEqual(status, 409)
        self.assertIn("not_latest_version", json.loads(text)["reasons"])
        # the fresh version is still releasable after a check
        self.post("/api/check", {"version_id": new_version})
        status, text = self.post("/api/approve", {"version_id": new_version})
        self.assertTrue(json.loads(text)["ok"], text)

    # -- client acceptance ------------------------------------------------------
    def test_client_accept_rules(self):
        f = self.flow("accept-1")
        _, text = self.post("/api/approve", None, token=False)  # auth sanity
        # 1: missing option_id
        status, text = request(self.base, "POST", f"/p/{f['token']}/accept",
                               {}, {"Origin": self.base})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(text)["ok"])
        # 2: cross-deal option id
        other = self.flow("accept-2")
        with self.db() as conn:
            other_opt = conn.execute(
                "SELECT value FROM json_each((SELECT public_option_ids_json"
                " FROM proposal_versions WHERE id=?))", (other["version_id"],)
            ).fetchone()["value"]
        status, text = request(self.base, "POST", f"/p/{f['token']}/accept",
                               {"option_id": other_opt}, {"Origin": self.base})
        self.assertFalse(json.loads(text)["ok"])
        self.assertIn("does not belong", json.loads(text)["error"])
        # 3: happy accept with the right option
        with self.db() as conn:
            own_opt = conn.execute(
                "SELECT value FROM json_each((SELECT public_option_ids_json"
                " FROM proposal_versions WHERE id=?))", (f["version_id"],)
            ).fetchone()["value"]
        status, text = request(self.base, "POST", f"/p/{f['token']}/accept",
                               {"option_id": own_opt}, {"Origin": self.base})
        self.assertTrue(json.loads(text)["ok"], text)
        # 4: repeat acceptance refused
        status, text = request(self.base, "POST", f"/p/{f['token']}/accept",
                               {"option_id": own_opt}, {"Origin": self.base})
        self.assertFalse(json.loads(text)["ok"])
        self.assertIn("Already accepted", json.loads(text)["error"])

    def test_stale_acceptance_after_supersede_blocked(self):
        f = self.flow("supers-1")
        _, text = self.post("/api/draft", {"version_id": f["version_id"],
                                           "body": "Thanks Juniper Cafe — we can deliver"
                                                   " three videos by 2026-12-11 for"
                                                   " S$2,700."})
        v2 = json.loads(text)["version_id"]
        self.post("/api/check", {"version_id": v2})
        status, text = self.post("/api/approve", {"version_id": v2})
        self.assertTrue(json.loads(text)["ok"], text)
        with self.db() as conn:
            own_opt = conn.execute(
                "SELECT value FROM json_each((SELECT public_option_ids_json"
                " FROM proposal_versions WHERE id=?))", (f["version_id"],)
            ).fetchone()["value"]
        status, text = request(self.base, "POST", f"/p/{f['token']}/accept",
                               {"option_id": own_opt}, {"Origin": self.base})
        self.assertEqual(status, 404)  # old token no longer resolves

    # -- client change parsing ---------------------------------------------------
    def test_client_change_parses_explicit_terms_and_matches_engine(self):
        f = self.flow("change-1")
        status, text = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": "chg-explicit-1",
            "body": "five videos, five captioned versions by 4 Dec, same S$2,700,"
                    " your owner already agreed"})
        self.assertEqual(status, 200)
        offer_id = json.loads(text)["offer_id"]
        self.assertIsNotNone(offer_id)
        # duplicate request_id (same payload retry) is idempotent: same offer,
        # no extra round, no error. The retry must repeat the EXACT body —
        # reuse of a request_id with a different payload is a 409 conflict
        # (covered in test_overnight_regressions).
        status, text = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": "chg-explicit-1",
            "body": "five videos, five captioned versions by 4 Dec, same S$2,700,"
                    " your owner already agreed"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text)["offer_id"], offer_id)
        # evaluate the new offer round and check the applied terms
        self.post("/api/evaluate", {"deal_id": f["deal_id"], "offer_id": offer_id})
        deal = self.deal_state(f["deal_id"])
        self.assertEqual(len(deal["offers"]), 2)  # history preserved
        offer2 = deal["offers"][1]["offer"]
        self.assertEqual(offer2["deliverables"], 5)
        self.assertEqual(offer2["caption_versions"], 5)
        self.assertEqual(offer2["deadline"], "2026-12-04")
        self.assertEqual(offer2["budget_sgd"], 2700)
        v2 = deal["versions"][1]
        self.assertEqual(v2["verdict"], "counter")
        # exact engine figures for the 5/5/2026-12-04/2700 counteroffer
        est = v2["estimate"]
        self.assertEqual(est["planning_hours"], 52.32)
        self.assertEqual(est["list_sgd"], 4000)
        self.assertEqual(est["floor_sgd"], 3662.4)
        self.assertEqual(est["capacity_hours"], 28.8)
        prices = {o["option_id"]: o["price_sgd"] for o in v2["options"]}
        self.assertEqual(prices, {"hold_date": 2050, "hold_budget": 2700,
                                  "hold_scope": 4000, "phased": 4000})
        # terms identical to a direct engine run of the same request
        import core
        direct = core.run_engine_evaluate({
            "as_of": "2026-11-23",
            "comparable_ids": ["P-031", "P-036", "P-041", "P-044"],
            "offer": {**INQUIRY["offer"], "deliverables": 5, "caption_versions": 5,
                      "deadline": "2026-12-04", "budget_sgd": 2700}})
        self.assertEqual(v2["options"], direct["options"])
        self.assertEqual(v2["estimate"], direct["estimate"])

    def test_client_change_unstated_captions_keep_prior(self):
        f = self.flow("change-2")
        status, text = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": "chg-caps-1",
            "body": "four videos by 4 Dec for S$2,700"})
        self.assertEqual(status, 200)
        offer_id = json.loads(text)["offer_id"]
        self.post("/api/evaluate", {"deal_id": f["deal_id"], "offer_id": offer_id})
        deal = self.deal_state(f["deal_id"])
        offer2 = deal["offers"][1]["offer"]
        self.assertEqual(offer2["deliverables"], 4)
        self.assertEqual(offer2["caption_versions"], 3)  # prior value, unstated
        self.assertEqual(offer2["deadline"], "2026-12-04")
        self.assertEqual(offer2["budget_sgd"], 2700)

    def test_unknown_change_text_queued_not_fixture(self):
        f = self.flow("change-3")
        before = len(self.deal_state(f["deal_id"])["offers"])
        status, text = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": "chg-unknown-1",
            "body": "we want something more premium, please advise options"})
        self.assertEqual(status, 202)
        res = json.loads(text)
        self.assertEqual(res["status"], "clarify")
        self.assertIsNone(res["offer_id"])
        after = self.deal_state(f["deal_id"])
        self.assertEqual(len(after["offers"]), before)  # no offer invented
        queued = [s for s in after["steps"] if s["step_key"] == "client_change"
                  and s["status"] == "queued"]
        self.assertTrue(queued)
        # no counteroffer fixture terms were silently created
        for offer in after["offers"]:
            self.assertNotEqual(offer["offer"]["deliverables"], 5)


class EmbedFramingTests(unittest.TestCase):
    def test_chat_pages_allow_listed_embed_origins_only(self):
        tmp = tempfile.mkdtemp(prefix="bidpilot-embed-")
        srv, owner = server.make_server(str(Path(tmp) / "test.db"), port=0,
                                        embed_origin="https://studio.example.com, https://shell.example.com")
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ("/inquire", "/i/any-token"):
                try:
                    with urllib.request.urlopen(base + path) as resp:
                        csp = resp.headers.get("Content-Security-Policy")
                except urllib.error.HTTPError as err:
                    csp = err.headers.get("Content-Security-Policy")
                self.assertEqual(csp, "frame-ancestors 'self' https://studio.example.com https://shell.example.com")
            with urllib.request.urlopen(base + "/?owner=" + owner) as resp:
                csp = resp.headers.get("Content-Security-Policy")
            self.assertEqual(csp, "frame-ancestors 'self'")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_invalid_embed_origin_rejected(self):
        tmp = tempfile.mkdtemp(prefix="bidpilot-embed-bad-")
        with self.assertRaises(ValueError):
            server.make_server(str(Path(tmp) / "test.db"), port=0,
                               embed_origin="http://insecure.example.com")


class ChatHistoryPersistenceTests(unittest.TestCase):
    """The embedded chat must resume the same user's thread across reloads."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-chat-history-")
        cls.srv, cls.owner = server.make_server(str(Path(cls.tmp) / "test.db"), port=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_start_page_resumes_saved_inquiry(self):
        status, html = request(self.base, "GET", "/inquire")
        self.assertEqual(status, 200)
        # guarded per-browser token storage (third-party iframe storage can throw)
        self.assertIn("localStorage.getItem(TOKEN_KEY)", html)
        self.assertIn("function saveToken(t){try{", html)
        # resume check against the thread state, then redirect with full history
        self.assertIn("fetch('/i/'+saved+'/state')", html)
        self.assertIn("location.replace('/i/'+saved)", html)
        # explicit new-inquiry escape hatch clears the saved token
        self.assertIn("has('new')", html)
        self.assertIn("clearToken()", html)
        # token saved as soon as an inquiry is created
        self.assertIn("saveToken(token)", html)
        # sending is blocked while the resume check is in flight
        self.assertIn("if(busy||resuming)return", html)

    def test_private_page_saves_token_and_offers_new_inquiry(self):
        status, body = request(self.base, "POST", "/inquire",
                               {"text": "history test", "request_id": "hist-1"},
                               {"Origin": self.base})
        self.assertEqual(status, 200)
        token = json.loads(body)["token"]
        status, html = request(self.base, "GET", "/i/" + token)
        self.assertEqual(status, 200)
        self.assertIn("saveToken(token)", html)
        self.assertIn('id="newinquiry"', html)
        self.assertIn("clearToken();location.href='/inquire?new=1'", html)
        # full history is served for the saved thread
        status, body = request(self.base, "POST", f"/i/{token}/messages",
                               {"text": "second message", "request_id": "hist-2"},
                               {"Origin": self.base})
        self.assertEqual(status, 200)
        status, body = request(self.base, "GET", f"/i/{token}/state")
        self.assertEqual(status, 200)
        turns = json.loads(body)["turns"]
        self.assertEqual([t["body"] for t in turns], ["history test", "second message"])

    def test_new_query_serves_start_page(self):
        status, html = request(self.base, "GET", "/inquire?new=1")
        self.assertEqual(status, 200)
        self.assertIn("Start a private inquiry", html)

    def test_unknown_saved_thread_404s_so_storage_is_cleared(self):
        status, _ = request(self.base, "GET", "/i/" + "x" * 43 + "/state")
        self.assertEqual(status, 404)


class ReferenceRemovalTests(unittest.TestCase):
    """Clients can delete a reference they uploaded by mistake."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-ref-remove-")
        cls.srv, cls.owner = server.make_server(str(Path(cls.tmp) / "test.db"), port=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def _new_inquiry(self, text, req):
        status, body = request(self.base, "POST", "/inquire",
                               {"text": text, "request_id": req}, {"Origin": self.base})
        self.assertEqual(status, 200)
        return json.loads(body)["token"]

    def test_client_removes_reference_over_http(self):
        import base64
        token = self._new_inquiry("removal test", "rm-1")
        status, body = request(self.base, "POST", f"/i/{token}/upload",
                               {"filename": "oops.mp4", "mime": "video/mp4",
                                "data": base64.b64encode(b"not-a-video").decode()},
                               {"Origin": self.base})
        self.assertEqual(status, 200)
        asset_id = json.loads(body)["asset_id"]
        status, body = request(self.base, "GET", f"/i/{token}/state")
        self.assertEqual([a["id"] for a in json.loads(body)["assets"]], [asset_id])
        # The private page renders a Remove control wired to the remove route.
        status, html = request(self.base, "GET", f"/i/{token}")
        self.assertIn("base+'/remove'", html)
        self.assertIn("Tap again to remove", html)
        # A different inquiry's token cannot remove this asset.
        other = self._new_inquiry("other client", "rm-2")
        status, body = request(self.base, "POST", f"/i/{other}/remove",
                               {"asset_id": asset_id}, {"Origin": self.base})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "not_found")
        # Cross-origin write is rejected by the same-origin CSRF guard.
        status, _ = request(self.base, "POST", f"/i/{token}/remove",
                            {"asset_id": asset_id}, {"Origin": "https://evil.example.com"})
        self.assertEqual(status, 403)
        # The owner removes it.
        status, body = request(self.base, "POST", f"/i/{token}/remove",
                               {"asset_id": asset_id}, {"Origin": self.base})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["removed"], asset_id)
        status, body = request(self.base, "GET", f"/i/{token}/state")
        self.assertEqual(json.loads(body)["assets"], [])
        # The media file is no longer served.
        status, _ = request(self.base, "GET", f"/i/{token}/asset/{asset_id}")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
