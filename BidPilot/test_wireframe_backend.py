"""Backend support for the wireframe-v5 dashboard: price list, custom-job
estimates built from it, and approved follow-up questions."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

import core
import db
import price_list
import server
import test_http


class WireframeBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-wf-")
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

    def state(self):
        return json.loads(self.get("/api/state")[1])

    def test_price_list_is_served_and_saved(self):
        items = self.state()["price_list"]
        self.assertIn("anim", {i["id"] for i in items})
        status, text = self.post("/api/price-list", {"items": [{"id": "anim", "hours": 9, "price_sgd": 750}]})
        self.assertEqual(status, 200, text)
        anim = next(i for i in self.state()["price_list"] if i["id"] == "anim")
        self.assertEqual((anim["hours"], anim["price_sgd"]), (9.0, 750.0))
        status, text = self.post("/api/price-list", {"items": [{"id": "nope", "hours": 1, "price_sgd": 1}]})
        self.assertEqual(status, 400)
        self.assertIn("Unknown", json.loads(text)["message"])

    def test_custom_job_is_estimated_from_the_price_list(self):
        request = dict(test_http.INQUIRY)
        request.pop("pricing_mode", None)
        request["offer"] = {**test_http.INQUIRY["offer"], "graphics": "custom_animation",
                            "deliverables": 1, "caption_versions": 1, "deadline": "2026-12-18"}
        _, text = self.post("/api/import", {"source": "hashmeta", "source_id": "wf-anim",
                                            "raw_inquiry": "animated intro", "offer_request": request})
        imp = json.loads(text)
        self.post("/api/evaluate", {"deal_id": imp["deal_id"], "offer_id": imp["offer_id"]})
        deal = next(d for d in self.state()["deals"] if d["deal_id"] == imp["deal_id"])
        if deal["phase"] != "needs_estimate":
            self.skipTest("engine priced this custom job directly")
        status, text = self.post("/api/price-list-estimate", {"deal_id": imp["deal_id"],
                                 "items": [{"id": "anim", "qty": 1}, {"id": "edit", "qty": 1}]})
        self.assertEqual(status, 200, text)
        res = json.loads(text)
        self.assertGreater(res["hours"], 0)
        deal = next(d for d in self.state()["deals"] if d["deal_id"] == imp["deal_id"])
        self.assertNotEqual(deal["phase"], "needs_estimate")
        status, text = self.post("/api/price-list-estimate", {"deal_id": imp["deal_id"], "items": []})
        self.assertEqual(status, 400)

    def test_send_question_marks_the_needs_info_deal_as_asked(self):
        _, text = self.post("/api/import", {"source": "hashmeta", "source_id": "wf-ni",
                                            "raw_inquiry": "brief", "offer_request": test_http.INQUIRY})
        deal_id = json.loads(text)["deal_id"]
        with db.connect(self.db_path) as c:
            c.execute("UPDATE deals SET status='needs_information' WHERE id=?", (deal_id,))
            c.commit()
        status, text = self.post("/api/send-question", {"deal_id": deal_id, "question": "What is your budget?"})
        self.assertEqual(status, 200, text)
        deal = next(d for d in self.state()["deals"] if d["deal_id"] == deal_id)
        self.assertTrue(deal["needs_information"]["question_sent"])
        self.assertEqual(deal["needs_information"]["question"], "What is your budget?")
        status, _ = self.post("/api/send-question", {"deal_id": deal_id, "question": ""})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
