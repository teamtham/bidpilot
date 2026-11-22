"""QwenWork OpenAPI pipeline regressions: Drive uploads, client-safe portfolio,
reference cues, comparable validation and compose gating. Fake transports only;
no live API calls, no real deals."""
import base64
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import conversation
import core
import db
import portfolio
import qwenwork_api as qw
import qwenwork_commercial as commercial
import qwenwork_runtime as runtime
import studio_assistant
from test_http import INQUIRY, RAW1


class DriveClient(qw.Client):
    """Real drive_upload logic over a canned direct-upload transport."""

    def __init__(self):
        super().__init__(token_path=Path(tempfile.mkdtemp()) / 'oauth.json')
        self.calls = []
        self.puts = []

    def api(self, path, method='GET', data=None, headers=None):
        self.calls.append((path, method, data, headers))
        if path == '/v1/drive/direct-uploads':
            return {'session_id': 'upload-session',
                    'upload': {'url': 'https://upload.example/put',
                               'headers': {'Content-Type': 'application/octet-stream'}}}
        if path.endswith('/complete'):
            return {'file_id': 'file-' + str(len(self.calls))}
        raise AssertionError('unexpected path ' + path)

    def _put_bytes(self, url, headers, payload):
        self.puts.append((url, dict(headers), bytes(payload)))


class SessionClient(DriveClient):
    """Canned cloud session: one scripted final message per created turn."""

    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)
        self.prompts = []
        self.turns = {}

    def _new_turn(self):
        tid = 't' + str(len(self.turns))
        self.turns[tid] = self.answers.pop(0) if self.answers else '{}'
        return tid

    def api(self, path, method='GET', data=None, headers=None):
        if path == '/v1/sessions' and method == 'POST':
            return {'session_id': 'sess-1', 'first_turn_id': self._new_turn()}
        if path == '/v1/sessions/sess-1/prompt' and method == 'POST':
            self.prompts.append(data['text'])
            return {'turn_id': self._new_turn()}
        match = re.fullmatch(r'/v1/sessions/sess-1/turns/(t\d+)', path)
        if match and method == 'GET':
            return {'status': 'completed', 'final_message': self.turns[match.group(1)]}
        return super().api(path, method, data, headers)


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = db.connect(Path(self.tmp.name) / 'app.db')
        core.load_seed(self.c)
        self.c.executescript(runtime.SCHEMA)
        self.c.executescript(qw.SCHEMA)
        portfolio.seed(self.c)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def inquiry(self, text=RAW1):
        return conversation.create_inquiry(self.c, {'text': text, 'request_id': 'start'},
                                           provider='qwenwork')['inquiry_id']


