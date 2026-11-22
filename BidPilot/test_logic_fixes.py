"""Founder dashboard logic fixes: phases, plain refusals, quiet polls, labels.

Each test exercises one fix through the real server or service layer on a
temporary database. Nothing here loosens the outbound check or the approval
gate; several tests prove those still refuse.
"""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import core
import db
import deal_logic
import mcp_server
import server
import test_http

HERE = Path(__file__).resolve().parent
COUNTER_BODY = "five videos, five captioned versions by 4 Dec, same S$2,700"


class LogicFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-logic-")
        cls.db_path = str(Path(cls.tmp) / "test.db")
        cls.srv, cls.owner = server.make_server(cls.db_path, port=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    post = classmethod(test_http.HttpTests.post.__func__)
    get = classmethod(test_http.HttpTests.get.__func__)
    import_inquiry = classmethod(test_http.HttpTests.import_inquiry.__func__)
    flow = classmethod(test_http.HttpTests.flow.__func__)

    def conn(self):
        return db.connect(self.db_path)

    def deal(self, deal_id):
        _, text = self.get("/api/state")
        return next(d for d in json.loads(text)["deals"] if d["deal_id"] == deal_id)

    def evaluated(self, key, offer=None):
        request = dict(test_http.INQUIRY, offer={**test_http.INQUIRY["offer"], **(offer or {})})
        status, text = self.post("/api/import", {"source": "hashmeta", "source_id": key,
                                                  "raw_inquiry": "brief", "offer_request": request})
        self.assertEqual(status, 200, text)
        imp = json.loads(text)
        _, text = self.post("/api/evaluate", {"deal_id": imp["deal_id"], "offer_id": imp["offer_id"]})
        return imp["deal_id"], imp["offer_id"], json.loads(text)["version_id"]

    # 1 + 15 -- stale estimates ------------------------------------------------
    def test_stale_estimate_flags_deal_and_reevaluate_tool_reruns_it(self):
        deal_id, offer_id, version_id = self.evaluated("logic-stale")
        with self.conn() as c:
            c.execute("UPDATE evaluations SET capacity_revision=-1 WHERE offer_id=?", (offer_id,))
        with self.conn() as c:
            d = next(d for d in server.state(c)["deals"] if d["deal_id"] == deal_id)
        self.assertIn(d["phase"], ('ready_for_review', 'counter_ready'))
        self.assertTrue(d["needs_fresh_estimate"])
        self.assertIn("Re-run the estimate", d["fresh_estimate_reason"])
        mcp_server.DB_PATH = self.db_path
        args = {"deal_id": deal_id, "offer_id": offer_id, "request_id": "logic-stale-reeval"}
        first = mcp_server.t_reevaluate_offer(args)
        again = mcp_server.t_reevaluate_offer(args)
        self.assertEqual(first, again)                       # idempotent per request_id
        self.assertNotEqual(first["offer_id"], offer_id)     # a fresh round, old kept
        d = self.deal(deal_id)
        self.assertEqual(len(d["offers"]), 2)
        self.assertFalse(d["needs_fresh_estimate"])
        self.assertIn(d["phase"], ("ready_for_review", "counter_ready"))

    def test_refusals_carry_plain_english_messages(self):
        deal_id, _, version_id = self.evaluated("logic-refusal")
        status, text = self.post("/api/approve", {"version_id": version_id})
        self.assertEqual(status, 409)
        res = json.loads(text)
        self.assertIn("outbound_not_passed", res["reasons"])     # codes unchanged
        self.assertIn("hasn't passed the check", res["message"])
        self.assertTrue(all(r["message"] for r in res["refusals"]))
        status, text = self.post("/api/close-deal", {"deal_id": deal_id})
        self.assertIn("isn't available", json.loads(text)["message"])
        self.assertIn("calendar", deal_logic.explain("capacity_changed"))
        self.assertIn("option a, option b".replace("option ", ""),
                      deal_logic.explain("unsafe_options:a,b"))

    # 2 + 13 -- one phase per deal; rejected deals leave the queue -------------
    def test_rejected_deal_leaves_queue_and_reports_rejected(self):
        deal_id, _, version_id = self.evaluated("logic-reject")
        self.assertIn(self.deal(deal_id)["phase"], ("ready_for_review", "counter_ready"))
        status, _ = self.post("/api/reject", {"version_id": version_id})
        self.assertEqual(status, 200)
        d = self.deal(deal_id)
        self.assertEqual((d["status"], d["phase"]), ("rejected", "rejected"))
        self.assertFalse(d["show_estimate_checks"])

    def test_settle_rejected_deals_reconciles_older_rejections(self):
        deal_id, _, version_id = self.evaluated("logic-old-reject")
        with self.conn() as c:     # a rejection recorded before the deal status was settled
            c.execute("UPDATE proposal_versions SET status='rejected' WHERE id=?", (version_id,))
        with self.conn() as c:
            self.assertEqual(core.settle_rejected_deals(c), [deal_id])
            self.assertEqual(core.settle_rejected_deals(c), [])
        self.assertEqual(self.deal(deal_id)["status"], "rejected")

    def test_booked_deal_phase_hides_estimate_checks(self):
        f = self.flow("logic-booked")
        token = f["url"].rsplit("/", 1)[1]
        with self.conn() as c:
            option = json.loads(c.execute("SELECT public_option_ids_json FROM proposal_versions"
                                          " WHERE id=?", (f["version_id"],)).fetchone()[0])[0]
        test_http.request(self.base, "POST", f"/p/{token}/accept", {"option_id": option},
                          {"Origin": self.base})
        self.assertEqual(self.deal(f["deal_id"])["phase"], "sent")
        status, text = self.post("/api/confirm-booking", {"version_id": f["version_id"]})
        self.assertEqual(status, 200, text)
        d = self.deal(f["deal_id"])
        self.assertEqual((d["phase"], d["phase_label"]), ("booked", "Booked"))
        self.assertFalse(d["show_estimate_checks"])

    # 3 + 9 -- needs-info fields survive polls; polls don't grow history -----
    def test_needs_info_fields_survive_polls_and_polls_are_quiet(self):
        deal_id, offer_id, _ = self.evaluated("logic-ni")
        with self.conn() as c:
            c.execute("UPDATE deals SET status='needs_information' WHERE id=?", (deal_id,))
            core.record_step(c, deal_id, None, "needs_information", "blocked",
                             json.dumps({"missing_fields": ["offer.budget_sgd"],
                                         "question": "What budget should we plan for?"}))
            before = c.execute("SELECT COUNT(*) FROM run_steps WHERE deal_id=?", (deal_id,)).fetchone()[0]
            for i in range(5):    # scheduled polls re-reporting the same wait
                core.record_step(c, deal_id, None, "needs_information", "blocked",
                                 f"Scheduled poll {i}: still waiting for the budget")
            after = c.execute("SELECT COUNT(*) FROM run_steps WHERE deal_id=?", (deal_id,)).fetchone()[0]
            c.commit()
        self.assertEqual(before, after)
        d = self.deal(deal_id)
        self.assertEqual(d["needs_information"]["missing_fields"], ["offer.budget_sgd"])
        self.assertEqual(d["phase"], "needs_info")
        mcp_server.DB_PATH = self.db_path
        listed = [x["deal_id"] for x in mcp_server.t_list_pending_work({})["needs_information"]]
        self.assertNotIn(deal_id, listed)     # already asked: not handed out again

    def test_needs_info_falls_back_to_intake_output(self):
        deal_id, offer_id, _ = self.evaluated("logic-ni-fallback")
        with self.conn() as c:
            c.execute("INSERT INTO agent_outputs VALUES (?,?,?,?,?,?,?)",
                      (db.new_id(), deal_id, offer_id, "intake",
                       json.dumps({"offer": {}, "evidence": {}, "missing_fields": ["offer.budget_sgd"],
                                   "clarification": None}), "logic-ni-out", db.now()))
            core.record_step(c, deal_id, None, "needs_information", "blocked", "prose only")
            c.commit()
            info = core.needs_information_of(c, deal_id)
        self.assertEqual(info["missing_fields"], ["offer.budget_sgd"])

    def test_state_caps_timeline_and_keeps_latest_output_per_kind(self):
        deal_id, offer_id, _ = self.evaluated("logic-cap")
        with self.conn() as c:
            for i in range(60):
                core.record_step(c, deal_id, offer_id, "scoping" if i % 2 else "intake", "ok", f"n{i}")
            for i in range(3):
                c.execute("INSERT INTO agent_outputs VALUES (?,?,?,?,?,?,?)",
                          (db.new_id(), deal_id, offer_id, "scoping",
                           json.dumps({"scope_profile": [], "comparable_ids": [str(i)], "differences": []}),
                           f"logic-cap-{i}", db.now() + i))
            c.commit()
        d = self.deal(deal_id)
        self.assertEqual(len(d["steps"]), server.STATE_STEP_LIMIT)
        self.assertGreater(d["steps_total"], server.STATE_STEP_LIMIT)
        scoping = [o for o in d["agent_outputs"] if o["kind"] == "scoping"]
        self.assertEqual(len(scoping), 1)
        self.assertEqual(scoping[0]["payload"]["comparable_ids"], ["2"])
        self.assertEqual(d["agent_outputs_total"], 3)

    # 4 -- what a blocked draft must lose --------------------------------------
    def test_check_violations_are_stored_and_explained(self):
        deal_id, _, version_id = self.evaluated("logic-violation")
        _, text = self.post("/api/draft", {"version_id": version_id,
                                           "body": "Our price floor is S$2,454.90."})
        new_version = json.loads(text)["version_id"]
        _, text = self.post("/api/check", {"version_id": new_version})
        res = json.loads(text)
        self.assertFalse(res["ok"])
        self.assertIn("private number", res["message"])
        latest = self.deal(deal_id)["versions"][-1]
        codes = {v["code"] for v in latest["check_violations"]}
        self.assertIn("PRIVATE_NUMBER", codes)
        self.assertTrue(all(v["message"].startswith("The draft") for v in latest["check_violations"]))
        self.assertIn(self.deal(deal_id)["phase"], ('ready_for_review', 'counter_ready'))

    # 5 -- unpriced deals can't be checked or approved as if they had a draft ---
    def test_unpriced_deal_cannot_be_checked_or_approved(self):
        deal_id, _, version_id = self.evaluated("logic-unpriced", {"graphics": "custom_animation"})
        d = self.deal(deal_id)
        if d["versions"][-1]["options"]:
            self.skipTest("engine priced custom animation in this configuration")
        self.assertEqual(d["phase"], "needs_estimate")
        self.assertTrue(d["estimate_required"])
        _, text = self.post("/api/check", {"version_id": version_id})
        res = json.loads(text)
        self.assertFalse(res["ok"])
        self.assertEqual(res["result"]["violations"][0]["code"], "NO_PRICED_OPTIONS")
        self.assertIn("no price", res["message"])
        status, text = self.post("/api/approve", {"version_id": version_id})
        self.assertEqual(status, 409)
        self.assertIn("no price yet", json.loads(text)["message"])

    # 6 -- identical options merge ---------------------------------------------
    def test_identical_options_are_merged_keeping_the_first(self):
        same = {"deliverables": 3, "caption_versions": 3, "revision_rounds": 2,
                "deadline": "2026-12-25", "price_sgd": 2700.0, "phases": []}
        result = {"verdict": "counter", "options": [
            {"option_id": "hold_budget", "kind": "hold_budget", **same},
            {"option_id": "hold_scope", "kind": "hold_scope", **same},
            {"option_id": "phased", "kind": "phased", **dict(same, price_sgd=4000.0)}],
            "option_metrics": {"hold_budget": {}, "hold_scope": {}, "phased": {}}}
        merged = core.merge_identical_options(result)
        self.assertEqual([o["option_id"] for o in merged["options"]], ["hold_scope", "phased"])
        self.assertEqual(set(merged["option_metrics"]), {"hold_scope", "phased"})
        self.assertEqual(len(result["options"]), 3)          # input untouched

    # 7 + 8 + 11 -- shared labels, client dates, plurals ------------------------
    def test_labels_dates_and_plurals(self):
        self.assertEqual(server.option_label("hold_scope", {"deliverables": 5}), "Keep all five")
        self.assertEqual(server.option_label("hold_date"), "Keep the date")
        self.assertEqual(server.option_label("phased"), "Phased")
        self.assertEqual(deal_logic.client_date("2026-12-11", today=__import__("datetime").date(2026, 11, 22)), "11 Dec")
        self.assertEqual(deal_logic.client_date("2027-01-01", today=__import__("datetime").date(2026, 11, 22)), "1 Jan 2027")
        self.assertEqual(deal_logic.client_amount(2700.0), "S$2,700")
        self.assertEqual(deal_logic.plural(1, "video"), "1 video")
        option = {"option_id": "as_offered", "kind": "as_offered", "deliverables": 1,
                  "caption_versions": 1, "revision_rounds": 2, "deadline": "2026-12-11",
                  "price_sgd": 900.0, "phases": []}
        draft = core.draft_for({"verdict": "accept", "options": [option]}, "Juniper Cafe")
        self.assertIn("1 video by 11 Dec", draft)
        self.assertNotIn("2026-12-11", draft)
        self.assertTrue(core.run_outbound_check({"draft": draft, "approved_options": [option],
                                                 "private_values": {"floor_sgd": 800.0},
                                                 "other_client_names": []})["passed"])

    def test_new_drafts_use_client_dates_and_still_pass_the_check(self):
        deal_id, _, version_id = self.evaluated("logic-draft-date")
        draft = self.deal(deal_id)["versions"][-1]["draft"]
        self.assertNotIn("2026-12-11", draft)
        _, text = self.post("/api/check", {"version_id": version_id})
        self.assertTrue(json.loads(text)["ok"], text)

    # 10 -- test-deal flag and read-only listing -------------------------------
    def test_is_test_flag_created_date_and_listing_script(self):
        deal_id, _, _ = self.evaluated("eval20261123-logic-01")
        d = self.deal(deal_id)
        self.assertTrue(d["is_test"])
        self.assertIsNotNone(d["created_at"])
        self.assertFalse(deal_logic.is_test_deal("hashmeta", "LEAD-0001", "three promo videos"))
        out = subprocess.run([sys.executable, "-B", str(HERE / "scripts" / "list_test_deals.py"),
                              self.db_path, "--json"], capture_output=True, text=True, check=True)
        rows = json.loads(out.stdout)
        row = next(r for r in rows if r["deal_id"] == deal_id)
        self.assertTrue(row["is_test"])
        self.assertIn("phase", row)

    def test_automatic_refresh_is_quiet_after_first_refresh(self):
        deal_id, offer_id, _ = self.evaluated('logic-auto-refresh')
        with self.conn() as c:
            c.execute('UPDATE evaluations SET capacity_revision=-1 WHERE offer_id=?', (offer_id,))
            c.commit()
            self.assertEqual(len(core.refresh_stale_estimates(c)), 1)
            self.assertEqual(core.refresh_stale_estimates(c), [])

    def test_identical_agent_outputs_with_new_request_ids_are_quiet(self):
        deal_id, offer_id, _ = self.evaluated('logic-output-dedupe')
        mcp_server.DB_PATH = self.db_path
        args = {'deal_id': deal_id, 'offer_id': offer_id, 'kind': 'clarification',
                'payload': {'questions': ['What budget should we record?']}, 'request_id': 'dedupe-first'}
        first = mcp_server.t_save_agent_output(args)
        self.assertEqual(first, mcp_server.t_save_agent_output({**args, 'request_id': 'dedupe-second'}))
        with self.conn() as c:
            c.execute("UPDATE deals SET status='needs_information' WHERE id=?", (deal_id,))
            self.assertEqual(core.needs_information_of(c)['missing_fields'] if False else core.needs_information_of(c, deal_id)['missing_fields'], ['offer.budget_sgd'])

    def test_manual_estimate_validates_before_writing(self):
        deal_id, _, _ = self.evaluated('logic-manual-validation', {'graphics': 'custom_animation'})
        for hours, price in ((0, 1000), (-1, 1000), (float('nan'), 1000), (3, 0)):
            status, _ = self.post('/api/manual-estimate', {'deal_id': deal_id, 'hours_needed': hours, 'list_price': price})
            self.assertEqual(status, 400)
        status, _ = test_http.request(self.base, 'POST', '/api/manual-estimate', {'deal_id': deal_id, 'hours_needed': 3, 'list_price': 1000})
        self.assertIn(status, (403, 401))

    def test_manual_estimate_success_uses_production_validation(self):
        import estimating
        deal_id, _, _ = self.evaluated('logic-manual-success', {'graphics': 'custom_animation'})
        with self.conn() as c:
            estimating.save_rate_version(c, 'manual-test', [{'role': 'producer', 'cost_rate_cents': 4500, 'sell_rate_cents': 9000}])
            estimating.add_availability(c, 'producer', '2026-11-23T00:00:00', '2026-12-11T23:59:59')
            c.commit()
        status, text = self.post('/api/manual-estimate', {'deal_id': deal_id, 'hours_needed': 3, 'list_price': 1000})
        self.assertEqual(status, 200, text)
        d = self.deal(deal_id)
        self.assertEqual(d['versions'][-1]['estimate']['planning_hours'], 3)
        self.assertEqual(d['versions'][-1]['estimate']['list_sgd'], 1000)
        self.assertTrue(d['versions'][-1]['options'])
        self.assertEqual(d['versions'][-1]['outbound_status'], 'unchecked')

    # 12 -- client name must be a name ------------------------------------------
    def test_client_name_must_not_be_a_description(self):
        self.assertTrue(deal_logic.looks_like_description("SMU white coffee promo (inquiry chat)"))
        self.assertTrue(deal_logic.looks_like_description(""))
        for name in ("Juniper Cafe", "Northwind Bakery", "Glow Studio", "Rise Bakery"):
            self.assertFalse(deal_logic.looks_like_description(name), name)
        deal_id, _, _ = self.evaluated("logic-name")
        self.assertFalse(self.deal(deal_id)["client_name_needed"])


if __name__ == "__main__":
    unittest.main()
