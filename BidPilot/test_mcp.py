"""Stage 8: real JSON-RPC tests for the scoped BidPilot STDIO MCP server.

Spawns mcp_server.py as a subprocess against an isolated temp database and
exercises initialize / tools/list / tools/call over newline-delimited JSON.

Run: python3 -B -m unittest -v test_mcp
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import core  # noqa: E402
import db  # noqa: E402

INQUIRY = {
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

TOOL_NAMES = ["claim_intake", "get_deal", "evaluate_offer", "save_draft",
              "check_draft", "run_trace", "negotiation_payload"]


class Client:
    def __init__(self, db_path):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(BASE / "mcp_server.py"), db_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def request(self, method, params=None, msg_id=None):
        msg = {"jsonrpc": "2.0", "id": msg_id if msg_id is not None else 1,
               "method": method}
        if params is not None:
            msg["params"] = params
        return self.roundtrip(msg)

    def roundtrip(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        return json.loads(line)

    def notify(self, method):
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name, arguments, msg_id=None):
        return self.request("tools/call", {"name": name, "arguments": arguments},
                            msg_id)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def tool_result(resp):
    """Unwrap a tools/call response into the parsed result payload."""
    assert "error" not in resp, resp
    result = resp["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def tool_error(resp):
    """Return the error text of a failed tool call."""
    assert "error" not in resp, resp
    assert resp["result"].get("isError"), resp
    return resp["result"]["content"][0]["text"]


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bidpilot-mcp-")
        self.db_path = str(Path(self.tmp) / "mcp.db")
        conn = db.connect(self.db_path)
        core.load_seed(conn)
        conn.close()
        self.client = Client(self.db_path)

    def tearDown(self):
        self.client.close()

    def initialize(self):
        resp = self.client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "bidpilot")
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.client.notify("notifications/initialized")

    def full_flow(self, source_id="mcp-1", draft=None):
        claim = tool_result(self.client.call_tool("claim_intake", {
            "source": "hashmeta", "source_id": source_id,
            "client": INQUIRY["offer"]["client"],
            "raw_inquiry": "sample inquiry one", "offer_request": INQUIRY}))
        evaluation = tool_result(self.client.call_tool("evaluate_offer", {
            "deal_id": claim["deal_id"], "offer_id": claim["offer_id"]}))
        version_id = evaluation["draft_version"]["version_id"]
        if draft:
            saved = tool_result(self.client.call_tool("save_draft", {
                "version_id": version_id, "draft_body": draft,
                "request_id": f"save-{source_id}"}))
            version_id = saved["draft_version"]["version_id"]
        return claim, evaluation, version_id

    # -- protocol -----------------------------------------------------------
    def test_initialize_and_tools_list(self):
        self.initialize()
        resp = self.client.request("tools/list")
        tools = resp["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], TOOL_NAMES)
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
        text = json.dumps(tools)
        for banned in ("approve", "release", "rotate", "booking", "confirm",
                       "sql", "shell", "exec", "file"):
            self.assertNotIn(banned, text.lower())

    def test_unknown_tool_rejected(self):
        self.initialize()
        resp = self.client.call_tool("approve", {"version_id": "x"})
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn("not exposed", resp["error"]["message"])
        resp = self.client.call_tool("confirm_booking", {})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_method(self):
        resp = self.client.request("resources/list")
        self.assertEqual(resp["error"]["code"], -32601)

    # -- full scoped workflow ------------------------------------------------
    def test_full_flow_engine_numbers_and_check(self):
        self.initialize()
        claim, evaluation, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — we can deliver 3 videos by 2026-12-11"
                  " for S$2,700.")
        self.assertTrue(claim["created"])
        est = evaluation["estimate"]
        self.assertEqual(est["planning_hours"], 35.07)
        self.assertEqual(est["capacity_hours"], 50.40)
        self.assertEqual(est["list_sgd"], 2700.00)
        self.assertEqual(est["floor_sgd"], 2454.90)
        self.assertEqual(evaluation["verdict"], "accept")
        # engine options are the exact public terms
        self.assertGreaterEqual(len(evaluation["options"]), 1)
        # duplicate claim resumes without a second offer
        again = tool_result(self.client.call_tool("claim_intake", {
            "source": "hashmeta", "source_id": "mcp-1",
            "client": INQUIRY["offer"]["client"],
            "raw_inquiry": "retry copy", "offer_request": INQUIRY}))
        self.assertEqual(again["deal_id"], claim["deal_id"])
        self.assertEqual(again["offer_id"], claim["offer_id"])
        self.assertFalse(again["created"])
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM offers").fetchone()[0], 1)
        conn.close()
        # outbound check passes on the clean generated draft
        checked = tool_result(self.client.call_tool("check_draft", {
            "version_id": version_id}))
        self.assertTrue(checked["outcome"]["passed"], checked)
        self.assertEqual(checked["version"]["outbound_status"], "passed")
        # idempotent re-evaluate: no second evaluation or version
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM evaluations").fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM proposal_versions").fetchone()[0], 2)
        conn.close()
        rerun = tool_result(self.client.call_tool("evaluate_offer", {
            "deal_id": claim["deal_id"], "offer_id": claim["offer_id"]}))
        self.assertEqual(rerun["evaluation_id"], evaluation["evaluation_id"])
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM evaluations").fetchone()[0], 1)
        conn.close()

    def test_run_trace_records_stages(self):
        self.initialize()
        claim, evaluation, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        self.client.call_tool("check_draft", {"version_id": version_id})
        trace = tool_result(self.client.call_tool("run_trace", {
            "deal_id": claim["deal_id"]}))
        keys = [s["step_key"] for s in trace["steps"]]
        for expected in ("intake", "engine_evaluate", "edit_draft",
                         "outbound_check"):
            self.assertIn(expected, keys)
        self.assertEqual(trace["deal"]["client"], "Juniper Cafe")
        self.assertEqual(len(trace["versions"]), 2)
        text = json.dumps(trace)
        self.assertNotIn("token_hash", text)
        self.assertNotIn("token", text)

    # -- validation and ownership --------------------------------------------
    def test_invalid_intake_rejected(self):
        self.initialize()
        bad = json.loads(json.dumps(INQUIRY))
        bad["offer"]["deliverables"] = -1
        err = tool_error(self.client.call_tool("claim_intake", {
            "source": "hashmeta", "source_id": "bad-1",
            "client": "X", "raw_inquiry": "raw", "offer_request": bad}))
        self.assertIn("engine_validation_failed", err)

    def test_missing_intake_fields_rejected(self):
        self.initialize()
        err = tool_error(self.client.call_tool("claim_intake", {
            "source": "hashmeta", "source_id": "bad-2"}))
        self.assertIn("missing_field", err)

    def test_disallowed_source_rejected(self):
        self.initialize()
        err = tool_error(self.client.call_tool("claim_intake", {
            "source": "crm", "source_id": "s", "client": "c",
            "raw_inquiry": "r", "offer_request": INQUIRY}))
        self.assertIn("source_not_allowed", err)

    def test_non_sample_record_rejected(self):
        self.initialize()
        conn = db.connect(self.db_path)
        conn.execute(
            "INSERT INTO deals (id, source, source_id, client, raw_inquiry,"
            " status, current_version_id, sample_data, created_at)"
            " VALUES ('real-1','crm','r1','Real Client','x','new',NULL,0,0)")
        conn.commit()
        conn.close()
        for args in ({"deal_id": "real-1"},
                     {"deal_id": "real-1", "offer_id": "whatever"}):
            err = tool_error(self.client.call_tool("get_deal", args))
            self.assertIn("non_sample_record_rejected", err)
        err = tool_error(self.client.call_tool("run_trace", {"deal_id": "real-1"}))
        self.assertIn("non_sample_record_rejected", err)

    def test_unknown_ids_rejected(self):
        self.initialize()
        self.assertIn("deal_not_found", tool_error(
            self.client.call_tool("get_deal", {"deal_id": "nope"})))
        self.assertIn("version_not_found", tool_error(
            self.client.call_tool("check_draft", {"version_id": "nope"})))

    def test_offer_ownership_mismatch(self):
        self.initialize()
        claim_a, _, _ = self.full_flow(source_id="own-a")
        claim_b, _, _ = self.full_flow(source_id="own-b")
        err = tool_error(self.client.call_tool("evaluate_offer", {
            "deal_id": claim_a["deal_id"], "offer_id": claim_b["offer_id"]}))
        self.assertIn("offer_ownership_mismatch", err)

    # -- edit invalidates check; MCP cannot approve ---------------------------
    def test_edit_invalidates_prior_check(self):
        self.initialize()
        claim, evaluation, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        checked = tool_result(self.client.call_tool("check_draft", {
            "version_id": version_id}))
        self.assertTrue(checked["outcome"]["passed"])
        # edit -> new unchecked version
        saved = tool_result(self.client.call_tool("save_draft", {
            "version_id": version_id, "draft_body": "Edited: fewer videos.",
            "request_id": "save-edit-1"}))
        new_id = saved["draft_version"]["version_id"]
        self.assertNotEqual(new_id, version_id)
        self.assertEqual(saved["draft_version"]["outbound_status"], "unchecked")
        # old version no longer releasable: approve (dashboard-side) fails
        conn = db.connect(self.db_path)
        res = core.approve(conn, version_id)
        conn.close()
        self.assertFalse(res["ok"])
        self.assertIn("not_latest_version", res["reasons"])

    def test_private_draft_blocked_by_outbound_check(self):
        self.initialize()
        claim, evaluation, version_id = self.full_flow(
            draft="Our floor is S$2,454.90 and internal planning is 35.07h,"
                  " minimum rate S$70.")
        checked = tool_result(self.client.call_tool("check_draft", {
            "version_id": version_id}))
        self.assertFalse(checked["outcome"]["passed"], checked)
        self.assertTrue(checked["outcome"]["violations"])
        self.assertEqual(checked["version"]["outbound_status"], "blocked")

    def test_version_not_published_via_mcp(self):
        self.initialize()
        claim, evaluation, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        self.client.call_tool("check_draft", {"version_id": version_id})
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT status FROM proposal_versions WHERE id=?",
            (version_id,)).fetchone()[0], "checked")
        self.assertIsNone(conn.execute(
            "SELECT token_hash FROM proposal_versions WHERE id=?",
            (version_id,)).fetchone()[0])
        conn.close()

    def test_public_projection_has_no_tokens_or_other_clients(self):
        self.initialize()
        claim, _, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        deal = tool_result(self.client.call_tool("get_deal", {
            "deal_id": claim["deal_id"]}))
        text = json.dumps(deal)
        self.assertNotIn("token", text)
        for row in db.connect(self.db_path).execute(
                "SELECT DISTINCT client FROM seed_past_projects").fetchall():
            if row["client"] != "Juniper Cafe":
                self.assertNotIn(row["client"], text)

    # -- concurrency ----------------------------------------------------------
    def test_concurrent_duplicate_claims_single_deal(self):
        clients = [Client(self.db_path) for _ in range(2)]
        for c in clients:
            c.request("initialize", {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}})
        results = []
        barrier = threading.Barrier(2)

        def worker(c):
            barrier.wait()
            results.append(c.call_tool("claim_intake", {
                "source": "hashmeta", "source_id": "conc-1",
                "client": INQUIRY["offer"]["client"],
                "raw_inquiry": "concurrent", "offer_request": INQUIRY}))

        threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for c in clients:
            c.close()
        payloads = [tool_result(r) for r in results]
        self.assertEqual(payloads[0]["deal_id"], payloads[1]["deal_id"])
        self.assertEqual(payloads[0]["offer_id"], payloads[1]["offer_id"])
        self.assertTrue(payloads[0]["created"] ^ payloads[1]["created"])
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM offers").fetchone()[0], 1)
        conn.close()

    # -- stage 10: sample-data collision on claim -----------------------------
    def _counts(self):
        conn = db.connect(self.db_path)
        out = {
            "deals": conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0],
            "offers": conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0],
            "steps": conn.execute("SELECT COUNT(*) FROM run_steps").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "versions": conn.execute(
                "SELECT COUNT(*) FROM proposal_versions").fetchone()[0],
        }
        conn.close()
        return out

    def _insert_non_sample_deal(self, with_offer=False):
        conn = db.connect(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO deals (id, source, source_id, client,"
            " raw_inquiry, status, current_version_id, sample_data, created_at)"
            " VALUES ('real-2','hashmeta','real-key','Real Client','x','new',NULL,0,0)")
        if with_offer:
            conn.execute(
                "INSERT INTO offers (id, deal_id, source, source_id,"
                " from_version_id, round, as_of, offer_json, evidence_json,"
                " comparable_ids_json, created_at)"
                " VALUES ('real-offer','real-2','hashmeta','real-key',NULL,1,"
                " '2026-11-23', '{}', '{}', '[]', 0)")
        conn.commit()
        conn.close()

    def test_claim_colliding_with_non_sample_deal_rejected(self):
        self.initialize()
        for with_offer in (False, True):
            with self.subTest(with_offer=with_offer):
                self._insert_non_sample_deal(with_offer=with_offer)
                before = self._counts()
                err = tool_error(self.client.call_tool("claim_intake", {
                    "source": "hashmeta", "source_id": "real-key",
                    "client": INQUIRY["offer"]["client"],
                    "raw_inquiry": "colliding claim", "offer_request": INQUIRY}))
                self.assertIn("non_sample_record_rejected", err)
                # rejected call: non-sample rows and all counts unchanged
                self.assertEqual(self._counts(), before)

    def test_concurrent_claims_on_non_sample_deal_all_rejected(self):
        self.initialize()
        self._insert_non_sample_deal()
        before = self._counts()
        clients = [Client(self.db_path) for _ in range(2)]
        for c in clients:
            c.request("initialize", {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}})
        results = []
        errs = []
        barrier = threading.Barrier(2)

        def worker(c):
            barrier.wait()
            resp = c.call_tool("claim_intake", {
                "source": "hashmeta", "source_id": "real-key",
                "client": INQUIRY["offer"]["client"],
                "raw_inquiry": "concurrent", "offer_request": INQUIRY})
            if resp["result"].get("isError"):
                errs.append(resp["result"]["content"][0]["text"])
            else:
                results.append(resp)

        threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for c in clients:
            c.close()
        self.assertEqual(len(results), 0)
        self.assertEqual(len(errs), 2)
        for e in errs:
            self.assertIn("non_sample_record_rejected", e)
        self.assertEqual(self._counts(), before)

    # -- stage 10: save_draft exactly-once -------------------------------------
    def _save(self, version_id, body, request_id, client=None):
        return (client or self.client).call_tool("save_draft", {
            "version_id": version_id, "draft_body": body,
            "request_id": request_id})

    def test_save_draft_identical_retry_returns_original(self):
        self.initialize()
        claim, _, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        first = tool_result(self._save(version_id, "Edit A.", "sd-idem-1"))
        second = tool_result(self._save(version_id, "Edit A.", "sd-idem-1"))
        self.assertEqual(first, second)
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM proposal_versions WHERE deal_id=?",
            (claim["deal_id"],)).fetchone()[0], 3)  # engine draft + 1 edit
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM mcp_operations").fetchone()[0], 2)
        conn.close()

    def test_save_draft_request_id_conflict_rejected(self):
        self.initialize()
        claim, _, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        tool_result(self._save(version_id, "Edit A.", "sd-conflict-1"))
        # same id, different payload -> reject, no new version or op record
        err = tool_error(self._save(version_id, "Different text.", "sd-conflict-1"))
        self.assertIn("request_id_conflict", err)
        # same id, same payload -> still the original result
        again = tool_result(self._save(version_id, "Edit A.", "sd-conflict-1"))
        self.assertIn("Edit A.", again["draft_version"]["draft_body"])
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM proposal_versions WHERE deal_id=?",
            (claim["deal_id"],)).fetchone()[0], 3)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM mcp_operations").fetchone()[0], 2)
        conn.close()

    def test_save_draft_concurrent_retries_create_one_version(self):
        self.initialize()
        claim, _, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        clients = [Client(self.db_path) for _ in range(2)]
        for c in clients:
            c.request("initialize", {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "0"}})
        results = []
        barrier = threading.Barrier(2)

        def worker(c):
            barrier.wait()
            results.append(self._save(version_id, "Concurrent edit.", "sd-conc-1", c))

        threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for c in clients:
            c.close()
        payloads = [tool_result(r) for r in results]
        self.assertEqual(payloads[0], payloads[1])
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM proposal_versions WHERE deal_id=?",
            (claim["deal_id"],)).fetchone()[0], 3)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM mcp_operations").fetchone()[0], 2)
        conn.close()

    def test_save_draft_requires_request_id(self):
        self.initialize()
        claim, _, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        err = tool_error(self.client.call_tool("save_draft", {
            "version_id": version_id, "draft_body": "x"}))
        self.assertIn("missing_field:request_id", err)

    def test_save_draft_published_version_not_editable(self):
        self.initialize()
        claim, _, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        tool_result(self.client.call_tool("check_draft", {"version_id": version_id}))
        conn = db.connect(self.db_path)
        res = core.approve(conn, version_id)
        conn.close()
        self.assertTrue(res["ok"], res)
        err = tool_error(self._save(version_id, "late edit.", "sd-late-1"))
        self.assertIn("record_not_editable", err)
        conn = db.connect(self.db_path)
        self.assertEqual(conn.execute(
            "SELECT status FROM proposal_versions WHERE id=?",
            (version_id,)).fetchone()[0], "published")
        conn.close()

    # -- stage 10: negotiation_payload ------------------------------------------
    def test_negotiation_payload_outward_safe_only(self):
        self.initialize()
        claim, evaluation, version_id = self.full_flow(
            draft="Thanks Juniper Cafe — 3 videos by 2026-12-11, S$2,700.")
        # no draft passed the check yet: terms only
        payload = tool_result(self.client.call_tool("negotiation_payload", {
            "evaluation_id": evaluation["evaluation_id"]}))
        self.assertEqual(payload["options"], evaluation["options"])
        self.assertIsNone(payload["client_message"])
        self.assertNotIn("floor", json.dumps(payload))
        tool_result(self.client.call_tool("check_draft", {"version_id": version_id}))
        payload = tool_result(self.client.call_tool("negotiation_payload", {
            "evaluation_id": evaluation["evaluation_id"]}))
        self.assertIn("S$2,700", payload["client_message"])
        text = json.dumps(payload)
        for banned in ("floor_sgd", "capacity_hours", "planning_hours",
                       "min_hourly", "option_metrics", "run_steps",
                       "estimate", "private_note"):
            self.assertNotIn(banned, text)
        self.assertNotIn("token", text)

    def test_negotiation_payload_unknown_and_non_sample_rejected(self):
        self.initialize()
        err = tool_error(self.client.call_tool("negotiation_payload", {
            "evaluation_id": "nope"}))
        self.assertIn("evaluation_not_found", err)
        conn = db.connect(self.db_path)
        conn.execute(
            "INSERT INTO deals (id, source, source_id, client, raw_inquiry,"
            " status, current_version_id, sample_data, created_at)"
            " VALUES ('real-3','crm','r3','Real Client','x','new',NULL,0,0)")
        conn.execute(
            "INSERT INTO offers (id, deal_id, source, source_id, from_version_id,"
            " round, as_of, offer_json, evidence_json, comparable_ids_json,"
            " created_at) VALUES ('ro-1','real-3','crm','r3',NULL,1,'2026-11-23',"
            " '{}','{}','[]',0)")
        conn.execute(
            "INSERT INTO evaluations (id, offer_id, verdict, reason_codes_json,"
            " result_json, engine_version, created_at) VALUES"
            " ('ev-real','ro-1','accept','[]','{}','e',0)")
        conn.commit()
        conn.close()
        err = tool_error(self.client.call_tool("negotiation_payload", {
            "evaluation_id": "ev-real"}))
        self.assertIn("non_sample_record_rejected", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