class DriveUploadTests(PipelineCase):
    def test_upload_idempotent_per_sha_and_path(self):
        local = Path(self.tmp.name) / 'clip.mp4'
        local.write_bytes(b'synthetic video bytes')
        client = DriveClient()
        first = client.drive_upload(self.c, local, '/bidpilot/portfolio/clips/a/clip.mp4', 'video/mp4')
        calls_after_first = len(client.calls)
        again = client.drive_upload(self.c, local, '/bidpilot/portfolio/clips/a/clip.mp4', 'video/mp4')
        self.assertEqual(first, again)
        self.assertEqual(len(client.calls), calls_after_first)  # no re-upload
        self.assertEqual(len(client.puts), 1)
        # Same bytes at a new path is a new Drive object.
        other = client.drive_upload(self.c, local, '/bidpilot/portfolio/clips/b/clip.mp4', 'video/mp4')
        self.assertNotEqual(first, other)
        # Changed bytes at the same path is a new upload (paths never overwrite).
        local.write_bytes(b'different bytes')
        changed = client.drive_upload(self.c, local, '/bidpilot/portfolio/clips/a/clip.mp4', 'video/mp4')
        self.assertNotIn(changed, (first, other))
        rows = self.c.execute('SELECT COUNT(*) FROM qwenwork_drive_files').fetchone()[0]
        self.assertEqual(rows, 3)

    def test_initiation_uses_stable_idempotency_key(self):
        local = Path(self.tmp.name) / 'clip.mp4'
        local.write_bytes(b'synthetic video bytes')
        client = DriveClient()
        client.drive_upload(self.c, local, '/bidpilot/inquiries/i/a.mp4', 'video/mp4')
        init = client.calls[0]
        self.assertEqual(init[0], '/v1/drive/direct-uploads')
        self.assertEqual(init[1], 'POST')
        self.assertEqual(init[2]['path'], '/bidpilot/inquiries/i/a.mp4')
        self.assertEqual(init[2]['size'], len(b'synthetic video bytes'))
        key = init[3]['Idempotency-Key']
        self.assertTrue(key)
        self.assertEqual(client.puts[0][1], {'Content-Type': 'application/octet-stream'})
        self.assertTrue(client.calls[-1][0].endswith('/complete'))

    def test_drive_path_scope_enforced(self):
        local = Path(self.tmp.name) / 'clip.mp4'
        local.write_bytes(b'x')
        with self.assertRaisesRegex(qw.APIError, 'invalid_drive_path'):
            DriveClient().drive_upload(self.c, local, '/elsewhere/clip.mp4', 'video/mp4')

    def test_complete_response_file_object_id(self):
        class FileObjectClient(DriveClient):
            def api(self, path, method='GET', data=None, headers=None):
                if path.endswith('/complete'):
                    self.calls.append((path, method, data, headers))
                    return {'file': {'file_id': 'fid-123', 'path': '/bidpilot/x'}}
                return super().api(path, method, data, headers)
        local = Path(self.tmp.name) / 'clip.mp4'
        local.write_bytes(b'synthetic video bytes')
        self.assertEqual(FileObjectClient().drive_upload(
            self.c, local, '/bidpilot/inquiries/i/a.mp4', 'video/mp4'), 'fid-123')

    def test_completed_init_replay_skips_put(self):
        class ReplayedClient(DriveClient):
            def api(self, path, method='GET', data=None, headers=None):
                if path == '/v1/drive/direct-uploads':
                    self.calls.append((path, method, data, headers))
                    return {'session_id': 'upload-session', 'status': 'completed'}
                return super().api(path, method, data, headers)
        local = Path(self.tmp.name) / 'clip.mp4'
        local.write_bytes(b'synthetic video bytes')
        client = ReplayedClient()
        fid = client.drive_upload(self.c, local, '/bidpilot/inquiries/i/a.mp4', 'video/mp4')
        self.assertTrue(fid)
        self.assertEqual(len(client.puts), 0)  # bytes already stored remotely


