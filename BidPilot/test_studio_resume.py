"""Studio resolution through in-process owner HTTP and private-chat MCP."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import core
import db
from test_http import INQUIRY
import test_studio_journey


class StudioResume(test_studio_journey.Journey):
    # Reuse the socket harness, not its unrelated media/lifecycle tests.
    test_inquiry_to_confirmed_booking = None
    test_conversation_scoping_to_detailed_proposal = None
    test_configured_buyer_origin_is_separate_from_founder = None

    def request(self):
        request = copy.deepcopy(INQUIRY)
        request.pop('pricing_mode', None)
        request['production_facts'] = {'production.original_edits': 1,
                                       'production.cutdowns': 2,
                                       'production.clips_preselected': True}
        return request

    def post(self, path, body):
        status, raw = self.http('POST', path, body, True)
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def register(self):
        self.post('/api/estimating/promo-template', {
            'version': 'resume-test', 'basis_date': '2026-11-21',
            'note': 'Synthetic producer approval',
            'supported': {'editing_level': 'standard', 'graphics': 'basic_titles'},
            'rate': {'cost_rate_cents': 4500, 'sell_rate_cents': 9000}, 'buffer_pct': 20,
            'hours': {'prep': 1, 'shoot': 5, 'ingest': 1, 'review_selected': 1,
                      'review_per_footage_minute': .05, 'original_edit': 4, 'cutdown': 1,
                      'captions': .5, 'review_round': .5, 'delivery': .25}})
        return self.post('/api/estimating/availability', {
            'resource': 'promo_producer', 'start_at': '2026-11-23T09:00',
            'end_at': '2026-12-11T17:00'})

    def verdict(self, evaluation):
        conn = db.connect(self.path)
        try:
            return conn.execute('SELECT verdict FROM evaluations WHERE id=?', (evaluation,)).fetchone()[0]
        finally:
            conn.close()

    def test_structured_resolution_converges(self):
        payload = {'source': 'manual', 'source_id': 'studio-resume', 'offer_request': self.request()}
        claimed = self.post('/api/import', payload)
        original = self.post('/api/evaluate', claimed)
        self.assertEqual(self.verdict(original['eval_id']), 'clarify')
        self.assertEqual(self.post('/api/evaluate', claimed), original)
        registered = self.register()
        self.assertEqual(registered['studio_resumes'][0]['deal_id'], claimed['deal_id'])
        status, raw = self.http('GET', '/api/estimating/pending', owner=True)
        self.assertEqual(status, 200, raw)
        self.assertEqual(len(json.loads(raw)['studio_resumes']), 1)
        status, _ = self.http('POST', '/api/estimating/resume', claimed)
        self.assertEqual(status, 403)
        def retry(_):
            conn = db.connect(self.path)
            try:
                return core.evaluate(conn, claimed['deal_id'], claimed['offer_id'])
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(retry, range(8)))
        self.assertEqual(len(set(results)), 1)
        fresh, version = results[0]
        self.assertNotEqual(fresh, original['eval_id'])
        self.assertEqual(self.verdict(fresh), 'accept')
        self.assertEqual(self.verdict(original['eval_id']), 'clarify')
        resumed = self.post('/api/estimating/resume', claimed)
        self.assertEqual(resumed['evaluation_id'], fresh)
        duplicate = self.post('/api/import', payload)
        self.assertEqual(duplicate['deal_id'], claimed['deal_id'])
        self.assertEqual(self.post('/api/evaluate', duplicate)['eval_id'], fresh)
        self.post('/api/estimating/availability', {'resource': 'unrelated',
            'start_at': '2026-11-23T09:00', 'end_at': '2026-12-11T17:00'})
        self.post('/api/estimating/availability', {'resource': 'promo_producer',
            'start_at': '2027-01-01T09:00', 'end_at': '2027-02-01T17:00'})
        self.assertEqual(retry(0), (fresh, version))
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute('SELECT count(*) FROM evaluations').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT count(*) FROM resource_allocations').fetchone()[0], 0)
        finally:
            conn.close()

    def test_existing_clarification_without_fingerprint_resumes(self):
        claimed = self.post('/api/import', {'source': 'manual', 'source_id': 'old-studio-resume',
                                            'offer_request': self.request()})
        original = self.post('/api/evaluate', claimed)
        conn = db.connect(self.path)
        conn.execute('DELETE FROM studio_evaluation_inputs')
        conn.commit()
        conn.close()
        registered = self.register()
        self.assertEqual(len(registered['studio_resumes']), 1)
        fresh = self.post('/api/estimating/resume', claimed)
        self.assertNotEqual(fresh['evaluation_id'], original['eval_id'])
        self.assertEqual(self.verdict(fresh['evaluation_id']), 'accept')

    def test_private_chat_identical_revision_resumes(self):
        status, raw = self.http('POST', '/inquire', {'text': 'Synthetic cafe brief', 'request_id': 'start'})
        self.assertEqual(status, 200, raw)
        inquiry = json.loads(raw)['inquiry_id']
        self.rpc('commit_chat_turn', {'inquiry_id': inquiry, 'through_sequence': 1,
            'expected_revision': 0, 'request_id': 'answer', 'reply': 'Your brief is noted.', 'fact_changes': []})
        args = {'inquiry_id': inquiry, 'expected_revision': 0, 'offer_request': self.request()}
        original = self.rpc('price_inquiry_scope', args)
        self.assertEqual(self.verdict(original['evaluation_id']), 'clarify')
        self.register()
        fresh = self.rpc('price_inquiry_scope', args)
        self.assertEqual(fresh['deal_id'], original['deal_id'])
        self.assertNotEqual(fresh['evaluation_id'], original['evaluation_id'])
        self.assertEqual(self.verdict(fresh['evaluation_id']), 'accept')
        self.assertEqual(self.rpc('price_inquiry_scope', args), fresh)
        conn = db.connect(self.path)
        try:
            row = conn.execute('SELECT * FROM proposal_versions WHERE id=?', (fresh['version_id'],)).fetchone()
            self.assertIsNone(core.inquiry_quote.scope_error(conn, row))
            self.assertEqual(conn.execute('SELECT count(*) FROM evaluations').fetchone()[0], 2)
        finally:
            conn.close()
