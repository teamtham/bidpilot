"""Client change chat on the proposal page, and the engine pricing the change."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import change_chat
import server
import test_http


class ChangeChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {"GEMINI_API_KEY": ""})
        cls.env.start()
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-change-chat-")
        cls.db_path = str(Path(cls.tmp) / "test.db")
        cls.srv, cls.owner = server.make_server(cls.db_path, port=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.env.stop()

    post = classmethod(test_http.HttpTests.post.__func__)
    get = classmethod(test_http.HttpTests.get.__func__)
    import_inquiry = classmethod(test_http.HttpTests.import_inquiry.__func__)
    flow = classmethod(test_http.HttpTests.flow.__func__)
    state = test_http.HttpTests.state
    deal_state = test_http.HttpTests.deal_state

    def chat(self, f, text):
        status, raw = self.post(f["url"] + "/change-chat", {"text": text}, token=False)
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def test_page_has_chat_not_library(self):
        f = self.flow("cc-page")
        status, page = self.get(f["url"], token=False)
        self.assertEqual(status, 200)
        self.assertIn('id="cc-form"', page)
        self.assertIn("Want something changed?", page)
        self.assertNotIn("Explore studio work", page)
        self.assertIn('id="rc-count"', page)          # the form stays as a fallback

    def test_chat_fills_terms_and_change_is_priced(self):
        f = self.flow("cc-flow")
        first = self.chat(f, "Can we add a fourth video and get them by 4 Dec? We're opening earlier.")
        self.assertEqual(first["terms"]["count"], 4)
        self.assertEqual(first["terms"]["deadline"], "2026-12-04")
        self.assertIn("captions", first["turns"][-1]["text"].lower())
        second = self.chat(f, "Yes, captions on all of them")
        self.assertEqual(second["terms"]["captions"], 4)
        third = self.chat(f, "Up to about S$3,000")
        self.assertEqual(third["terms"]["budget"], 3000.0)
        self.assertEqual(sorted(third["changed"]), ["budget", "captions", "count", "deadline"])
        for turn in third["turns"]:
            if turn["sender"] == "assistant":
                self.assertNotRegex(turn["text"], r"(?i)booked|approved|we agree")
        # The page reloads with the same chat and card.
        _, page = self.get(f["url"], token=False)
        self.assertIn("Up to about S$3,000", page)
        self.assertIn("<del>3</del> <ins>4</ins>", page)
        # Send to Alex = the existing change request with the confirmed terms.
        t = third["terms"]
        status, raw = self.post(f["url"] + "/request-change", {
            "body": third["client_words"], "request_id": "cc-send-1",
            "terms": {"count": t["count"], "captions": t["captions"],
                      "deadline": t["deadline"], "budget": t["budget"]}}, token=False)
        self.assertEqual(status, 200, raw)
        offer_id = json.loads(raw)["offer_id"]
        # The engine priced the change straight away.
        deal = self.deal_state(f["deal_id"])
        latest = max(deal["versions"], key=lambda v: v["version_no"])
        self.assertEqual(latest["offer_id"], offer_id)
        self.assertIn(latest["verdict"], ("accept", "accept_with_approval", "counter", "decline"))
        self.assertTrue(latest["comparison"]["is_revision"])
        changed = {c["field"] for c in latest["comparison"]["changed_fields"]}
        self.assertTrue({"deliverables", "deadline", "budget_sgd"} <= changed)
        _, dash = self.get("/dashboard")
        self.assertIn("function changePanel", dash)
        # The client page now says the change is with Alex, with no chat box.
        _, page = self.get(f["url"], token=False)
        self.assertIn("Your change request is with Alex", page)
        self.assertNotIn('id="cc-form"', page)
        # More chat on the same link is refused once nothing is live to change.
        status, _ = self.post(f["url"] + "/change-chat", {"text": "one more video"}, token=False)
        self.assertIn(status, (200, 404))

    def test_reset_and_bad_input(self):
        f = self.flow("cc-reset")
        self.chat(f, "one more video please")
        status, raw = self.post(f["url"] + "/change-chat", {"reset": True}, token=False)
        self.assertEqual(json.loads(raw)["changed"], [])
        status, _ = self.post(f["url"] + "/change-chat", {"text": ""}, token=False)
        self.assertEqual(status, 400)
        status, _ = self.post(f["url"] + "/change-chat", {"text": "x" * 2000}, token=False)
        self.assertEqual(status, 400)
        status, _ = self.post("/p/not-a-token/change-chat", {"text": "hi"}, token=False)
        self.assertEqual(status, 404)
        status, _ = self.post(f["url"] + "/change-chat", {"text": "hi"}, token=False, origin=False)
        self.assertEqual(status, 403)

    def test_parser_and_reply_guard(self):
        from datetime import date
        draft = {"count": 3, "captions": 3, "deadline": "2026-12-11", "budget": 2700.0}
        as_of = date(2026, 11, 23)
        self.assertEqual(change_chat.parse("Keep the date but make it three videos", draft, as_of), {"count": 3})
        self.assertEqual(change_chat.parse("no captions", draft, as_of), {"captions": 0})
        self.assertEqual(change_chat.parse("budget 3200", draft, as_of), {"budget": 3200.0})
        self.assertEqual(change_chat.parse("by 1 Nov", draft, as_of), {"deadline": "2027-11-01"})
        self.assertIsNone(change_chat._safe_reply("Great, you're booked!", draft))
        self.assertIsNone(change_chat._safe_reply("That would be S$3,350.", draft))
        self.assertEqual(change_chat._safe_reply("Noted, your budget is S$2,700.", draft),
                         "Noted, your budget is S$2,700.")


if __name__ == "__main__":
    unittest.main()