class PortfolioSyncTests(PipelineCase):
    FORBIDDEN_KEY = re.compile(r'price|cost|fee|rate|hour|floor|capacity|budget|client|note|margin', re.I)

    def walk(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key, item
                yield from self.walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from self.walk(item)

    def test_manifest_has_no_private_fields(self):
        manifest = runtime.portfolio_manifest(self.c)
        self.assertTrue(manifest['assets'])
        for key, value in self.walk(manifest):
            self.assertNotRegex(key, self.FORBIDDEN_KEY)
        blob = json.dumps(manifest)
        for name in core.other_client_names(self.c, ''):
            self.assertNotIn(name, blob)
        self.assertNotRegex(blob, r'S\$|SGD\s*\d|\d+\s*(?:hours|hrs)')
        for item in manifest['assets']:
            self.assertTrue(item['clip_drive_path'].startswith('/bidpilot/portfolio/clips/'))
            self.assertIn('project_id', item)

    def test_sync_uploads_clips_and_manifest_once(self):
        client = DriveClient()
        path = runtime.sync_portfolio(client, self.c)
        self.assertTrue(path.startswith('/bidpilot/portfolio/'))
        self.assertTrue(path.endswith('/portfolio.json'))
        shared = self.c.execute('SELECT COUNT(*) FROM portfolio_assets WHERE shared=1').fetchone()[0]
        self.assertEqual(len(client.puts), shared + 1)  # clips + manifest
        puts_after = len(client.puts)
        self.assertEqual(runtime.sync_portfolio(client, self.c), path)
        self.assertEqual(len(client.puts), puts_after)  # unchanged data: no re-upload
        self.assertEqual(runtime.synced_portfolio_path(self.c), path)

    def test_portfolio_change_republishes_manifest_not_clips(self):
        client = DriveClient()
        first = runtime.sync_portfolio(client, self.c)
        shared = self.c.execute('SELECT COUNT(*) FROM portfolio_assets WHERE shared=1').fetchone()[0]
        self.assertEqual(len(client.puts), shared + 1)
        with self.c:
            self.c.execute("UPDATE portfolio_assets SET title='New title' WHERE id='cafe'")
        second = runtime.sync_portfolio(client, self.c)
        self.assertNotEqual(first, second)
        self.assertEqual(len(client.puts), shared + 2)  # only the manifest re-uploaded
        self.assertEqual(runtime.synced_portfolio_path(self.c), second)

    def test_private_assets_never_synced(self):
        with self.c:
            self.c.execute("UPDATE portfolio_assets SET shared=0 WHERE id='cafe'")
        client = DriveClient()
        runtime.sync_portfolio(client, self.c)
        manifest = runtime.portfolio_manifest(self.c)
        self.assertNotIn('cafe', [a['asset_id'] for a in manifest['assets']])
        self.assertFalse(any('cafe' in put[2].decode('utf-8', 'replace') for put in client.puts[-1:]))


class ReferencePipelineTests(PipelineCase):
    def setUp(self):
        super().setUp()
        self.i = self.inquiry('Cafe launch promo. Here is a reference clip we like.')
        raw = b'synthetic reference bytes'
        upload = conversation.upload(self.c, self.i, {
            'filename': 'espresso.mp4', 'mime': 'video/mp4',
            'data': base64.b64encode(raw).decode()})
        self.asset, self.run = upload['asset_id'], upload['analysis_id']
        with self.c:
            self.c.execute("UPDATE reference_runs SET status='done', result_json=? WHERE id=?",
                           (json.dumps({'duration': 12.0}), self.run))
            self.c.execute("INSERT INTO media_notifications(run_id,inquiry_id) VALUES (?,?)",
                           (self.run, self.i))
        runtime.discover(self.c)
        self.row = self.c.execute("SELECT * FROM qwenwork_work WHERE kind='chat'").fetchone()

    def test_prompt_uploads_reference_and_names_drive_path(self):
        client = DriveClient()
        text = runtime.prompt(self.c, self.row, client)
        expected = '/bidpilot/inquiries/' + self.i + '/' + self.asset + '.mp4'
        self.assertIn(expected, text)
        self.assertIn('12.0', text)
        self.assertEqual(len(client.puts), 1)
        material = json.loads(text.rsplit('\n', 1)[1])
        self.assertEqual(material['media_analysis_id'], self.run)
        self.assertEqual(material['reference']['drive_path'], expected)
        self.assertNotIn('pricing_history', material)

    def test_observations_saved_as_linked_cues(self):
        client = DriveClient()
        runtime.prompt(self.c, self.row, client)
        answer = {'reply': 'Which moments of the reference should we keep?', 'fact_changes': [],
                  'reference_observations': [
                      {'start': 0, 'end': 2.5, 'body': 'Warm espresso pour in close-up',
                       'uncertainty': 'Sampled frames only'}]}
        runtime.apply(self.c, self.row, answer)
        agent = self.c.execute("SELECT * FROM reference_observations WHERE run_id=? AND kind='agent'", (self.run,)).fetchall()
        cues = self.c.execute("SELECT * FROM reference_observations WHERE run_id=? AND kind='production_cue'", (self.run,)).fetchall()
        self.assertEqual(len(agent), 1)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]['supersedes'], agent[0]['id'])
        self.assertEqual(cues[0]['method'], 'QwenWork agent cue')
        self.assertEqual((cues[0]['start'], cues[0]['end']), (0.0, 2.5))
        self.assertEqual(self.c.execute("SELECT status FROM media_notifications WHERE run_id=?", (self.run,)).fetchone()[0], 'done')
        # Replay is idempotent: no duplicate evidence, same receipt.
        runtime.apply(self.c, self.row, answer)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM reference_observations WHERE run_id=? AND kind='agent'", (self.run,)).fetchone()[0], 1)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM inquiry_turns WHERE sender='assistant'").fetchone()[0], 1)

    def test_timestamps_beyond_duration_rejected(self):
        client = DriveClient()
        runtime.prompt(self.c, self.row, client)
        answer = {'reply': 'Which moments should we keep?', 'fact_changes': [],
                  'reference_observations': [{'start': 10, 'end': 99, 'body': 'Late moment', 'uncertainty': 'x'}]}
        with self.assertRaisesRegex(ValueError, 'invalid_reference_observation'):
            runtime.apply(self.c, self.row, answer)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM reference_observations WHERE kind='agent'").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM inquiry_turns WHERE sender='assistant'").fetchone()[0], 0)

    def test_unsafe_cue_rejected(self):
        client = DriveClient()
        runtime.prompt(self.c, self.row, client)
        other = sorted(core.other_client_names(self.c, ''))[0]
        answer = {'reply': 'Which moments should we keep?', 'fact_changes': [],
                  'reference_observations': [{'start': 0, 'end': 1, 'body': 'Like ' + other + ' did', 'uncertainty': 'x'}]}
        with self.assertRaisesRegex(ValueError, 'unsafe_reference_cue'):
            runtime.apply(self.c, self.row, answer)

    def test_cue_limits_match_schema(self):
        client = DriveClient()
        runtime.prompt(self.c, self.row, client)
        answer = {'reply': 'Which moments should we keep?', 'fact_changes': [],
                  'reference_observations': [{'start': 0, 'end': 1, 'body': 'x' * 2001, 'uncertainty': 'x'}]}
        with self.assertRaisesRegex(ValueError, 'invalid_reference_observation'):
            runtime.apply(self.c, self.row, answer)

    def test_media_job_survives_prior_text_commit(self):
        # A text turn committed after discovery bumps the revision; the media
        # job must refresh to it instead of going stale.
        conversation.commit(self.c, {'inquiry_id': self.i, 'through_sequence': 1,
                                     'expected_revision': 0, 'request_id': 'text-first',
                                     'reply': 'Which moments of the reference should we keep?',
                                     'fact_changes': []})
        answer = json.dumps({'reply': 'Which moments should we keep?', 'fact_changes': [],
                             'reference_observations': [{'start': 0, 'end': 2,
                                                         'body': 'Warm espresso pour in close-up',
                                                         'uncertainty': 'Sampled frames only'}]})
        client = SessionClient([answer])
        for _ in range(2):
            with self.c:
                self.c.execute('UPDATE qwenwork_work SET due=0')
            runtime.tick(client, self.c)
        row = self.c.execute('SELECT * FROM qwenwork_work WHERE key=?', (self.row['key'],)).fetchone()
        self.assertEqual(row['status'], 'done')
        self.assertEqual(self.c.execute(
            "SELECT COUNT(*) FROM reference_observations WHERE run_id=? AND kind='production_cue'",
            (self.run,)).fetchone()[0], 1)


