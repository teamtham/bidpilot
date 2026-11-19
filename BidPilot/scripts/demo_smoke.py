#!/usr/bin/env python3
"""Replay fixture agent outputs over HTTP and MCP, not the QwenWork agents."""
import copy
import json
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import server

INQUIRY = {
    "as_of": "2026-11-23", "comparable_ids": ["P-031", "P-036", "P-041", "P-044"],
    "offer": {"client": "Juniper Cafe", "format": "product_promo",
              "production_mode": "one_shoot_day", "shoot_days": 1, "locations": 1,
              "interviewees": 0, "scripting": "light", "deliverables": 3,
              "editing_level": "standard", "graphics": "basic_titles", "revision_rounds": 2,
              "caption_versions": 3, "deadline": "2026-12-11", "budget_sgd": 2700,
              "direct_costs_sgd": 0}}
QUOTE = {"name_business": "Juniper Cafe", "reply_email": "sample@example.com",
         "project_brief": "Three 30-second Instagram promo videos for a cafe launch.",
         "video_format": "product_promo", "production_mode": "one_shoot_day",
         "shoot_days": 1, "locations": 1, "interviewees": 0, "scripting": "light",
         "deliverables": 3, "editing_level": "standard", "graphics": "basic_titles",
         "revision_rounds": 2, "caption_versions": 3, "caption_every_deliverable": True,
         "deadline": "2026-12-11", "budget_sgd": 2700, "travel_kit_talent": "None",
         "request_id": "fixture-quote"}


