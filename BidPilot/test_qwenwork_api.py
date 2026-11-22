"""Loopback-only OAuth and quote-session regression tests."""
import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import core
import conversation
import db
import qwenwork_api as qw
import server
from test_http import INQUIRY, RAW1, request


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, value, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.endswith('openid-configuration'):
            return self.answer({'authorization_endpoint': self.server.base + '/authorize',
                                'token_endpoint': self.server.base + '/token'})
        self.server.auth.append(self.headers.get('Authorization'))
        if path.endswith('/events'):
            return self.answer({'items': [], 'next_cursor': 8, 'has_more': False})
        if '/turns/' in path:
            return self.answer({'status': self.server.turn_status, 'final_message': 'Here is our proposal for your review.'})
        return self.answer({'status': self.server.session_status})

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        if self.path == '/token':
            self.server.forms.append(parse_qs(body.decode()))
            return self.answer({'access_token': 'test-access', 'refresh_token': 'rotated-refresh',
                                'token_type': 'Bearer', 'expires_in': 3600})
        self.server.prompts.append(json.loads(body))
        return self.answer({'session_id': 'test-session', 'first_turn_id': 'first', 'status': 'starting'}, 202)

    def do_DELETE(self):
        self.server.deleted += 1
        self.answer({})


class OpenAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = ThreadingHTTPServer(('127.0.0.1', 0), Fake)
        self.fake.base = 'http://127.0.0.1:' + str(self.fake.server_address[1])
        self.fake.forms, self.fake.auth, self.fake.prompts = [], [], []
        self.fake.deleted = 0
        self.fake.session_status, self.fake.turn_status = 'idle', 'completed'
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.client = qw.Client('test-public-client', oauth_base=self.fake.base,
                                api_base=self.fake.base, token_path=Path(self.tmp.name) / 'oauth.json')
        self.conn = db.connect(Path(self.tmp.name) / 'app.db')
        core.load_seed(self.conn)
        self.conn.executescript(qw.SCHEMA)

    def tearDown(self):
        self.conn.close()
        self.fake.shutdown()
        self.fake.server_close()
        self.tmp.cleanup()

    def login(self):
        query = parse_qs(urlsplit(self.client.start_login()).query)
        self.client.finish_login({'state': query['state'], 'code': ['test-code']})
        return query

    def priced(self):
        inquiry = conversation.create_inquiry(self.conn, {'text': RAW1})['inquiry_id']
        deal, offer, _ = core.claim_or_resume(self.conn, 'site', inquiry, INQUIRY['offer']['client'], RAW1, INQUIRY)
        _, version = core.evaluate(self.conn, deal, offer)
        self.conn.execute('UPDATE inquiries SET deal_id=?,processed_seq=1 WHERE id=?', (deal, inquiry))
        revision = self.conn.execute('SELECT revision FROM inquiries WHERE id=?', (inquiry,)).fetchone()[0]
        self.conn.execute('INSERT INTO offer_briefs VALUES (?,?,?)', (offer, inquiry, revision))
        self.conn.commit()
        return version

    def test_pkce_and_exchange(self):
        query = parse_qs(urlsplit(self.client.start_login()).query)
        verifier = self.client.states[query['state'][0]][0]
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(query['code_challenge'], [expected])
        self.assertEqual(query['scope'], [qw.SCOPE])
        self.client.finish_login({'state': query['state'], 'code': ['test-code']})
        self.assertEqual(self.fake.forms[-1]['code_verifier'], [verifier])
        self.assertEqual(self.fake.forms[-1]['redirect_uri'], [qw.REDIRECT_URI])
        self.assertNotIn('client_secret', self.fake.forms[-1])
        self.assertEqual(os.stat(self.client.token_path).st_mode & 0o777, 0o600)
        with self.assertRaises(qw.APIError):
            self.client.finish_login({'state': query['state'], 'code': ['test-code']})

    def test_state_mismatch_and_expiry(self):
        with self.assertRaises(qw.APIError):
            self.client.finish_login({'state': ['wrong'], 'code': ['x']})
        query = parse_qs(urlsplit(self.client.start_login()).query)
        state = query['state'][0]
        verifier, _ = self.client.states[state]
        self.client.states[state] = (verifier, 0)
        with self.assertRaises(qw.APIError):
            self.client.finish_login({'state': [state], 'code': ['x']})

    def test_refresh_rotation_single_flight(self):
        self.client._save({'access_token': 'old', 'refresh_token': 'old-refresh', 'expires_at': 0})
        threads = [threading.Thread(target=self.client.access_token) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(self.fake.forms), 1)
        self.assertEqual(self.fake.forms[0]['refresh_token'], ['old-refresh'])
        self.assertEqual(self.client._load()['refresh_token'], 'rotated-refresh')

    def test_finished_saved_once_not_released(self):
        self.login()
        version = self.priced()
        first = self.client.request_draft(self.conn, version)
        self.assertEqual(self.client.request_draft(self.conn, version)['session_id'], first['session_id'])
        self.assertEqual(len(self.fake.prompts), 1)
        material = json.loads(self.fake.prompts[0]['initial_prompt']['text'].split('\n', 1)[1])
        self.assertEqual(set(material), {'studio_name','client_messages','confirmed_facts','options'})
        row = self.conn.execute('SELECT * FROM qwenwork_sessions').fetchone()
        self.client.follow_once(self.conn, row)
        self.client.follow_once(self.conn, row)
        saved = self.conn.execute('SELECT * FROM qwenwork_sessions').fetchone()
        self.assertEqual(saved['status'], 'done')
        result = self.conn.execute('SELECT * FROM proposal_versions WHERE id=?', (saved['result_version_id'],)).fetchone()
        self.assertIn(result['status'], ('checked','draft','blocked'))
        self.assertIsNone(result['token_hash'])
        self.assertIsNone(result['published_at'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM proposal_versions').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM approvals').fetchone()[0], 0)

    def test_idle_not_completion(self):
        self.login()
        self.client.request_draft(self.conn, self.priced())
        self.fake.turn_status = 'running'
        self.client.follow_once(self.conn, self.conn.execute('SELECT * FROM qwenwork_sessions').fetchone())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM proposal_versions').fetchone()[0], 1)

    def test_failed_terminated_no_draft(self):
        self.login()
        self.client.request_draft(self.conn, self.priced())
        self.fake.session_status = 'terminated'
        stop = threading.Event()
        thread = threading.Thread(target=self.client.worker, args=(Path(self.tmp.name) / 'app.db', stop))
        thread.start()
        time.sleep(0.2)
        stop.set()
        thread.join()
        self.assertEqual(self.conn.execute('SELECT status FROM qwenwork_sessions').fetchone()[0], 'failed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM proposal_versions').fetchone()[0], 1)

    def test_failed_turn_and_timeout(self):
        self.login()
        self.client.request_draft(self.conn, self.priced())
        row = self.conn.execute('SELECT * FROM qwenwork_sessions').fetchone()
        self.fake.turn_status = 'failed'
        with self.assertRaisesRegex(qw.APIError, 'turn_failed'):
            self.client.follow_once(self.conn, row)
        expired = dict(row)
        expired['created_at'] = 0
        with self.assertRaisesRegex(qw.APIError, 'session_timeout'):
            self.client.follow_once(self.conn, expired)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM proposal_versions').fetchone()[0], 1)

    def test_refresh_failure_requires_sign_in(self):
        self.client._save({'access_token':'old', 'expires_at':0})
        with self.assertRaisesRegex(qw.APIError, 'sign_in_required'):
            self.client.access_token()
        self.assertFalse(self.client.connected())

    def test_route_authentication(self):
        srv, owner = server.make_server(Path(self.tmp.name) / 'http.db')
        srv.qwenwork_api = self.client
        base = 'http://127.0.0.1:' + str(srv.server_address[1])
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertEqual(request(base, 'GET', '/qwenwork/connect')[0], 403)
            self.assertEqual(request(base, 'POST', '/api/qwenwork/draft', {'version_id':'x'}, {'Origin':base})[0], 403)
            query = parse_qs(urlsplit(self.client.start_login()).query)
            callback = '/qwenwork/callback?state=' + query['state'][0] + '&code=test-code'
            self.assertEqual(request(base, 'GET', callback)[0], 200)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == '__main__':
    unittest.main()