class ContractRepairTests(PipelineCase):
    BAD = json.dumps({'reply': 'What should the finished film show?', 'fact_changes': [
        {'subject': 'production.deliverable_matrix', 'value': '3 x 30s IG promos', 'status': 'confirmed'}]})
    GOOD = json.dumps({'reply': 'What should the finished film show?', 'fact_changes': [
        {'subject': 'production.deliverable_matrix',
         'value': 'three 30-second Instagram promo videos', 'status': 'confirmed'}]})

    def setUp(self):
        super().setUp()
        self.i = self.inquiry(RAW1)
        runtime.discover(self.c)
        self.key = self.c.execute("SELECT key FROM qwenwork_work WHERE kind='chat'").fetchone()[0]

    def tick(self, client):
        with self.c:
            self.c.execute('UPDATE qwenwork_work SET due=0')
        runtime.tick(client, self.c)

    def row(self):
        return self.c.execute('SELECT * FROM qwenwork_work WHERE key=?', (self.key,)).fetchone()

    def test_contract_error_repaired_with_feedback(self):
        client = SessionClient([self.BAD, self.GOOD])
        self.tick(client)  # session created
        self.tick(client)  # paraphrased fact rejected -> feedback turn
        self.assertEqual(len(client.prompts), 1)
        feedback = json.loads(client.prompts[0])
        self.assertEqual(feedback['contract_error'], 'ungrounded_fact')
        self.assertEqual(feedback['latest_buyer_message'], RAW1)
        self.assertEqual(self.row()['status'], 'running')
        self.assertEqual(self.row()['contract_retries'], 1)
        self.tick(client)  # corrected answer accepted
        self.assertEqual(self.row()['status'], 'done')
        value = self.c.execute('SELECT value_json FROM inquiry_facts WHERE inquiry_id=?', (self.i,)).fetchone()[0]
        self.assertEqual(json.loads(value), 'three 30-second Instagram promo videos')

    def test_repair_bounded_then_blocked(self):
        client = SessionClient([self.BAD] * 3)
        for _ in range(5):
            self.tick(client)
        self.assertEqual(self.row()['status'], 'blocked')
        self.assertEqual(self.row()['error'], 'ungrounded_fact')
        self.assertEqual(len(client.prompts), 2)  # at most two repair turns
        self.assertEqual(self.c.execute(
            "SELECT COUNT(*) FROM inquiry_turns WHERE sender='assistant'").fetchone()[0], 0)


