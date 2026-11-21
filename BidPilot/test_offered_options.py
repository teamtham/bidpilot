"""Founder can untick engine options before approving a counteroffer.

The optional option_ids list on /api/approve narrows what the client sees and
can accept. Absent means every option, exactly as before; the outbound check,
digest and option-set integrity rules are unchanged.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

import server
import test_http


class OfferedOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-offered-")
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

    def db(self):
        return test_http._db(self.db_path)

    def counter_version(self, key):
        f = self.flow(key)
        _, text = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": key + "-chg",
            "body": "five videos, five captioned versions by 4 Dec, same S$2,700"})
        offer2 = json.loads(text)["offer_id"]
        _, text = self.post("/api/evaluate", {"deal_id": f["deal_id"], "offer_id": offer2})
        version_id = json.loads(text)["version_id"]
        _, text = self.post("/api/check", {"version_id": version_id})
        self.assertTrue(json.loads(text)["ok"], text)
        with self.db() as conn:
            rows = conn.execute(
                "SELECT o.id, o.option_key FROM options o WHERE o.id IN (SELECT value FROM"
                " json_each((SELECT public_option_ids_json FROM proposal_versions WHERE id=?)))",
                (version_id,)).fetchall()
        keys = {r["option_key"]: r["id"] for r in rows}
        self.assertGreaterEqual(len(keys), 2)
        return version_id, keys

    def test_subset_is_the_only_thing_the_client_sees_and_can_accept(self):
        version_id, keys = self.counter_version("offered-subset")
        keep = [keys["hold_budget"], keys["phased"]]
        status, text = self.post("/api/approve", {"version_id": version_id, "option_ids": keep})
        self.assertEqual(status, 200, text)
        res = json.loads(text)
        status, page = test_http.request(self.base, "GET", res["url"], None, {})
        self.assertEqual(status, 200)
        self.assertIn("S$2,700.00", page)
        self.assertIn("Phased delivery", page)
        self.assertNotIn("S$2,050.00", page)        # unticked hold_date
        token = res["url"].rsplit("/", 1)[1]
        status, text = test_http.request(self.base, "POST", "/p/" + token + "/accept",
                               {"option_id": keys["hold_date"]}, {"Origin": self.base})
        self.assertNotEqual(json.loads(text).get("ok"), True, text)
        status, text = test_http.request(self.base, "POST", "/p/" + token + "/accept",
                                         {"option_id": keys["hold_budget"]}, {"Origin": self.base})
        self.assertTrue(json.loads(text).get("ok"), text)
        with self.db() as conn:
            stored = conn.execute("SELECT offered_option_ids_json FROM proposal_versions"
                                  " WHERE id=?", (version_id,)).fetchone()[0]
        self.assertEqual(sorted(json.loads(stored)), sorted(keep))

    def test_empty_or_foreign_subset_is_refused(self):
        version_id, keys = self.counter_version("offered-bad")
        for bad in ([], ["not-an-option"], [keys["phased"], keys["phased"]]):
            status, text = self.post("/api/approve", {"version_id": version_id,
                                                      "option_ids": bad})
            self.assertEqual(status, 409, text)
            self.assertIn("invalid_offered_options", json.loads(text)["reasons"])

    def test_no_subset_offers_everything(self):
        version_id, keys = self.counter_version("offered-all")
        status, text = self.post("/api/approve", {"version_id": version_id})
        self.assertEqual(status, 200, text)
        with self.db() as conn:
            stored = conn.execute("SELECT offered_option_ids_json FROM proposal_versions"
                                  " WHERE id=?", (version_id,)).fetchone()[0]
        self.assertIsNone(stored)


if __name__ == "__main__":
    unittest.main()
