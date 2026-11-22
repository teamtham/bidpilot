"""Deterministic queue/contract tests, not live API verification."""
import json
import unittest
from unittest.mock import patch

import conversation
import core
import db
import qwenwork_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(':memory:')
        core.load_seed(self.c)
        self.c.executescript(runtime.SCHEMA)
        self.i = conversation.create_inquiry(self.c, {'text': 'Two shoot days', 'request_id': 'start'}, provider='qwenwork')['inquiry_id']

    def tearDown(self):
        self.c.close()

    def test_discovery_is_durable_and_idempotent(self):
        runtime.discover(self.c)
        runtime.discover(self.c)
        rows = self.c.execute('SELECT * FROM qwenwork_work').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'chat')
        self.assertEqual(json.loads(rows[0]['payload_json'])['sequence'], 1)

    def test_final_reply_commits_once(self):
        runtime.discover(self.c)
        row = self.c.execute('SELECT * FROM qwenwork_work').fetchone()
        answer = {'reply': 'What should the finished film show?', 'fact_changes': []}
        runtime.apply(self.c, row, answer)
        runtime.apply(self.c, row, answer)
        self.assertEqual(self.c.execute("SELECT count(*) FROM inquiry_turns WHERE sender='assistant'").fetchone()[0], 1)

    def test_correction_rejects_old_result(self):
        runtime.discover(self.c)
        row = self.c.execute('SELECT * FROM qwenwork_work').fetchone()
        conversation.add_message(self.c, self.i, {'text': 'Actually no filming', 'request_id': 'correction'})
        with self.assertRaisesRegex(ValueError, 'stale_revision'):
            runtime.apply(self.c, row, {'reply': 'What is the intended audience?', 'fact_changes': []})
        self.assertEqual(self.c.execute("SELECT count(*) FROM inquiry_turns WHERE sender='assistant'").fetchone()[0], 0)

    def test_unknown_commercial_completion_rejected(self):
        runtime.enqueue(self.c, 'brief:test', 'brief', self.i, {'expected_revision': 0})
        row = self.c.execute("SELECT * FROM qwenwork_work WHERE key='brief:test'").fetchone()
        with self.assertRaisesRegex(ValueError, 'invalid_commercial_answer'):
            runtime.apply(self.c, row, {'done': True})

    def test_all_remote_operations_are_disabled(self):
        runtime.enqueue(self.c, 'brief:test', 'brief', self.i, {'expected_revision': 0})
        row = self.c.execute("SELECT * FROM qwenwork_work WHERE key='brief:test'").fetchone()
        for name, arguments in [('get_scoping_history', {}), ('negotiation_payload', {'evaluation_id': 'other'}),
                                ('evaluate_offer', {'offer_id': 'other'}), ('get_reference_evidence', {'analysis_id': 'other'})]:
            with self.subTest(name=name), patch('mcp_server.handle') as tool:
                with self.assertRaisesRegex(ValueError, 'invalid_commercial_answer'):
                    runtime.apply(self.c, row, {'operations': [{'name': name, 'arguments': arguments}]})
                tool.assert_not_called()
        text = runtime.prompt(self.c, row)
        material = json.loads(text.rsplit('\n', 1)[1])
        self.assertNotIn('tools', material)
        self.assertNotIn('pricing_history', material)

    def test_chat_prompt_excludes_private_context(self):
        runtime.discover(self.c)
        row = self.c.execute('SELECT * FROM qwenwork_work').fetchone()
        ctx = conversation.context(self.c, {'inquiry_id': self.i})
        ctx['pricing_history'] = [{'private': 'OTHER_CLIENT_SECRET'}]
        ctx['proposal_email'] = {'email': 'PRIVATE_RECIPIENT'}
        with patch.object(conversation, 'context', return_value=ctx):
            text = runtime.prompt(self.c, row)
        self.assertNotIn('OTHER_CLIENT_SECRET', text)
        self.assertNotIn('PRIVATE_RECIPIENT', text)
        self.assertNotIn('pricing_history', text)

    def test_unknown_dispatch_is_visible_not_replayed(self):
        runtime.discover(self.c)
        with self.c:
            self.c.execute("UPDATE qwenwork_work SET status='dispatching'")
        client = unittest.mock.Mock()
        runtime.tick(client, self.c)
        client.api.assert_not_called()
        row = self.c.execute('SELECT * FROM qwenwork_work').fetchone()
        self.assertEqual((row['status'], row['error']), ('blocked', 'dispatch_outcome_unknown'))

    def test_old_queue_is_not_timed_out_before_dispatch(self):
        runtime.discover(self.c)
        with self.c:
            self.c.execute('UPDATE qwenwork_work SET created_at=0')
        client = unittest.mock.Mock()
        client.api.return_value = {'session_id': 'synthetic', 'first_turn_id': 'turn'}
        with patch.object(runtime, 'prompt', return_value='synthetic'):
            runtime.tick(client, self.c)
        self.assertEqual(self.c.execute('SELECT status FROM qwenwork_work').fetchone()[0], 'running')
        client.api.assert_called_once()

    def test_operation_payload_and_exhausted_budget_have_no_side_effects(self):
        runtime.discover(self.c)
        row = self.c.execute('SELECT * FROM qwenwork_work').fetchone()
        with self.assertRaisesRegex(ValueError, 'invalid_chat_answer'), patch('mcp_server.handle') as tool:
            runtime.apply(self.c, row, {'operations': [{'name': 'check_draft', 'arguments': {'version_id': 'other'}}]})
            tool.assert_not_called()
        with self.c:
            self.c.execute("UPDATE qwenwork_work SET status='running',session_id='synthetic',turn_id='turn',attempts=300")
        client = unittest.mock.Mock()
        runtime.tick(client, self.c)
        client.api.assert_not_called()
        self.assertEqual(self.c.execute('SELECT status FROM qwenwork_work').fetchone()[0], 'blocked')

    def test_runtime_exception_does_not_stop_draft_polling(self):
        import os
        import tempfile
        import threading
        from pathlib import Path
        import qwenwork_api
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'app.db'
            c = db.connect(path)
            c.executescript(qwenwork_api.SCHEMA)
            c.execute("INSERT INTO qwenwork_sessions(session_id,version_id,status,created_at) VALUES ('synthetic','version','queued',0)")
            c.commit()
            c.close()
            stop = threading.Event()
            client = qwenwork_api.Client()
            with patch.dict(os.environ, {'BIDPILOT_QWENWORK_EVENT_RUNTIME': '1'}), patch.object(runtime, 'tick', side_effect=RuntimeError('private')), patch.object(client, 'follow_once', side_effect=lambda *args: stop.set()) as follow:
                client.worker(path, stop)
            follow.assert_called_once()

    def test_expired_session_requeues_without_unknown_post_replay(self):
        runtime.discover(self.c)
        with self.c:
            self.c.execute('INSERT INTO qwenwork_inquiry_sessions VALUES (?,?,?)', (self.i, 'expired', 9999999999))
        client = unittest.mock.Mock()
        client.api.side_effect = ValueError('http_410')
        with patch.object(runtime, 'prompt', return_value='synthetic'):
            runtime.tick(client, self.c)
        self.assertEqual(self.c.execute('SELECT status FROM qwenwork_work').fetchone()[0], 'queued')
        self.assertEqual(self.c.execute('SELECT count(*) FROM qwenwork_inquiry_sessions').fetchone()[0], 0)
        client.api.assert_called_once()

    def test_two_quick_messages_dispatch_only_latest_snapshot(self):
        runtime.discover(self.c)
        conversation.add_message(self.c, self.i, {'text': 'Actually supplied footage', 'request_id': 'quick'})
        client = unittest.mock.Mock()
        runtime.tick(client, self.c)
        client.api.assert_not_called()
        self.assertEqual(self.c.execute('SELECT status FROM qwenwork_work ORDER BY created_at LIMIT 1').fetchone()[0], 'stale')

    def test_active_inquiry_turn_serializes_followup(self):
        runtime.discover(self.c)
        with self.c:
            self.c.execute("UPDATE qwenwork_work SET status='running',session_id='synthetic',turn_id='turn',due=9999999999")
        runtime.enqueue(self.c, 'later', 'chat', self.i, {'revision': 0, 'sequence': 1})
        client = unittest.mock.Mock()
        runtime.tick(client, self.c)
        client.api.assert_not_called()

    def test_rate_limit_has_sanitized_recovery_category(self):
        client = unittest.mock.Mock()
        client.api.side_effect = ValueError('http_429')
        with patch.object(runtime, 'prompt', return_value='synthetic'):
            runtime.tick(client, self.c)
        self.assertEqual(self.c.execute('SELECT error FROM qwenwork_work').fetchone()[0], 'http_429')

    def test_failed_api_keeps_saved_message(self):
        class Client:
            def api(self, *args):
                raise ValueError('unexpected_html_response')
        with patch.object(runtime, 'prompt', return_value='synthetic'):
            runtime.tick(Client(), self.c)
        self.assertEqual(self.c.execute('SELECT status FROM qwenwork_work').fetchone()[0], 'blocked')
        self.assertEqual(self.c.execute("SELECT count(*) FROM inquiry_turns WHERE sender='buyer'").fetchone()[0], 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM inquiry_turns WHERE sender='assistant'").fetchone()[0], 0)
