"""POST /qwenwork/callback: shared-secret auth, idempotency, founder approval gate.

Run: python3 -B -m unittest -v test_qwenwork_callback
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import conversation
import db
import qwenwork_callback
import server
from test_http import INQUIRY, RAW1, _db, request

SECRET = "s" * 40


class QwenWorkCallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-qwcb-")
        secret_file = Path(cls.tmp) / "secret"
        secret_file.write_text(SECRET + "\n")
        os.environ.pop(qwenwork_callback.SECRET_ENV, None)
        cls.db_path = str(Path(cls.tmp) / "test.db")
        cls.srv, cls.owner = server.make_server(cls.db_path, port=0,
                                                qwenwork_secret_file=str(secret_file))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def owner_post(self, path, data):
        status, text = request(self.base, "POST", path, data,
                               {"X-Owner-Token": self.owner, "Origin": self.base})
        self.assertEqual(status, 200, text)
        return json.loads(text)

    def priced_inquiry(self, source_id):
        """An inquiry bound to a deal whose engine draft awaits the founder."""
        conn = db.connect(self.db_path)
        try:
            inquiry = conversation.create_inquiry(conn, {"text": RAW1})["inquiry_id"]
        finally:
            conn.close()
        imp = self.owner_post("/api/import", {
            "source": "site", "source_id": "inquiry-" + inquiry, "raw_inquiry": RAW1,
            "offer_request": INQUIRY})
        version = self.owner_post("/api/evaluate", {"deal_id": imp["deal_id"],
                                                    "offer_id": imp["offer_id"]})["version_id"]
        with _db(self.db_path) as conn:
            conn.execute("UPDATE inquiries SET deal_id=? WHERE id=?", (imp["deal_id"], inquiry))
            conn.commit()
        return inquiry, imp["deal_id"], version

    def deliver(self, payload, signed=True, secret=SECRET, stamp=None, bearer=None):
        raw = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if signed:
            stamp = str(int(time.time()) if stamp is None else stamp)
            headers["X-BidPilot-Timestamp"] = stamp
            headers["X-BidPilot-Signature"] = qwenwork_callback.sign(secret, stamp, raw)
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        import urllib.request, urllib.error
        req = urllib.request.Request(self.base + qwenwork_callback.PATH, data=raw,
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_signed_result_becomes_unapproved_draft_once(self):
        inquiry, deal, version = self.priced_inquiry("a")
        payload = {"job_id": "job-a", "status": "completed", "inquiry_id": inquiry,
                   "result": {"draft_body": "Hi, here is our proposal for your promo videos."}}
        status, body = self.deliver(payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["outcome"], "draft_saved")
        self.assertTrue(body["founder_approval_required"])
        new = body["draft_version_id"]
        with _db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM proposal_versions WHERE id=?", (new,)).fetchone()
            self.assertEqual(row["status"], "draft")
            self.assertEqual(row["outbound_status"], "unchecked")
            self.assertIsNone(row["token_hash"])
            self.assertEqual(row["previous_version_id"], version)
            self.assertFalse(conn.execute(
                "SELECT 1 FROM approvals WHERE version_id=?", (new,)).fetchone())
        # Duplicate delivery: same answer, no second version.
        status, again = self.deliver(payload)
        self.assertEqual(status, 200)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["draft_version_id"], new)
        with _db(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM proposal_versions WHERE deal_id=?",
                                 (deal,)).fetchone()[0]
        self.assertEqual(count, 2)
        # Same job id, different body: rejected.
        status, body = self.deliver({**payload, "result": {"draft_body": "other"}})
        self.assertEqual((status, body["error"]), (409, "job_id_conflict"))

    def test_bearer_and_metadata_inquiry(self):
        inquiry, _, _ = self.priced_inquiry("b")
        status, body = self.deliver({"job_id": "job-b", "metadata": {"inquiry_id": inquiry},
                                     "result": {"draft_body": "Draft"}},
                                    signed=False, bearer=SECRET)
        self.assertEqual((status, body["outcome"]), (200, "draft_saved"))

    def test_authentication_failures(self):
        payload = {"job_id": "job-x", "inquiry_id": "nope", "result": {"draft_body": "x"}}
        self.assertEqual(self.deliver(payload, signed=False)[0], 401)
        self.assertEqual(self.deliver(payload, secret="w" * 40)[1]["error"], "bad_signature")
        self.assertEqual(self.deliver(payload, stamp=int(time.time()) - 3600)[1]["error"],
                         "stale_or_missing_timestamp")
        self.assertEqual(self.deliver(payload, signed=False, bearer="w" * 40)[0], 401)

    def test_unknown_and_unpriced_inquiries(self):
        status, body = self.deliver({"job_id": "job-u", "inquiry_id": "missing",
                                     "result": {"draft_body": "x"}})
        self.assertEqual((status, body["error"]), (404, "unknown_inquiry"))
        conn = db.connect(self.db_path)
        try:
            inquiry = conversation.create_inquiry(conn, {"text": "hello"})["inquiry_id"]
        finally:
            conn.close()
        status, body = self.deliver({"job_id": "job-h", "inquiry_id": inquiry,
                                     "result": {"draft_body": "x"}})
        self.assertEqual((status, body["outcome"]), (200, "held_not_priced"))
        status, body = self.deliver({"job_id": "job-f", "inquiry_id": inquiry,
                                     "status": "failed", "error": "model timeout"})
        self.assertEqual((status, body["outcome"]), (200, "failed"))

    def test_bad_payloads(self):
        self.assertEqual(self.deliver({"inquiry_id": "x"})[1]["error"], "job_id_required")
        self.assertEqual(self.deliver({"job_id": "j", "inquiry_id": "x"})[1]["error"],
                         "result.draft_body_required")

    def test_disabled_without_secret(self):
        srv, _ = server.make_server(str(Path(self.tmp) / "off.db"), port=0)
        self.assertIsNone(srv.qwenwork_secret)
        srv.server_close()

    def test_short_secret_rejected(self):
        short = Path(self.tmp) / "short"
        short.write_text("tooshort")
        with self.assertRaises(ValueError):
            qwenwork_callback.load_secret(str(short))


if __name__ == "__main__":
    unittest.main()