class MCP:
    def __init__(self, path):
        self.proc = subprocess.Popen([sys.executable, "-B", "-u", str(BASE / "mcp_server.py"), str(path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "fixture-smoke", "version": "1"}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server exited without a response")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError("MCP request failed: " + str(response["error"]))
        return response["result"]

    def call(self, name, args):
        result = self.request("tools/call", {"name": name, "arguments": args})
        if result.get("isError"):
            raise RuntimeError(name + ": " + result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()


def run():
    checks = []
    def check(label, condition):
        checks.append((label, bool(condition)))
        if not condition:
            raise AssertionError(label)

    print("BidPilot demo: fixture replay, not the QwenWork agents")
    with tempfile.TemporaryDirectory(prefix="bidpilot-smoke-") as tmp:
        http, owner = server.make_server(Path(tmp) / "smoke.db", port=0)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % http.server_address[1]
        client = None
        try:
            def request(path, data=None, authenticated=False):
                headers = {"Origin": base}
                if authenticated:
                    headers["X-Owner-Token"] = owner
                payload = None
                if data is not None:
                    payload = json.dumps(data).encode()
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(base + path, data=payload, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=10) as response:
                        body = response.read().decode()
                        return response.status, body
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read().decode()

            def post(path, data, authenticated=False):
                status, body = request(path, data, authenticated)
                if status >= 400:
                    raise RuntimeError("HTTP %d at %s" % (status, path.split("/p/")[0] or "/p/[redacted]"))
                return json.loads(body)

            quote = post("/quote", QUOTE)
            check("Quote received", quote.get("ok") and quote.get("source_id"))
            client = MCP(Path(tmp) / "smoke.db")
            pending = client.call("list_pending_work", {})
            check("Inquiry appears in pending work", any(x["source_id"] == quote["source_id"] for x in pending["inquiries"]))
            claim = client.call("claim_intake", {"source": "site", "source_id": quote["source_id"],
                "client": "Juniper Cafe", "raw_inquiry": QUOTE["project_brief"], "offer_request": INQUIRY})
            deal, offer = claim["deal_id"], claim["offer_id"]
            for kind, payload in (
                ("intake", {"offer": INQUIRY["offer"], "evidence": {"source": "fixture quote"},
                            "missing_fields": [], "clarification": None}),
                ("scoping", {"scope_profile": [], "comparable_ids": INQUIRY["comparable_ids"],
                             "differences": []}),
            ):
                client.call("save_agent_output", {"deal_id": deal, "offer_id": offer, "kind": kind,
                    "payload": payload, "request_id": "fixture-output-" + kind})
            for step in ("claimed", "intake", "scoping", "engine"):
                client.call("record_step", {"deal_id": deal, "offer_id": offer, "step_key": step,
                                            "status": "ok", "request_id": "fixture-step-" + step})
            ev = client.call("evaluate_offer", {"deal_id": deal, "offer_id": offer})
            est = ev["estimate"]
            check("Inquiry 1 verdict and pinned figures", ev["verdict"] == "accept" and
                  tuple(est[k] for k in ("planning_hours", "capacity_hours", "list_sgd", "floor_sgd", "effective_rate_sgd")) ==
                  (35.07, 50.40, 2700, 2454.90, 76.99))
            client.call("save_agent_output", {"deal_id": deal, "offer_id": offer, "kind": "negotiator",
                "payload": {"draft": "Thanks Juniper Cafe. We can deliver 3 videos by 2026-12-11 for S$2,700.",
                            "option_ids": ["as_offered"]}, "request_id": "fixture-output-negotiator"})
            saved = client.call("save_draft", {"version_id": ev["draft_version"]["version_id"],
                "draft_body": "Thanks Juniper Cafe. We can deliver 3 videos by 2026-12-11 for S$2,700.",
                "request_id": "fixture-original-draft"})
            version = saved["draft_version"]["version_id"]
            checked = client.call("check_draft", {"version_id": version})
            check("Original draft passes outbound check", checked["outcome"]["passed"])
            for step in ("draft", "outbound", "review"):
                client.call("record_step", {"deal_id": deal, "offer_id": offer, "step_key": step,
                                            "status": "ok", "request_id": "fixture-step-" + step})
            approval = post("/api/approve", {"version_id": version}, True)
            check("Founder approves original proposal", approval["ok"])
            token = approval["token"]
            status, _ = request("/p/" + token)
            check("Client opens original proposal", status == 200)
            change = post("/p/" + token + "/request-change", {"body": "Five videos by 4 Dec, same budget; please caption all five.",
                                                               "request_id": "fixture-change"})
            check("Client change queued", change["status"] == "clarify")
            updated = copy.deepcopy(INQUIRY)
            updated["offer"].update(deliverables=5, caption_versions=5, deadline="2026-12-04")
            submitted = client.call("submit_change_offer", {"deal_id": deal, "request_id": "fixture-change",
                "version_id": version, "offer_request": updated,
                "evidence": {"raw_message": "Five videos by 4 Dec, same budget; please caption all five."}})
            counter = client.call("evaluate_offer", {"deal_id": deal, "offer_id": submitted["offer_id"]})
            est = counter["estimate"]
            check("Counter verdict and pinned figures", counter["verdict"] == "counter" and
                  tuple(est[k] for k in ("planning_hours", "capacity_hours", "list_sgd", "floor_sgd", "effective_rate_sgd")) ==
                  (52.32, 28.80, 4000, 3662.40, 51.61))
            opts = {o["option_id"]: o for o in counter["options"]}
            check("Counter option terms", set(opts) == {"hold_date", "hold_budget", "hold_scope", "phased"} and
                  [(opts[k]["deliverables"], opts[k]["deadline"], opts[k]["price_sgd"]) for k in
                   ("hold_date", "hold_budget", "hold_scope")] ==
                  [(2, "2026-12-04", 2050), (3, "2026-12-11", 2700), (5, "2026-12-18", 4000)] and
                  opts["phased"]["price_sgd"] == 4000 and opts["phased"]["phases"] ==
                  [{"deliverables": 2, "deadline": "2026-12-04"}, {"deliverables": 3, "deadline": "2026-12-18"}])
            counter_version = counter["draft_version"]["version_id"]
            _, state_body = request("/api/state", authenticated=True)
            state = json.loads(state_body)
            deal_state = next(d for d in state["deals"] if d["deal_id"] == deal)
            view = next(v for v in deal_state["versions"] if v["version_id"] == counter_version)
            metric = view["option_metrics"]["hold_date"]
            check("Two-video option private metrics", (metric["planning_hours"], metric["floor_sgd"]) == (26.45, 1851.50))
            blocked = client.call("save_draft", {"version_id": counter_version,
                "draft_body": "Our price floor is S$3,662.40.", "request_id": "fixture-blocked"})
            blocked_id = blocked["draft_version"]["version_id"]
            result = client.call("check_draft", {"version_id": blocked_id})
            violations = json.dumps(result["outcome"]["violations"])
            check("Private floor draft blocked", not result["outcome"]["passed"] and
                  ("PRIVATE_NUMBER" in violations or "PRIVATE_LABEL" in violations))
            status, _ = request("/api/approve", {"version_id": blocked_id}, True)
            check("Blocked draft cannot be approved", status == 409)
            clean = client.call("save_draft", {"version_id": blocked_id,
                "draft_body": "Thank you. Please review the available options below.", "request_id": "fixture-clean"})
            version2 = clean["draft_version"]["version_id"]
            check("Clean counter draft passes", client.call("check_draft", {"version_id": version2})["outcome"]["passed"])
            released = post("/api/approve", {"version_id": version2}, True)
            check("Founder approves counter", released["ok"])
            _, state_body = request("/api/state", authenticated=True)
            deal_state = next(d for d in json.loads(state_body)["deals"] if d["deal_id"] == deal)
            view = next(v for v in deal_state["versions"] if v["version_id"] == version2)
            phased = next(o["option_row_id"] for o in view["option_meta"] if o["option_id"] == "phased")
            accepted = post("/p/" + released["token"] + "/accept", {"option_id": phased})
            check("Client accepts phased option", accepted["ok"])
            confirmed = post("/api/confirm-booking", {"version_id": version2}, True)
            check("Founder confirms phased booking", confirmed["ok"] and confirmed["option_id"] == phased)
        except Exception as exc:
            if not checks or checks[-1][1]:
                checks.append(("Unexpected error: " + str(exc), False))
        finally:
            if client is not None:
                client.close()
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)
    print("\nResult  Check")
    print("------  -----")
    for label, passed in checks:
        print("%-6s  %s" % ("PASS" if passed else "FAIL", label))
    return 0 if checks and all(passed for _, passed in checks) else 1


if __name__ == "__main__":
    sys.exit(run())
