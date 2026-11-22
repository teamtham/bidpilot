"""Synthetic end-to-end contracts. No real SMTP or production database."""
import base64
import json
import os
from pathlib import Path
import smtplib
import unittest
from unittest.mock import patch

import commerce
import conversation
import core
import db
import delivery
from test_http import INQUIRY
import test_studio_journey as journey_helpers

CONFIG = {'BIDPILOT_PUBLIC_URL': 'https://proposals.example.com',
          'BIDPILOT_SMTP_HOST': 'sandbox.invalid', 'BIDPILOT_MAIL_FROM': 'studio@example.com',
          'BIDPILOT_SMTP_USER': 'sandbox', 'BIDPILOT_SMTP_PASSWORD': 'synthetic-only'}


class ProposalDeliveryTests(journey_helpers.Journey):
    # Inherit transport helpers, not unrelated parent test cases.
    test_inquiry_to_confirmed_booking = None
    test_conversation_scoping_to_detailed_proposal = None

    def setUp(self):
        super().setUp()
        self.config = patch.dict(os.environ, CONFIG)
        self.config.start()
        self.c = db.connect(self.path)
        self.mail = []

    def tearDown(self):
        evidence_root = os.environ.get('BIDPILOT_TEST_EVIDENCE')
        if evidence_root:
            import shutil
            target = Path(evidence_root) / self._testMethodName
            target.mkdir(parents=True, exist_ok=True)
            evidence = db.connect(str(target / 'synthetic.db'))
            self.c.backup(evidence)
            evidence.close()
            for index, message in enumerate(self.mail):
                (target / ('mail-' + str(index) + '.eml')).write_bytes(message.as_bytes())
            for row in self.c.execute('SELECT version_id,url FROM proposal_outbox'):
                token = row['url'].split('/p/')[-1]
                status, body = self.http('GET', '/p/' + token)
                (target / (row['version_id'] + '-buyer.html')).write_bytes(body)
                status, body = self.http('GET', '/preview/' + row['version_id'], owner=True)
                (target / (row['version_id'] + '-preview.html')).write_bytes(body)
            print('Synthetic verification: ' + str(target))
        self.c.close()
        self.config.stop()
        super().tearDown()

    def start(self, clip=False):
        status, raw = self.http('POST', '/inquire', {'text': '', 'attachment_first': True, 'request_id': 'start'})
        self.assertEqual(status, 200, raw)
        started = json.loads(raw)
        self.inquiry = started['inquiry_id']
        self.base = '/i/' + started['token']
        if clip:
            sample = Path(__file__).parent / 'sample-portfolio/cafe.mp4'
            status, raw = self.http('POST', self.base + '/upload', {
                'filename': 'reference.mp4', 'mime': 'video/mp4',
                'data': base64.b64encode(sample.read_bytes()).decode()})
            self.assertEqual(status, 200, raw)
            # Upload itself is the first action. Analysis is independently
            # queued; no fabricated visual interpretation is injected.
            self.assertEqual(self.rpc('get_conversation_context', {'inquiry_id': self.inquiry})['turns'], [])
        return started

    def turn(self, text, email=None, confirmed=True):
        status, raw = self.http('POST', self.base + '/messages', {'text': text, 'request_id': 'buyer-' + text})
        self.assertEqual(status, 200, raw)
        seq = json.loads(raw)['sequence']
        context = self.rpc('get_conversation_context', {'inquiry_id': self.inquiry})
        args = {'inquiry_id': self.inquiry, 'through_sequence': seq,
                'expected_revision': context['revision'], 'request_id': 'reply-' + str(seq),
                'reply': 'Thanks, your project is saved for studio review.', 'fact_changes': []}
        if email:
            args['proposal_email'] = {'email': email, 'confirmed': confirmed}
        return self.rpc('commit_chat_turn', args)

    def prepare(self, headline='A cafe launch', intro='A warm espresso-led story for your launch.'):
        self.turn('Juniper Cafe requests three videos with captions by 2026-12-11 and budget 2700.')
        priced = self.rpc('price_inquiry_scope', {'inquiry_id': self.inquiry, 'expected_revision': 0, 'offer_request': INQUIRY})
        pending = self.rpc('list_pending_work', {})
        self.assertIn(priced['version_id'], json.dumps(pending['proposal_compositions']))
        page = {'headline': headline, 'introduction': intro, 'sections': [{'title': 'Creative direction', 'body': 'Coffee details and welcoming service shape the story.'}]}
        composed = self.rpc('compose_proposal_page', {'version_id': priced['version_id'], 'page': page})
        replay = self.rpc('compose_proposal_page', {'version_id': priced['version_id'], 'page': page})
        self.assertEqual(composed['version_id'], replay['version_id'])
        self.assertIn(composed['version_id'], json.dumps(self.rpc('list_pending_work', {})['proposal_checks']))
        self.assertTrue(self.rpc('check_draft', {'version_id': composed['version_id']})['outcome']['passed'])
        self.version = composed['version_id']
        return priced

    def review(self, ids=None):
        status, raw = self.http('POST', '/api/release-review', {'version_id': self.version, **({'option_ids': ids} if ids else {})}, owner=True)
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def approve(self, review=None):
        review = review or self.review()
        payload = {'version_id': self.version, 'option_ids': review['option_ids'], 'reviewed_digest': review['reviewed_digest']}
        with patch('delivery.smtplib.SMTP') as smtp:
            smtp.return_value.send_message.side_effect = lambda m: self.mail.append(m) or {}
            status, raw = self.http('POST', '/api/approve-and-send', payload, owner=True)
        return status, json.loads(raw), payload

    def test_clip_only_late_corrected_email_single_release_and_link(self):
        self.start(clip=True)
        priced = self.prepare()
        status, blocked, _ = self.approve()
        self.assertEqual(status, 409)
        self.assertIn('waiting_for_buyer_email', blocked['reasons'])
        self.turn('Please send to sam@example.com', 'sam@example.com', False)
        self.turn('Actually use corrected@example.com', 'corrected@example.com')
        self.assertEqual(self.rpc('get_conversation_context', {'inquiry_id': self.inquiry})['revision'], 0)
        self.assertEqual(delivery.recipient(self.c, priced['deal_id'])['email'], 'corrected@example.com')
        preview_status, preview = self.http('GET', '/preview/' + self.version, owner=True)
        self.assertEqual(preview_status, 200)
        status, result, payload = self.approve()
        self.assertEqual(status, 200, result)
        self.assertEqual(result['email_status'], 'accepted')
        self.assertEqual(len(self.mail), 1)
        self.assertEqual(self.mail[0]['To'], 'corrected@example.com')
        self.assertIn(result['url'], self.mail[0].get_content())
        buyer_status, buyer = self.http('GET', '/p/' + result['url'].split('/p/')[1])
        self.assertEqual(buyer_status, 200)
        self.assertIn(b'A warm espresso-led story', preview)
        self.assertIn(b'A warm espresso-led story', buyer)
        self.assertIn(b'data-proposal-design="initial"', buyer)
        self.assertGreater(self.c.execute('SELECT version_no FROM proposal_versions WHERE id=?', (self.version,)).fetchone()[0], 1)
        self.assertNotIn(b'corrected@example.com', buyer)
        status, raw = self.http('POST', '/api/approve-and-send', payload, owner=True)
        self.assertEqual(status, 200, raw)
        self.assertEqual(len(self.mail), 1)
        self.assertEqual(self.c.execute('SELECT count(*) FROM proposal_outbox').fetchone()[0], 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM approvals WHERE action='approve'").fetchone()[0], 1)
        self.assertNotIn(self.version, json.dumps(self.rpc('list_pending_work', {})['proposal_compositions']))

    def test_invalid_email_stale_recipient_and_unfinished_composition(self):
        self.start()
        for email in ('bad', 'x@example.com\r\nBcc: victim@example.com', 'a..b@example.com'):
            with self.assertRaises(ValueError):
                delivery.validate_email(email)
        self.prepare()
        self.turn('Send to first@example.com', 'first@example.com')
        old = self.review()
        self.turn('Correct it to second@example.com', 'second@example.com')
        status, result, _ = self.approve(old)
        self.assertEqual(status, 409)
        self.assertIn('review_mismatch', result['reasons'])
        draft = core.save_draft(self.c, self.version, 'An updated cafe direction.')
        self.version = draft
        self.rpc('check_draft', {'version_id': draft})
        status, result, _ = self.approve()
        self.assertEqual(status, 409)
        self.assertIn('composition_pending', result['reasons'])
        self.assertEqual(self.mail, [])

    def test_counter_composition_and_stale_selection(self):
        self.start()
        self.prepare()
        self.turn('Send to counter@example.com', 'counter@example.com')
        status, initial, _ = self.approve()
        self.assertEqual(status, 200)
        old_version = self.version
        token = initial['url'].split('/p/')[1]
        old_option = self.review()['option_ids'][0]
        request_text = 'Please replace the original reels with one original and two cutdowns; keep the shared shoot.'
        status, raw = self.http('POST', '/p/' + token + '/request-change', {'body': request_text, 'request_id': 'master-and-cutdowns'})
        self.assertEqual(status, 202, raw)
        changes = self.rpc('list_pending_work', {})['change_requests']
        change = next(item for item in changes if item['request_id'] == 'master-and-cutdowns')
        self.assertEqual(change['body'], request_text)
        # Commercial values remain the immutable engine result. This test
        # verifies orchestration, not QwenWork's actual narrative selection or
        # a claimed effort saving; detailed original/derivative pricing is
        # separately exercised by test_alternatives and test_estimating.
        revised = self.rpc('submit_change_offer', {'deal_id': change['deal_id'], 'request_id': change['request_id'],
            'version_id': old_version, 'offer_request': INQUIRY, 'evidence': {'raw_message': request_text}})
        evaluated = self.rpc('evaluate_offer', {'deal_id': change['deal_id'], 'offer_id': revised['offer_id']})
        source = evaluated['draft_version']['version_id']
        page = {'headline': 'Your revised cafe launch', 'introduction': 'The main story now leads the shorter derivative edits.',
                'sections': [{'title': 'What changed', 'body': 'The requested master and cutdowns replace the separate original stories. The shared shoot carries over.'}]}
        composed = self.rpc('compose_proposal_page', {'version_id': source, 'page': page})
        self.version = composed['version_id']
        self.rpc('check_draft', {'version_id': self.version})
        status, counter, _ = self.approve()
        self.assertEqual(status, 200, counter)
        self.assertEqual(len(self.mail), 2)
        self.assertIn('revised proposal', self.mail[-1]['Subject'])
        status, body = self.http('GET', '/p/' + counter['url'].split('/p/')[1])
        self.assertEqual(status, 200)
        self.assertIn(b'master and cutdowns', body)
        self.assertIn(b'data-proposal-design="counter"', body)
        self.assertIn(b'class="replacement"', body)
        self.assertNotIn(b'paid in two parts', body)
        self.assertEqual(self.http('GET', '/p/' + token)[0], 404)
        self.assertEqual(self.http('POST', '/p/' + token + '/accept', {'option_id': old_option})[0], 404)

    def test_stale_capacity_review_and_durable_composition_wake(self):
        import operations
        self.start()
        priced = self.prepare()
        self.turn('Send to stale@example.com', 'stale@example.com')
        review = self.review()
        with self.c:
            self.c.execute("INSERT INTO capacity_revisions(revision,created_at) VALUES (1,0)")
        status, result, _ = self.approve(review)
        self.assertEqual(status, 409)
        self.assertIn('review_mismatch', result['reasons'])
        self.assertIn('capacity_changed', result['reasons'])
        self.assertTrue(operations.event_completed(self.c, 'proposal_composition', {'version_id': priced['version_id']}))
        fresh = core.save_draft(self.c, self.version, 'Fresh creative direction for review.')
        self.assertFalse(operations.event_completed(self.c, 'proposal_composition', {'version_id': fresh}))

    def test_mail_rejection_retry_same_version_and_missing_credentials(self):
        self.start()
        self.prepare()
        self.turn('Send to sandbox@example.com', 'sandbox@example.com')
        review = self.review()
        with patch.dict(os.environ, {'BIDPILOT_SMTP_PASSWORD': ''}):
            status, result, payload = self.approve(review)
        self.assertEqual(status, 200)
        self.assertEqual(result['email_status'], 'failed')
        self.assertEqual(self.mail, [])
        with patch('delivery.smtplib.SMTP') as smtp:
            smtp.return_value.send_message.side_effect = smtplib.SMTPDataError(550, b'rejected')
            status, raw = self.http('POST', '/api/retry-email', {'version_id': self.version}, owner=True)
        self.assertEqual(json.loads(raw)['status'], 'failed')
        with patch('delivery.smtplib.SMTP') as smtp:
            smtp.return_value.send_message.side_effect = lambda m: self.mail.append(m) or {}
            status, raw = self.http('POST', '/api/retry-email', {'version_id': self.version}, owner=True)
        self.assertEqual(json.loads(raw)['status'], 'accepted')
        self.assertEqual(len(self.mail), 1)
        self.assertEqual(self.c.execute('SELECT count(*) FROM proposal_outbox').fetchone()[0], 1)
        self.assertIn(result['url'], self.mail[0].get_content())


if __name__ == '__main__':
    unittest.main()