class ComparableValidationTests(PipelineCase):
    def test_unknown_ids_dropped_and_recorded(self):
        kept, similar, dropped = commercial.validate_comparables(self.c, {
            'comparable_ids': [{'id': 'P-041', 'why': 'cafe promo, same format'},
                               {'id': 'P-999', 'why': 'invented'}],
            'similar_work_asset_id': 'cafe'})
        self.assertEqual([k['id'] for k in kept], ['P-041'])
        self.assertEqual(similar, 'cafe')
        self.assertEqual(dropped, [{'id': 'P-999', 'reason': 'unknown_project'}])

    def test_unknown_asset_and_over_limit_dropped(self):
        import estimating
        known = sorted({r['project_id'] for r in estimating.get_history(self.c)})
        kept, similar, dropped = commercial.validate_comparables(self.c, {
            'comparable_ids': [{'id': pid, 'why': 'x'} for pid in known[:commercial.MAX_COMPARABLES + 2]],
            'similar_work_asset_id': 'not-an-asset'})
        self.assertEqual(len(kept), commercial.MAX_COMPARABLES)
        self.assertIsNone(similar)
        self.assertIn({'id': 'not-an-asset', 'reason': 'unknown_asset'}, dropped)
        self.assertTrue(any(d['reason'] == 'over_limit' for d in dropped))

    def test_malformed_selection_rejected(self):
        with self.assertRaisesRegex(ValueError, 'invalid_comparable_selection'):
            commercial.validate_comparables(self.c, {'comparable_ids': 'P-041'})
        with self.assertRaisesRegex(ValueError, 'invalid_comparable_selection'):
            commercial.validate_comparables(self.c, {'comparable_ids': [{'why': 'no id'}]})

    def submitted_brief(self):
        inquiry = self.inquiry()
        runtime.discover(self.c)
        row = self.c.execute("SELECT * FROM qwenwork_work WHERE kind='chat'").fetchone()
        runtime.apply(self.c, row, {'reply': 'What should the finished film show?', 'fact_changes': []})
        revision = self.c.execute('SELECT revision,processed_seq FROM inquiries WHERE id=?', (inquiry,)).fetchone()
        studio_assistant.submit_brief(self.c, inquiry, revision['revision'], revision['processed_seq'])
        runtime.discover(self.c)
        return inquiry, self.c.execute("SELECT * FROM qwenwork_work WHERE kind='brief'").fetchone()

    def test_brief_prompt_offers_synced_portfolio(self):
        inquiry, row = self.submitted_brief()
        client = DriveClient()
        text = runtime.prompt(self.c, row, client)
        path = runtime.synced_portfolio_path(self.c)
        self.assertIn(path, text)
        self.assertIn('comparable_ids', text)
        material = json.loads(text.rsplit('\n', 1)[1])
        self.assertNotIn('pricing_history', material)
        # Without a client and without a prior sync the safe fallback remains.
        self.c.execute('DELETE FROM qwenwork_drive_files')
        self.c.commit()
        fallback = runtime.prompt(self.c, row)
        self.assertIn('comparable_ids must be empty', fallback)

    def test_engine_receives_only_validated_comparables(self):
        inquiry, row = self.submitted_brief()
        request = {k: v for k, v in INQUIRY.items() if k != 'pricing_mode'}
        answer = {'offer_request': request,
                  'comparable_ids': [{'id': 'P-041', 'why': 'cafe launch promo'},
                                     {'id': 'P-999', 'why': 'invented project'}],
                  'similar_work_asset_id': 'cafe'}
        commercial.apply(self.c, row, answer)
        deal = self.c.execute('SELECT deal_id FROM inquiries WHERE id=?', (inquiry,)).fetchone()['deal_id']
        offer = self.c.execute('SELECT comparable_ids_json FROM offers WHERE deal_id=? ORDER BY round DESC LIMIT 1', (deal,)).fetchone()
        self.assertEqual(json.loads(offer['comparable_ids_json']), ['P-041'])
        selection = self.c.execute('SELECT * FROM qwenwork_selections WHERE inquiry_id=?', (inquiry,)).fetchone()
        self.assertEqual(selection['similar_work_asset_id'], 'cafe')
        self.assertIn('P-999', selection['dropped_json'])
        steps = self.c.execute("SELECT step_key,\"trigger\" FROM run_steps WHERE deal_id=? ORDER BY rowid", (deal,)).fetchall()
        api_steps = {s['step_key'] for s in steps if s['trigger'] == 'api'}
        self.assertTrue({'intake', 'scoping', 'engine'} <= api_steps)


