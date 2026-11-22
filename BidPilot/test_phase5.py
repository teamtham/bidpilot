"""BidPilot Phase 5 end-to-end UI/API regression tests.

Exercises the normal demo path on an ephemeral loopback server with an
isolated temporary database: labeled sample intake -> evaluation -> check ->
pre-approval client preview -> approve -> recoverable/reissuable client link
-> explicit client change -> counter evaluation (four option cards) -> clean
check and approval -> phased acceptance -> founder confirmation -> closed
state. Also covers staleness, tampering, authorization and cross-origin
guards. The preview database is never touched.

Run: python3 -B -m unittest -v test_phase5
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
RAW1 = ("three 30-second Instagram promo videos, one location, captions,"
        " by 11 Dec, budget S$2,700")
CHANGE = "five videos, five captioned versions by 4 Dec for S$2,700"


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


class Phase5Tests(unittest.TestCase):
    # Stage 11: every test gets a fresh database + ephemeral server, because a
    # confirmed booking now durably reserves capacity for later evaluations.
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bidpilot-p5-")
        self.db_path = str(Path(self.tmp) / "p5.db")
        self.srv, self.owner = server.make_server(self.db_path, port=0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    # -- helpers ------------------------------------------------------------
    def post(self, path, data=None, token=True, origin=True):
        headers = {}
        if token:
            headers["X-Owner-Token"] = self.owner
        if origin:
            headers["Origin"] = self.base
        return request(self.base, "POST", path, data, headers)

    def get(self, path, token=True, origin=None):
        headers = {"X-Owner-Token": self.owner} if token else {}
        if origin:
            headers["Origin"] = origin
        return request(self.base, "GET", path, None, headers)

    def state(self):
        _, text = self.get("/api/state")
        return json.loads(text)

    def deal_state(self, deal_id):
        return next(d for d in self.state()["deals"] if d["deal_id"] == deal_id)

    def db(self):
        return _db(self.db_path)

    def import_inquiry(self, source_id, raw=RAW1):
        status, text = self.post("/api/import", {
            "source": "hashmeta", "source_id": source_id, "raw_inquiry": raw,
            "offer_request": INQUIRY})
        assert status == 200, text
        return json.loads(text)

    def evaluate(self, deal_id, offer_id):
        _, text = self.post("/api/evaluate", {"deal_id": deal_id, "offer_id": offer_id})
        return json.loads(text)

    def option_row_id(self, version_id, option_key):
        with self.db() as conn:
            row = conn.execute(
                "SELECT value FROM json_each((SELECT public_option_ids_json"
                " FROM proposal_versions WHERE id=?))", (version_id,)).fetchall()
            ids = [r["value"] for r in row]
        for oid in ids:
            with self.db() as conn:
                key = conn.execute("SELECT option_key FROM options WHERE id=?",
                                   (oid,)).fetchone()["option_key"]
            if key == option_key:
                return oid
        raise AssertionError(f"option {option_key} not found")

    # -- 1: intake, validation, idempotent evaluate -------------------------
    def test_intake_validation_and_idempotent_evaluate(self):
        # missing budget -> incomplete intake persists as needs_information;
        # nothing is fabricated, no offer is created, and the missing field path
        # is recorded so it can be supplied later (C5 behavior).
        bad = json.loads(json.dumps(INQUIRY))
        del bad["offer"]["budget_sgd"]
        status, text = self.post("/api/import", {
            "source": "hashmeta", "source_id": "p5-invalid-1",
            "raw_inquiry": "x", "offer_request": bad})
        self.assertEqual(status, 200)
        res = json.loads(text)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["offer_id"])
        with self.db() as conn:
            deal = conn.execute(
                "SELECT id, status FROM deals WHERE source_id='p5-invalid-1'").fetchone()
            self.assertIsNotNone(deal)
            self.assertEqual(deal["status"], "needs_information")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM offers WHERE deal_id=?", (deal["id"],)).fetchone()[0], 0)
            note = conn.execute(
                "SELECT private_note FROM run_steps WHERE deal_id=? AND"
                " step_key='needs_information' ORDER BY rowid DESC LIMIT 1",
                (deal["id"],)).fetchone()
            self.assertIn("offer.budget_sgd",
                          json.loads(note["private_note"])["missing_fields"])

        imp = self.import_inquiry("p5-e2e-1")
        self.assertTrue(imp["fresh"])
        # duplicate resumes without overwriting or a second offer
        dup = self.import_inquiry("p5-e2e-1", "DIFFERENT raw text on retry")
        self.assertEqual((dup["deal_id"], dup["offer_id"]),
                         (imp["deal_id"], imp["offer_id"]))
        self.assertTrue(dup["duplicate"])

        # state surfaces raw inbound + structured fields + round
        deal = self.deal_state(imp["deal_id"])
        self.assertEqual(deal["raw_inquiry"], RAW1)
        self.assertEqual(len(deal["offers"]), 1)
        self.assertEqual(deal["offers"][0]["round"], 1)
        self.assertEqual(deal["offers"][0]["offer"]["deliverables"], 3)
        self.assertEqual(deal["offers"][0]["offer"]["as_of"] if False else
                         deal["offers"][0]["as_of"], "2026-11-23")

        # idempotent evaluate: same version, one evaluation, one version
        first = self.evaluate(imp["deal_id"], imp["offer_id"])
        second = self.evaluate(imp["deal_id"], imp["offer_id"])
        self.assertEqual(first["version_id"], second["version_id"])
        self.assertEqual(first["eval_id"], second["eval_id"])
        with self.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE offer_id=?",
                (imp["offer_id"],)).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM proposal_versions WHERE deal_id=?",
                (imp["deal_id"],)).fetchone()[0], 1)

        # fixture 1 exact figures
        deal = self.deal_state(imp["deal_id"])
        v = deal["versions"][0]
        est = v["estimate"]
        self.assertEqual((est["planning_hours"], est["capacity_hours"]),
                         (35.07, 50.4))
        self.assertEqual(est["list_sgd"], 2700)

        # check passes
        _, text = self.post("/api/check", {"version_id": first["version_id"]})
        self.assertTrue(json.loads(text)["ok"], text)

    def test_pre_approval_preview_and_release_flow(self):
        imp = self.import_inquiry("p5-e2e-2")
        ev = self.evaluate(imp["deal_id"], imp["offer_id"])
        version_id = ev["version_id"]
        _, text = self.post("/api/check", {"version_id": version_id})
        self.assertTrue(json.loads(text)["ok"], text)

        # option cards / phased dates / outbound status visible pre-approval
        deal = self.deal_state(imp["deal_id"])
        v = deal["versions"][0]
        self.assertEqual(v["outbound_status"], "passed")
        self.assertEqual(len(v["options"]), 1)

        # owner-only preview of the exact client page before approval
        status, html = self.get(f"/preview/{version_id}")
        self.assertEqual(status, 200)
        self.assertIn("S$2,700", html)
        self.assertIn("As offered", html)
        self.assertIn("Preview only", html)
        status, _ = self.get(f"/preview/{version_id}", token=False)
        self.assertEqual(status, 403)
        status, _ = request(self.base, "GET", f"/preview/{version_id}",
                            None, {"X-Owner-Token": "wrong"})
        self.assertEqual(status, 403)

        # approve -> exact copyable URL in the response
        status, text = self.post("/api/approve", {"version_id": version_id})
        res = json.loads(text)
        self.assertTrue(res["ok"], text)
        url = res["url"]
        self.assertTrue(url.startswith("/p/"))
        # the sent card offers Go to page / Copy link from the founder's
        # private copy of the client link, which survives a reload
        status, html = self.get("/dashboard")
        self.assertEqual(status, 200)
        for marker in ("Go to page", "Copy link", "client_url"):
            self.assertIn(marker, html)
        _, text = self.get("/api/state")
        latest = [v for d in json.loads(text)["deals"] for v in d["versions"]
                  if v["version_id"] == version_id][0]
        self.assertEqual(latest["client_url"], url)
        # client page: public terms only
        status, html = request(self.base, "GET", url, None, {})
        self.assertEqual(status, 200)
        self.assertIn("S$2,700", html)
        self.assertNotIn("floor", html.lower())
        self.assertNotIn("capacity", html.lower())
        self.assertNotIn("Glow Studio", html)

    def test_rotate_reissue_invalidates_old_token(self):
        imp = self.import_inquiry("p5-rotate-1")
        ev = self.evaluate(imp["deal_id"], imp["offer_id"])
        self.post("/api/check", {"version_id": ev["version_id"]})
        _, text = self.post("/api/approve", {"version_id": ev["version_id"]})
        res = json.loads(text)
        old_token, deal_id = res["token"], imp["deal_id"]
        # unauthenticated and cross-origin rotate refused
        status, _ = request(self.base, "POST", "/api/rotate-token",
                            {"version_id": ev["version_id"]}, {"Origin": self.base})
        self.assertEqual(status, 403)
        status, _ = self.post("/api/rotate-token", {"version_id": ev["version_id"]},
                              origin=False)
        self.assertEqual(status, 403)
        # authorized rotate: new link works, old link stops resolving, audited
        status, text = self.post("/api/rotate-token", {"version_id": ev["version_id"]})
        rot = json.loads(text)
        self.assertTrue(rot["ok"], text)
        status, _ = request(self.base, "GET", f"/p/{old_token}", None, {})
        self.assertEqual(status, 404)
        status, html = request(self.base, "GET", rot["url"], None, {})
        self.assertEqual(status, 200)
        with self.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM approvals WHERE version_id=?"
                " AND action='rotate_token'", (ev["version_id"],)).fetchone()[0], 1)

    # -- 2: the full round trip with counter and booking --------------------
    def test_full_path_counter_acceptance_confirmation_closed(self):
        imp = self.import_inquiry("p5-e2e-3")
        ev = self.evaluate(imp["deal_id"], imp["offer_id"])
        v1 = ev["version_id"]
        self.post("/api/check", {"version_id": v1})
        _, text = self.post("/api/approve", {"version_id": v1})
        res = json.loads(text)
        token1 = res["token"]

        # explicit client change (all five captioned versions stated)
        status, text = request(self.base, "POST", f"/p/{token1}/request-change",
                               {"body": CHANGE}, {"Origin": self.base})
        self.assertEqual(status, 200)
        offer2 = json.loads(text)["offer_id"]
        self.assertIsNotNone(offer2)

        # counter evaluation: four named option cards, exact figures
        ev2 = self.evaluate(imp["deal_id"], offer2)
        v2 = ev2["version_id"]
        deal = self.deal_state(imp["deal_id"])
        self.assertEqual(len(deal["offers"]), 2)
        self.assertEqual(deal["offers"][1]["round"], 2)
        self.assertEqual(deal["versions"][1]["verdict"], "counter")
        est = deal["versions"][1]["estimate"]
        self.assertEqual((est["planning_hours"], est["capacity_hours"]), (52.32, 28.8))
        self.assertEqual(est["list_sgd"], 4000)
        prices = {o["option_id"]: o["price_sgd"] for o in deal["versions"][1]["options"]}
        self.assertEqual(prices, {"hold_date": 2050, "hold_budget": 2700,
                                  "hold_scope": 4000, "phased": 4000})
        labels = {m["option_id"]: m["label"]
                  for m in deal["versions"][1]["option_meta"]}
        self.assertEqual(labels["phased"], "Phased")
        self.assertEqual(labels["hold_scope"], "Keep all five")

        # clean check and approval of the counter version
        _, text = self.post("/api/check", {"version_id": v2})
        self.assertTrue(json.loads(text)["ok"], text)
        status, text = self.post("/api/approve", {"version_id": v2})
        res2 = json.loads(text)
        self.assertTrue(res2["ok"], text)
        token2 = res2["token"]

        # old token no longer resolves; stale acceptance impossible
        status, _ = request(self.base, "GET", f"/p/{token1}", None, {})
        self.assertEqual(status, 404)

        # phased acceptance on the current published version
        phased = self.option_row_id(v2, "phased")
        status, text = request(self.base, "POST", f"/p/{token2}/accept",
                               {"option_id": phased}, {"Origin": self.base})
        self.assertTrue(json.loads(text)["ok"], text)
        # repeat acceptance refused; controls disabled in HTML
        status, text = request(self.base, "POST", f"/p/{token2}/accept",
                               {"option_id": phased}, {"Origin": self.base})
        self.assertIn("Already accepted", json.loads(text)["error"])
        status, html = request(self.base, "GET", f"/p/{token2}", None, {})
        self.assertIn("your acceptance is recorded", html)
        self.assertIn("disabled", html)

        # Alex sees the exact accepted version + option
        deal = self.deal_state(imp["deal_id"])
        acc = deal["versions"][1]["accepted_option"]
        self.assertEqual(acc["terms"]["option_id"], "phased")
        self.assertEqual(acc["terms"]["price_sgd"], 4000)

        # confirm booking: unauthorized + cross-origin refused first
        status, _ = request(self.base, "POST", "/api/confirm-booking",
                            {"version_id": v2}, {"Origin": self.base})
        self.assertEqual(status, 403)
        status, _ = self.post("/api/confirm-booking", {"version_id": v2}, origin=False)
        self.assertEqual(status, 403)
        # confirming the superseded first version is refused
        status, text = self.post("/api/confirm-booking", {"version_id": v1})
        self.assertEqual(status, 409)
        # authorized confirmation: atomic, idempotent, audited
        status, text = self.post("/api/confirm-booking", {"version_id": v2})
        conf = json.loads(text)
        self.assertTrue(conf["ok"], text)
        self.assertEqual(conf["option_id"], phased)
        status, text = self.post("/api/confirm-booking", {"version_id": v2})
        again = json.loads(text)
        self.assertTrue(again["ok"])
        self.assertTrue(again.get("already"))
        with self.db() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM confirmations WHERE version_id=?",
                (v2,)).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM approvals WHERE version_id=?"
                " AND action='confirm_booking'", (v2,)).fetchone()[0], 1)
        deal = self.deal_state(imp["deal_id"])
        self.assertEqual(deal["status"], "confirmed")
        self.assertTrue(deal["versions"][1]["confirmed"])
        # full round history preserved
        self.assertEqual(len(deal["versions"]), 2)
        self.assertEqual(len(deal["offers"]), 2)

        # close the deal
        status, text = self.post("/api/close-deal", {"deal_id": imp["deal_id"]})
        self.assertTrue(json.loads(text)["ok"], text)
        deal = self.deal_state(imp["deal_id"])
        self.assertEqual(deal["status"], "closed")
        status, text = self.post("/api/close-deal", {"deal_id": imp["deal_id"]})
        self.assertTrue(json.loads(text).get("already"))

    def test_confirm_without_acceptance_refused(self):
        imp = self.import_inquiry("p5-noaccept-1")
        ev = self.evaluate(imp["deal_id"], imp["offer_id"])
        self.post("/api/check", {"version_id": ev["version_id"]})
        _, text = self.post("/api/approve", {"version_id": ev["version_id"]})
        self.assertTrue(json.loads(text)["ok"], text)
        status, text = self.post("/api/confirm-booking",
                                 {"version_id": ev["version_id"]})
        self.assertEqual(status, 409)
        self.assertIn("no_pending_acceptance", json.loads(text)["reasons"])

    # -- 3: staleness of the authoritative seed data -------------------------
    def test_stale_seed_blocks_check_and_release(self):
        imp = self.import_inquiry("p5-stale-1")
        ev = self.evaluate(imp["deal_id"], imp["offer_id"])
        v = ev["version_id"]
        # tamper the recorded snapshot hash -> evaluation is stale
        with self.db() as conn:
            conn.execute("UPDATE evaluations SET seed_hash='deadbeef'"
                         " WHERE offer_id=?", (imp["offer_id"],))
            conn.commit()
        status, text = self.post("/api/check", {"version_id": v})
        res = json.loads(text)
        self.assertFalse(res["ok"])
        self.assertIn("seed_data_stale",
                      [x["text"] for x in res["result"]["violations"]])
        status, text = self.post("/api/approve", {"version_id": v})
        self.assertEqual(status, 409)
        self.assertIn("seed_data_stale", json.loads(text)["reasons"])

    def test_sqlite_seed_tamper_blocks_release(self):
        imp = self.import_inquiry("p5-seedtamper-1")
        ev = self.evaluate(imp["deal_id"], imp["offer_id"])
        v = ev["version_id"]
        with self.db() as conn:
            conn.execute("UPDATE seed_rate_card SET price_sgd=price_sgd+1"
                         " WHERE item='promo_video'")
            conn.commit()
        status, text = self.post("/api/check", {"version_id": v})
        res = json.loads(text)
        self.assertFalse(res["ok"])
        self.assertTrue(any(x["text"].startswith("seed_table_mismatch")
                            for x in res["result"]["violations"]), res)
        status, text = self.post("/api/approve", {"version_id": v})
        self.assertEqual(status, 409)

    def test_blocked_estimate_cannot_release(self):
        imp = self.import_inquiry("p5-blocked-1")
        ev = self.evaluate(imp["deal_id"], imp["offer_id"])
        self.post("/api/draft", {"version_id": ev["version_id"],
                                 "body": "Our price floor is S$2,454.90."})
        with self.db() as conn:
            latest = conn.execute(
                "SELECT id FROM proposal_versions WHERE deal_id=?"
                " ORDER BY version_no DESC LIMIT 1", (imp["deal_id"],)).fetchone()["id"]
        self.post("/api/check", {"version_id": latest})
        status, text = self.post("/api/approve", {"version_id": latest})
        self.assertEqual(status, 409)
        self.assertIn("outbound_not_passed", json.loads(text)["reasons"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
