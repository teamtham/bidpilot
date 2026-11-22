"""Expanded in-process lifecycle journeys.

These tests intentionally use ``test_studio_journey.Journey``'s socket-free
HTTP transport.  They cover seams that are easy to miss in narrower unit
tests: capacity collision simulation versus the confirmation-time audit,
structured creative requests, confirmed-booking amendments and closeout
evidence, stale inquiry turns, and private insight boundaries.
"""
import copy
import html
import json
import re
import shutil
import subprocess

import db

from test_http import INQUIRY, RAW1
from test_studio_journey import Journey


class ExpandedLifecycle(Journey):
    def owner_json(self, method, path, body=None):
        status, raw = self.http(method, path, body, owner=True)
        return status, json.loads(raw)

    def public_json(self, method, path, body=None):
        status, raw = self.http(method, path, body)
        return status, json.loads(raw)

    def mcp_error(self, name, args):
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": name, "arguments": args}}
        response = __import__("mcp_server").handle(msg)
        if "error" in response:
            return response["error"]["message"]
        result = response["result"]
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def publish(self, source_id, client="Juniper Cafe", request=None):
        request = copy.deepcopy(request or INQUIRY)
        request["offer"]["client"] = client
        status, imported = self.owner_json("POST", "/api/import", {
            "source": "site", "source_id": source_id,
            "raw_inquiry": RAW1, "offer_request": request,
        })
        self.assertEqual(status, 200, imported)
        status, evaluated = self.owner_json("POST", "/api/evaluate", {
            "deal_id": imported["deal_id"], "offer_id": imported["offer_id"],
        })
        self.assertEqual(status, 200, evaluated)
        version = evaluated["version_id"]
        status, checked = self.owner_json("POST", "/api/check", {"version_id": version})
        self.assertEqual(status, 200, checked)
        self.assertTrue(checked["ok"], checked)
        status, released = self.owner_json("POST", "/api/approve", {"version_id": version})
        self.assertEqual(status, 200, released)
        self.assertTrue(released["ok"], released)
        conn = db.connect(self.path)
        option_ids = json.loads(conn.execute(
            "SELECT public_option_ids_json FROM proposal_versions WHERE id=?",
            (version,)).fetchone()[0])
        conn.close()
        return {**imported, **evaluated, **released, "version_id": version,
                "option_ids": option_ids, "url": released["url"]}

    def accept_and_confirm(self, flow, option_id=None):
        option_id = option_id or flow["option_ids"][0]
        status, accepted = self.public_json(
            "POST", flow["url"] + "/accept", {"option_id": option_id})
        self.assertEqual(status, 200, accepted)
        self.assertTrue(accepted["ok"], accepted)
        status, confirmed = self.owner_json(
            "POST", "/api/confirm-booking", {"version_id": flow["version_id"]})
        self.assertEqual(status, 200, confirmed)
        self.assertTrue(confirmed["ok"], confirmed)
        return confirmed

    def test_capacity_collision_preview_matches_confirmation_impact(self):
        first = self.publish("collision-a", "Aster Cafe")
        second = self.publish("collision-b", "Birch Cafe")

        status, preview = self.owner_json("POST", "/api/capacity-preview", {
            "version_id": first["version_id"], "option_id": first["option_ids"][0],
        })
        self.assertEqual(status, 200, preview)
        self.assertTrue(preview["fits"], preview)
        affected = next(item for item in preview["affected"]
                        if item["version_id"] == second["version_id"])
        self.assertEqual(affected["client"], "Birch Cafe")
        self.assertTrue(affected["lost"])
        self.assertEqual(affected["remaining"], 0)

        self.accept_and_confirm(first)
        conn = db.connect(self.path)
        actual = json.loads(conn.execute(
            "SELECT details FROM capacity_impacts WHERE version_id=?",
            (second["version_id"],)).fetchone()[0])
        conn.close()
        self.assertEqual(actual, affected)

        status, raw = self.http("GET", "/api/state", owner=True)
        self.assertEqual(status, 200, raw)
        state = json.loads(raw)
        version = next(v for deal in state["deals"] if deal["deal_id"] == second["deal_id"]
                       for v in deal["versions"] if v["version_id"] == second["version_id"])
        self.assertEqual(version["impacts"], [affected])

    def test_proposal_swap_and_add_are_structured_review_requests(self):
        flow = self.publish("creative-requests", "Creative Cafe")
        status, page = self.http("GET", flow["url"])
        self.assertEqual(status, 200, page)
        # The studio-assistant change chat replaced the "Explore studio work"
        # library on the client page; the structured routes stay available.
        self.assertNotIn(b"Explore studio work", page)
        self.assertIn(b'id="cc-form"', page)
        self.assertIn(b'id="rc-count" type="number" min="1" value="3"', page)
        self.assertIn(b'id="rc-captions" type="number" min="0" value="3"', page)
        self.assertIn(b'id="rc-deadline" type="date" value="2026-12-11"', page)
        self.assertIn(b'id="rc-budget" type="number" step="0.01" value="2700', page)

        requests = [
            {"request_id": "swap-1", "option_id": flow["option_ids"][0],
             "target_deliverable": 1, "asset_id": "cafe", "action": "swap"},
            {"request_id": "add-1", "option_id": flow["option_ids"][0],
             "target_deliverable": 2, "asset_id": "bakery", "action": "add"},
        ]
        for payload in requests:
            status, result = self.public_json(
                "POST", flow["url"] + "/creative-change", payload)
            self.assertEqual(status, 202, result)
            self.assertTrue(result["ok"], result)

        # An exact retry is idempotent; reusing the key for different content is not.
        status, result = self.public_json(
            "POST", flow["url"] + "/creative-change", requests[0])
        self.assertEqual(status, 202, result)
        conflict = dict(requests[0], action="add")
        status, result = self.public_json(
            "POST", flow["url"] + "/creative-change", conflict)
        self.assertEqual(status, 409, result)
        self.assertEqual(result["error"], "request_id_conflict")

        conn = db.connect(self.path)
        saved = conn.execute(
            "SELECT request_id,option_id,reference_json FROM commerce_reference_requests "
            "WHERE request_id LIKE 'creative:%' ORDER BY request_id").fetchall()
        messages = conn.execute(
            "SELECT request_id,version_id,option_ids_json,body FROM messages "
            "WHERE request_id LIKE 'creative:%' ORDER BY request_id").fetchall()
        conn.close()
        self.assertEqual(len(saved), 2)
        self.assertEqual({json.loads(row["reference_json"])["action"] for row in saved},
                         {"swap", "add"})
        self.assertTrue(all(row["option_id"] == flow["option_ids"][0] for row in saved))
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(row["version_id"] == flow["version_id"] for row in messages))
        self.assertTrue(all(json.loads(row["option_ids_json"]) == [flow["option_ids"][0]]
                            for row in messages))

    def test_amendment_accept_confirm_closeout_feeds_next_estimate(self):
        flow = self.publish("amendment-base", "Juniper Cafe")
        self.accept_and_confirm(flow)

        status, requested = self.public_json(
            "POST", flow["url"] + "/additional-work",
            {"body": "Please add two revision rounds.", "request_id": "revise-twice"})
        self.assertEqual(status, 200, requested)
        pending = self.rpc("list_pending_work", {})["additional_work"]
        self.assertEqual(pending[0]["request_id"], "additional:revise-twice")

        order = self.rpc("price_additional_work", {
            "version_id": flow["version_id"], "kind": "extra_revision_round",
            "quantity": 2, "as_of": "2026-11-23", "deadline": "2026-12-11",
            "request_id": "revise-twice",
        })
        status, approved = self.owner_json("POST", "/api/commerce/approve", {
            "order_id": order["order_id"], "digest": order["digest"],
        })
        self.assertEqual(status, 200, approved)
        status, accepted = self.public_json(
            "POST", flow["url"] + "/change-order/" + order["order_id"] + "/accept", {})
        self.assertEqual(status, 200, accepted)
        self.assertTrue(accepted["pending"], accepted)
        status, confirmed = self.owner_json("POST", "/api/commerce/confirm", {
            "order_id": order["order_id"],
        })
        self.assertEqual(status, 200, confirmed)
        self.assertIn("reservation_id", confirmed)

        status, booking_page = self.http("GET", flow["url"])
        self.assertEqual(status, 200, booking_page)
        booking_page = booking_page.decode()
        self.assertIn("Original selection: S$2,700.00 by 2026-12-11", booking_page)
        self.assertIn("Current committed total: S$3,100.00", booking_page)
        self.assertIn("Committed total after this amendment: S$3,100.00", booking_page)
        self.assertIn("Booking confirmed", booking_page)
        self.assertNotIn("Accepted — awaiting Alex", booking_page)
        self.assertIn('onsubmit="requestMore(event); return false"', booking_page)
        self.assertNotIn("price floor", booking_page.lower())
        self.assertNotIn("capacity", booking_page.lower())

        next_order = self.rpc("price_additional_work", {
            "version_id": flow["version_id"], "base_order_id": order["order_id"],
            "kind": "extra_location", "quantity": 1,
            "as_of": "2026-11-23", "deadline": "2026-12-18",
        })
        status, approved = self.owner_json("POST", "/api/commerce/approve", {
            "order_id": next_order["order_id"], "digest": next_order["digest"],
        })
        self.assertEqual(status, 200, approved)
        status, revised_page = self.http("GET", flow["url"])
        self.assertEqual(status, 200, revised_page)
        revised_page = revised_page.decode()
        self.assertIn("Revised total if accepted: S$3,400.00", revised_page)
        self.assertIn("revised delivery date: 2026-12-18", revised_page)
        # Leave no pending amendment behind for the closeout portion below.
        conn = db.connect(self.path)
        conn.execute("DELETE FROM change_orders WHERE id=?", (next_order["order_id"],))
        conn.commit()
        conn.close()

        status, closed = self.owner_json("POST", "/api/commerce/closeout", {
            "version_id": flow["version_id"],
            "actuals": {"actual_hours": 40, "actual_cost_sgd": 150,
                        "end": "2026-12-12", "notes": "Producer verified",
                        "reliable": True},
        })
        self.assertEqual(status, 200, closed)

        next_request = copy.deepcopy(INQUIRY)
        next_request['pricing_mode'] = 'historical_replay'
        next_request["as_of"] = "2026-12-14"
        next_request["comparable_ids"] = []
        next_request["offer"].update(
            client="Next Cafe", revision_rounds=4, deadline="2027-01-08",
            budget_sgd=4000)
        status, imported = self.owner_json("POST", "/api/import", {
            "source": "site", "source_id": "after-closeout",
            "raw_inquiry": "A later matching brief", "offer_request": next_request,
        })
        self.assertEqual(status, 200, imported)
        status, evaluated = self.owner_json("POST", "/api/evaluate", {
            "deal_id": imported["deal_id"], "offer_id": imported["offer_id"],
        })
        self.assertEqual(status, 200, evaluated)
        conn = db.connect(self.path)
        result = json.loads(conn.execute(
            "SELECT result_json FROM evaluations WHERE offer_id=?",
            (imported["offer_id"],)).fetchone()[0])
        conn.close()
        pricing = result["historical_pricing"]
        self.assertEqual(pricing["status"], "applied", pricing)
        closeout_samples = [sample for sample in pricing["samples"]
                            if sample["project_id"].startswith("closeout:")]
        self.assertEqual(len(closeout_samples), 1, pricing)
        self.assertEqual(closeout_samples[0]["price_sgd"], 3100)

    def test_stale_chat_revision_and_private_insight_boundaries(self):
        status, started = self.public_json(
            "POST", "/inquire", {"text": "Two videos, budget $500", "request_id": "start"})
        self.assertEqual(status, 200, started)
        inquiry = started["inquiry_id"]
        token_path = "/i/" + started["token"]
        first = self.rpc("commit_chat_turn", {
            "inquiry_id": inquiry, "through_sequence": 1, "expected_revision": 0,
            "request_id": "agent-1", "reply": "Thanks, I have those details.",
            "fact_changes": [{"subject": "videos", "value": 2, "status": "confirmed",
                              "source_sequence": 1}],
        })
        self.assertEqual(first["revision"], 1)
        status, posted = self.public_json(
            "POST", token_path + "/messages",
            {"text": "Actually three videos", "request_id": "buyer-2", "references": []})
        self.assertEqual(status, 200, posted)
        error = self.mcp_error("commit_chat_turn", {
            "inquiry_id": inquiry, "through_sequence": 1, "expected_revision": 1,
            "request_id": "agent-stale", "reply": "Thanks.", "fact_changes": [],
        })
        self.assertIn("stale_revision", error)
        current = self.rpc("get_conversation_context", {"inquiry_id": inquiry})
        self.assertEqual(current["revision"], 1)
        self.assertEqual(current["turns"][-1]["body"], "Actually three videos")

        status, raw = self.http("GET", "/api/estimating/history")
        self.assertEqual(status, 403, raw)
        status, raw = self.http("GET", "/api/estimating/history", owner=True)
        self.assertEqual(status, 200, raw)
        founder_history = json.loads(raw)
        self.assertTrue(founder_history["history"])
        private = self.rpc("get_scoping_history", {})
        self.assertTrue(private["private"])
        self.assertIn("do not forward", private["note"].lower())
        unknown = self.mcp_error("confirm_booking", {})
        self.assertIn("unknown_tool", unknown)

    def test_dashboard_and_buyer_inline_scripts_parse(self):
        if not shutil.which("node"):
            self.skipTest("node is unavailable")
        flow = self.publish("script-parse", "Script Cafe")
        pages = []
        for path, owner in (("/dashboard", True), (flow["url"], False)):
            status, body = self.http("GET", path, owner=owner)
            self.assertEqual(status, 200, body)
            pages.append((path, body.decode()))
        for path, markup in pages:
            scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", markup,
                                 flags=re.IGNORECASE | re.DOTALL)
            self.assertTrue(scripts, path)
            for index, script in enumerate(scripts):
                checked = subprocess.run(
                    ["node", "--check", "-"], input=html.unescape(script), text=True,
                    capture_output=True, timeout=10)
                self.assertEqual(checked.returncode, 0,
                                 f"{path} script {index}: {checked.stderr}")


if __name__ == "__main__":
    import unittest
    unittest.main()
