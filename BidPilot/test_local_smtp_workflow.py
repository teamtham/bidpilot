"""Real loopback SMTP/TLS and HTTP acceptance; deterministic MCP composition."""
import email
from email import policy
import http.client
import json
import os
from pathlib import Path
import re
import socketserver
import ssl
import subprocess
import threading
import unittest
from unittest.mock import patch

import server
import test_proposal_delivery as workflow_helpers


class Catcher(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 synthetic catcher\r\n')
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.split(b' ', 1)[0].strip().upper()
            if command == b'EHLO':
                self.wfile.write(b'250-localhost\r\n250 AUTH PLAIN\r\n')
            elif command in (b'AUTH', b'MAIL', b'RCPT', b'RSET', b'NOOP'):
                self.wfile.write(b'250 OK\r\n' if command != b'AUTH' else b'235 authenticated\r\n')
            elif command == b'DATA':
                self.wfile.write(b'354 send data\r\n')
                data = []
                while True:
                    chunk = self.rfile.readline()
                    if chunk == b'.\r\n' or not chunk:
                        break
                    data.append(chunk[1:] if chunk.startswith(b'..') else chunk)
                if self.server.reject_next:
                    self.server.reject_next = False
                    self.wfile.write(b'550 synthetic rejection\r\n')
                else:
                    self.server.messages.append(email.message_from_bytes(b''.join(data), policy=policy.default))
                    self.wfile.write(b'250 accepted\r\n')
            elif command == b'QUIT':
                self.wfile.write(b'221 bye\r\n')
                return
            else:
                self.wfile.write(b'502 unsupported\r\n')


class TLSCatcher(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def get_request(self):
        sock, address = super().get_request()
        return self.context.wrap_socket(sock, server_side=True), address


class LocalSMTPWorkflow(workflow_helpers.ProposalDeliveryTests):
    test_clip_only_late_corrected_email_single_release_and_link = None
    test_configured_buyer_origin_is_separate_from_founder = None
    test_counter_composition_and_stale_selection = None
    test_invalid_email_stale_recipient_and_unfinished_composition = None
    test_mail_rejection_retry_same_version_and_missing_credentials = None
    test_stale_capacity_review_and_durable_composition_wake = None

    def setUp(self):
        super().setUp()
        self.http_server, self.owner = server.make_server(self.path, port=0)
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()
        cert, key = Path(self.tmp.name) / 'cert.pem', Path(self.tmp.name) / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True)
        self.catcher = TLSCatcher(('127.0.0.1', 0), Catcher)
        self.catcher.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.catcher.context.load_cert_chain(cert, key)
        self.catcher.messages = self.mail
        self.catcher.reject_next = False
        self.smtp_thread = threading.Thread(target=self.catcher.serve_forever, daemon=True)
        self.smtp_thread.start()
        self.smtp_config = patch.dict(os.environ, {'BIDPILOT_SMTP_HOST': '127.0.0.1',
            'BIDPILOT_SMTP_PORT': str(self.catcher.server_address[1]), 'BIDPILOT_SMTP_SECURITY': 'ssl'})
        self.smtp_config.start()
        self.saved_pages = {}

    def http(self, method, path, body=None, owner=False):
        conn = http.client.HTTPConnection('127.0.0.1', self.http_server.server_address[1], timeout=10)
        headers = {'Content-Type': 'application/json',
                   'Origin': 'http://127.0.0.1:' + str(self.http_server.server_address[1])}
        if owner:
            headers['X-Owner-Token'] = self.owner
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def approve(self, review=None):
        review = review or self.review()
        payload = {'version_id': self.version, 'option_ids': review['option_ids'],
                   'reviewed_digest': review['reviewed_digest']}
        status, raw = self.http('POST', '/api/approve-and-send', payload, owner=True)
        return status, json.loads(raw), payload

    def tearDown(self):
        # Export sanitized evidence only, never the DB or owner capability.
        root = os.environ.get('BIDPILOT_SMTP_EVIDENCE')
        if root:
            target = Path(root)
            target.mkdir(parents=True, exist_ok=True)
            for name, content in self.saved_pages.items():
                self.assertNotIn(self.owner.encode(), content)
                (target / name).write_bytes(content)
            for index, message in enumerate(self.mail):
                content = message.as_bytes()
                self.assertNotIn(self.owner.encode(), content)
                (target / ('smtp-mail-' + str(index) + '.eml')).write_bytes(content)
            report = {'transport': 'real smtplib SMTP_SSL to loopback TLS socketserver catcher',
                      'http': 'throwaway loopback HTTP server, synthetic temporary database',
                      'composition': 'deterministic in-process MCP calls; not live QwenWork',
                      'accepted_messages': len(self.mail), 'recipient': 'corrected@example.com',
                      'public_https_reachability': 'not verified; emailed HTTPS path mapped to loopback for testing',
                      'reference_design': 'not verified; both reference states blocked by Cloudflare',
                      'counter_pricing': 'legacy replay transport contract; detailed original/cutdown repricing not established',
                      'checks': ['clip-only start', 'late corrected email', 'preview and buyer options match',
                                 'initial and counter approved once', 'SMTP DATA rejection then retry',
                                 'stale selection rejected', 'duplicate approval does not send again']}
            text = json.dumps(report, indent=2)
            self.assertNotIn(self.owner, text)
            self.assertNotRegex(text, r'/p/[A-Za-z0-9_-]+')
            (target / 'verification.json').write_text(text)
        self.smtp_config.stop()
        self.catcher.shutdown()
        self.catcher.server_close()
        self.http_server.shutdown()
        self.http_server.server_close()
        self.http_thread.join()
        self.smtp_thread.join()
        super().tearDown()

    def capture_release(self, label, selected=None):
        review = self.review(selected)
        query = ''.join('&option_id=' + oid for oid in review['option_ids'])
        status, preview = self.http('GET', '/preview/' + self.version + '?unused=1' + query, owner=True)
        self.assertEqual(status, 200)
        status, result, payload = self.approve(review)
        self.assertEqual(status, 200, result)
        return review, preview, result, payload

    def verify_page(self, label, preview, result, ids):
        status, buyer = self.http('GET', '/p/' + result['url'].split('/p/')[1])
        self.assertEqual(status, 200)
        # Same structured card option IDs, quantities, dates and prices; only
        # preview status/action availability should differ.
        for oid in ids:
            self.assertIn(oid.encode(), buyer)
            self.assertIn(oid.encode(), preview)
        self.assertEqual(re.findall(rb'S\$[\d,.]+', preview), re.findall(rb'S\$[\d,.]+', buyer))
        self.assertIn(result['url'], self.mail[-1].get_content())
        self.assertEqual(self.mail[-1]['To'], 'corrected@example.com')
        self.saved_pages[label + '-preview.html'] = preview
        self.saved_pages[label + '-buyer.html'] = buyer

    def test_real_smtp_initial_counter_failure_retry(self):
        self.start(clip=True)
        priced = self.prepare()
        self.turn('Please send to first@example.com', 'first@example.com', False)
        self.turn('Actually use corrected@example.com', 'corrected@example.com')
        review, preview, initial, payload = self.capture_release('initial')
        self.assertEqual(initial['email_status'], 'accepted')
        self.verify_page('initial', preview, initial, review['option_ids'])
        self.http('POST', '/api/approve-and-send', payload, owner=True)
        self.assertEqual(len(self.mail), 1)
        old_token = initial['url'].split('/p/')[1]
        old_option = review['option_ids'][0]
        request = 'Please make one original and two cutdowns, keeping the shared shoot.'
        status, raw = self.http('POST', '/p/' + old_token + '/request-change',
                                {'body': request, 'request_id': 'smtp-counter'})
        self.assertEqual(status, 202, raw)
        change = next(c for c in self.rpc('list_pending_work', {})['change_requests'] if c['request_id'] == 'smtp-counter')
        from test_http import INQUIRY
        # Legacy fixture arithmetic tests the transport contract only. The
        # derivative request remains evidence, not invented pricing inputs.
        revised = self.rpc('submit_change_offer', {'deal_id': priced['deal_id'], 'version_id': self.version,
            'request_id': change['request_id'], 'offer_request': INQUIRY, 'evidence': {'raw_message': request}})
        evaluated = self.rpc('evaluate_offer', {'deal_id': priced['deal_id'], 'offer_id': revised['offer_id']})
        composed = self.rpc('compose_proposal_page', {'version_id': evaluated['draft_version']['version_id'],
            'page': {'headline': 'Revised cafe direction', 'introduction': 'The master story leads the derivative edits.',
                     'sections': [{'title': 'Your requested change', 'body': 'The original and cutdowns share the retained shoot.'}]}})
        self.version = composed['version_id']
        self.rpc('check_draft', {'version_id': self.version})
        self.catcher.reject_next = True
        review, preview, counter, payload = self.capture_release('counter')
        self.assertEqual(counter['email_status'], 'failed')
        self.assertEqual(len(self.mail), 1)
        status, raw = self.http('POST', '/api/retry-email', {'version_id': self.version}, owner=True)
        self.assertEqual(json.loads(raw)['status'], 'accepted')
        self.assertEqual(len(self.mail), 2)
        self.verify_page('counter', preview, counter, review['option_ids'])
        self.http('POST', '/api/approve-and-send', payload, owner=True)
        self.http('POST', '/api/retry-email', {'version_id': self.version}, owner=True)
        self.assertEqual(len(self.mail), 2)
        self.assertEqual(self.http('POST', '/p/' + old_token + '/accept', {'option_id': old_option})[0], 404)
        self.assertEqual(self.c.execute("SELECT count(*) FROM approvals WHERE action='approve'").fetchone()[0], 2)
        self.assertEqual(self.c.execute('SELECT count(*) FROM proposal_outbox').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