class ComposePortfolioTests(PipelineCase):
    def setUp(self):
        super().setUp()
        self.i = self.inquiry()
        deal, offer, _ = core.claim_or_resume(self.c, 'site', self.i, INQUIRY['offer']['client'], RAW1, INQUIRY)
        _, self.version = core.evaluate(self.c, deal, offer)
        with self.c:
            self.c.execute('UPDATE inquiries SET deal_id=?,processed_seq=1 WHERE id=?', (deal, self.i))
            self.c.execute('INSERT INTO offer_briefs VALUES (?,?,?)', (offer, self.i, 0))
        self.deal = deal

    def compose_row(self, revision=0):
        runtime.enqueue(self.c, 'compose:' + self.version, 'compose', self.i,
                        {'version_id': self.version, 'expected_revision': revision})
        return self.c.execute("SELECT * FROM qwenwork_work WHERE kind='compose'").fetchone()

    def select(self, comparable='P-041', similar='cafe'):
        self.c.executescript(commercial.SELECTIONS_SCHEMA)
        with self.c:
            self.c.execute('INSERT OR REPLACE INTO qwenwork_selections VALUES (?,?,?,?,?,?)',
                           (self.i, 0, json.dumps([{'id': comparable, 'why': 'match'}]), similar, '[]', 0))

    def page(self, asset_id):
        return {'page': {'headline': 'Your cafe launch', 'introduction': 'A warm launch film.',
                         'sections': [], 'portfolio': [
                             {'title': 'Opening morning', 'description': 'Warm cafe launch style.', 'asset_id': asset_id}]}}

    def test_validated_clip_allowed_and_checked(self):
        self.select()
        row = self.compose_row()
        outcome = commercial.apply(self.c, row, self.page('cafe'))
        saved = self.c.execute('SELECT page_json,outbound_status FROM proposal_versions WHERE id=?', (outcome['version_id'],)).fetchone()
        self.assertIn('cafe', saved['page_json'])
        self.assertIn(saved['outbound_status'], ('passed', 'blocked'))
        steps = self.c.execute("SELECT step_key FROM run_steps WHERE deal_id=? AND \"trigger\"='api'", (self.deal,)).fetchall()
        self.assertIn('compose', [s['step_key'] for s in steps])
        self.assertIn('outbound', [s['step_key'] for s in steps])

    def test_comparable_project_clip_allowed(self):
        self.select(comparable='P-041', similar=None)
        row = self.compose_row()
        outcome = commercial.apply(self.c, row, self.page('cafe'))  # cafe asset belongs to P-041
        self.assertTrue(outcome['version_id'])

    def test_unvalidated_clip_rejected(self):
        self.select()
        row = self.compose_row()
        with self.assertRaisesRegex(ValueError, 'unverified_portfolio_selection'):
            commercial.apply(self.c, row, self.page('bakery'))

    def test_no_selection_blocks_any_portfolio(self):
        row = self.compose_row()
        with self.assertRaisesRegex(ValueError, 'unverified_portfolio_selection'):
            commercial.apply(self.c, row, self.page('cafe'))

    def test_empty_portfolio_always_allowed(self):
        row = self.compose_row()
        answer = {'page': {'headline': 'Your cafe launch', 'introduction': 'A warm launch film.',
                           'sections': [], 'portfolio': []}}
        outcome = commercial.apply(self.c, row, answer)
        self.assertTrue(outcome['version_id'])


