"""Owner links survive restarts and in-page navigation without loosening access."""
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import server


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers
    except urllib.error.HTTPError as err:
        return err.code, err.headers


class OwnerSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-owner-")
        cls.token_file = Path(cls.tmp) / "private" / "owner_token"
        cls.srv, cls.owner = server.make_server(str(Path(cls.tmp) / "test.db"), port=0,
                                                public_origin="https://public.example.com",
                                                owner_token_file=str(cls.token_file))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_token_file_is_private_and_reused(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode), 0o600)
        self.assertEqual(server.load_owner_token(self.token_file), self.owner)

    def test_valid_link_sets_strict_httponly_cookie_that_then_works_alone(self):
        status, headers = fetch(self.base + "/?owner=" + self.owner)
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie")
        self.assertIn("bidpilot_owner=" + self.owner, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        jar = {"Cookie": "bidpilot_owner=" + self.owner}
        for path in ("/", "/dashboard", "/portfolio", "/api/state", "/portfolio?owner="):
            self.assertEqual(fetch(self.base + path, jar)[0], 200, path)

    def test_no_token_wrong_cookie_and_public_host_are_refused(self):
        self.assertEqual(fetch(self.base + "/")[0], 403)
        self.assertEqual(fetch(self.base + "/", {"Cookie": "bidpilot_owner=wrong"})[0], 403)
        public = {"Host": "public.example.com", "Cookie": "bidpilot_owner=" + self.owner}
        self.assertEqual(fetch(self.base + "/?owner=" + self.owner, public)[0], 403)

    def test_bad_token_never_sets_cookie(self):
        status, headers = fetch(self.base + "/?owner=nope")
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get("Set-Cookie"))


if __name__ == "__main__":
    unittest.main()