class RuntimeSwitchTests(PipelineCase):
    def test_session_named_for_founder_visibility(self):
        inquiry = self.inquiry()
        self.assertTrue(runtime.session_name(self.c, inquiry).startswith('BidPilot · '))
        runtime.discover(self.c)
        client = unittest.mock.Mock()
        client.api.return_value = {'session_id': 'synthetic', 'first_turn_id': 'turn'}
        runtime.tick(client, self.c)
        data = client.api.call_args.args[2]
        self.assertEqual(data['name'], runtime.session_name(self.c, inquiry))

    def test_runtime_off_when_env_unset(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'app.db'
            c = db.connect(path)
            c.executescript(qw.SCHEMA)
            c.execute("INSERT INTO qwenwork_sessions(session_id,version_id,status,created_at) VALUES ('synthetic','version','queued',0)")
            c.commit()
            c.close()
            stop = threading.Event()
            client = qw.Client()
            env = {k: v for k, v in os.environ.items() if k != 'BIDPILOT_QWENWORK_EVENT_RUNTIME'}
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(runtime, 'tick') as tick, \
                 patch.object(runtime, 'sync_portfolio') as sync, \
                 patch.object(client, 'follow_once', side_effect=lambda *args: stop.set()) as follow:
                client.worker(path, stop)
            tick.assert_not_called()
            sync.assert_not_called()
            follow.assert_called_once()


if __name__ == '__main__':
    unittest.main()