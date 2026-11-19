"""Transactional owner import and sample-reset checks."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
import server
import core
import db
import dashboard_ops
from test_mcp import INQUIRY


class DashboardOperationsTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(':memory:')
        core.load_seed(self.conn)
        self.bundle = {'source': 'manual', 'source_id': 'stage14',
                       'offer_request': copy.deepcopy(INQUIRY), 'draft': 'Thank you.',
                       'option_ids': ['as_offered'], 'steps': [{'step_key': 'scoping', 'status': 'ok'}],
                       'evaluation': {'estimate': {'floor_sgd': 0}}}

    def tearDown(self):
        self.conn.close()

    def test_import_recomputes_and_replays_atomically(self):
        result = dashboard_ops.import_run(self.conn, self.bundle)
        self.assertTrue(result['evaluation_mismatch'])
        stored = db.uj(self.conn.execute('SELECT result_json FROM evaluations').fetchone()[0])
        self.assertEqual(stored['estimate']['floor_sgd'], 2454.9)
        self.assertEqual(result, dashboard_ops.import_run(self.conn, self.bundle))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM proposal_versions').fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM run_steps WHERE step_key='scoping'").fetchone()[0], 1)

    def test_explicit_request_id_conflict(self):
        self.bundle['request_id'] = 'import-request'
        dashboard_ops.import_run(self.conn, self.bundle)
        self.bundle['draft'] = 'Changed text.'
        with self.assertRaisesRegex(ValueError, 'request_id_conflict'):
            dashboard_ops.import_run(self.conn, self.bundle)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM proposal_versions').fetchone()[0], 2)

    def test_invalid_option_rolls_back(self):
        self.bundle['option_ids'] = ['other-evaluation-option']
        with self.assertRaises(ValueError):
            dashboard_ops.import_run(self.conn, self.bundle)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM deals').fetchone()[0], 0)

    def test_option_from_other_evaluation_is_rejected(self):
        other = copy.deepcopy(INQUIRY)
        other['offer'].update(deliverables=5, caption_versions=5, budget_sgd=2700,
                              deadline='2026-12-04')
        deal, offer, _ = core.claim_or_resume(self.conn, 'manual', 'other', 'Juniper Cafe', '', other)
        core.evaluate(self.conn, deal, offer)
        foreign = self.conn.execute("SELECT option_key FROM options WHERE option_key='phased'").fetchone()[0]
        self.bundle['option_ids'] = [foreign]
        with self.assertRaisesRegex(ValueError, 'option_id_not_in_evaluation'):
            dashboard_ops.import_run(self.conn, self.bundle)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM deals').fetchone()[0], 1)

    def test_new_import_reevaluates_and_retry_replays(self):
        first = dashboard_ops.import_run(self.conn, self.bundle)
        later = {**self.bundle, 'request_id': 'second-import', 'draft': 'A new draft.'}
        second = dashboard_ops.import_run(self.conn, later)
        self.assertEqual(first['deal_id'], second['deal_id'])
        self.assertNotEqual(first['offer_id'], second['offer_id'])
        self.assertNotEqual(first['evaluation_id'], second['evaluation_id'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evaluations').fetchone()[0], 2)
        self.assertEqual(second, dashboard_ops.import_run(self.conn, later))

    def test_reordered_subset_is_bound_to_draft(self):
        counter = copy.deepcopy(INQUIRY)
        counter['offer'].update(deliverables=5, caption_versions=5, budget_sgd=2700,
                                deadline='2026-12-04')
        bundle = {**self.bundle, 'offer_request': counter, 'option_ids': ['phased', 'hold_date'],
                  'draft': 'Thank you for considering these options.'}
        imported = dashboard_ops.import_run(self.conn, bundle)
        version = self.conn.execute('SELECT * FROM proposal_versions WHERE id=?', (imported['version_id'],)).fetchone()
        self.assertEqual([r['option_key'] for r in core.options_of(self.conn, version)], ['phased', 'hold_date'])
        self.assertEqual(core.option_set_errors(self.conn, version), [])
        with patch.object(core, 'run_outbound_check', return_value={'passed': True, 'violations': []}) as check:
            core.check_draft(self.conn, version['id'])
        self.assertEqual([opt['option_id'] for opt in check.call_args.args[0]['approved_options']],
                         ['phased', 'hold_date'])
        next_id = core.save_draft(self.conn, version['id'], 'Thank you.')
        next_version = self.conn.execute('SELECT * FROM proposal_versions WHERE id=?', (next_id,)).fetchone()
        self.assertEqual([r['option_key'] for r in core.options_of(self.conn, next_version)], ['phased', 'hold_date'])
        self.assertEqual(core.option_set_errors(self.conn, next_version), [])

    def test_reset_request_retry_does_not_delete_new_data(self):
        request = {'confirm': 'RESET SAMPLE DATA', 'request_id': 'reset-once'}
        first = dashboard_ops.reset_sample(self.conn, request)
        dashboard_ops.import_run(self.conn, self.bundle)
        self.assertEqual(first, dashboard_ops.reset_sample(self.conn, request))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM deals').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'request_id_conflict'):
            dashboard_ops.reset_sample(self.conn, {**request, 'extra': 1})

    def test_reset_records_new_capacity_revision(self):
        imported = dashboard_ops.import_run(self.conn, self.bundle)
        self.conn.execute("INSERT INTO reservations (id,deal_id,status) VALUES ('sample-reservation',?,'active')", (imported['deal_id'],))
        self.conn.execute("INSERT INTO allocations (id,reservation_id,week_ending,planning_hundredths) VALUES ('sample-allocation','sample-reservation','2026-12-04',100)")
        self.conn.execute("INSERT INTO deals (id,source,source_id,sample_data) VALUES ('live-deal','manual','live',0)")
        self.conn.execute("INSERT INTO reservations (id,deal_id,status) VALUES ('live-reservation','live-deal','active')")
        self.conn.execute("INSERT INTO allocations (id,reservation_id,week_ending,planning_hundredths) VALUES ('live-allocation','live-reservation','2026-12-04',200)")
        self.conn.commit()
        dashboard_ops.reset_sample(self.conn, {'confirm': 'RESET SAMPLE DATA'})
        self.assertEqual(self.conn.execute('SELECT count(*) FROM capacity_revisions').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT id FROM allocations').fetchone()[0], 'live-allocation')
        snapshot = db.uj(self.conn.execute('SELECT snapshot_json FROM capacity_revisions').fetchone()[0])
        self.assertEqual(snapshot['reason'], 'sample_reset')

    def test_malformed_steps_fail_validation(self):
        for steps in ([{'step_key': [], 'status': 'ok'}], [{'step_key': 'intake', 'status': {}}]):
            with self.assertRaises(ValueError):
                dashboard_ops.import_run(self.conn, {**self.bundle, 'steps': steps})

    def test_reset_preserves_non_sample(self):
        imported = dashboard_ops.import_run(self.conn, self.bundle)
        self.conn.execute("INSERT INTO inbox (id,source_id,sample_data) VALUES ('sample-inbox','sample-source',1)")
        self.conn.execute("INSERT INTO mcp_operations (request_id,tool,result_json) VALUES (?,?,?)",
                          ('quote-sample', 'quote', db.j({'source_id': 'sample-source'})))
        self.conn.execute("INSERT INTO mcp_operations (request_id,tool,result_json) VALUES (?,?,?)",
                          ('legacy-save', 'save_draft', db.j({'draft_version': {'version_id': imported['version_id'],
                                                                              'deal_id': imported['deal_id']}})))
        self.conn.execute("INSERT INTO mcp_operations (request_id,tool,result_json) VALUES (?,?,?)",
                          ('ambiguous', 'save_draft', '{}'))
        self.conn.execute("INSERT INTO deals (id,source,source_id,sample_data) VALUES ('live','manual','live',0)")
        self.conn.execute("INSERT INTO inbox (id,source_id,sample_data) VALUES ('live','live',0)")
        self.conn.commit()
        with self.assertRaises(ValueError):
            dashboard_ops.reset_sample(self.conn, {'confirm': 'reset sample data'})
        result = dashboard_ops.reset_sample(self.conn, {'confirm': 'RESET SAMPLE DATA'})
        self.assertEqual(result['counts']['deals'], 1)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT request_id FROM mcp_operations')], ['ambiguous'])
        self.assertEqual(self.conn.execute('SELECT id FROM deals').fetchone()[0], 'live')
        self.assertEqual(self.conn.execute('SELECT id FROM inbox').fetchone()[0], 'live')
        self.assertEqual(result['counts']['seed_tables']['seed_past_projects'], len(core.seed_data()['past_projects']))


class DashboardRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'test.db')
        self.http, self.owner = server.make_server(self.path)
        self.port = self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, data=None, token=True, origin=None):
        headers = {'Origin': origin or f'http://127.0.0.1:{self.port}'}
        if token:
            headers['X-Owner-Token'] = self.owner
        if data is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(data)
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request(method, path, data, headers)
        response = conn.getresponse()
        result = response.status, response.read().decode()
        conn.close()
        return result

    def test_owner_and_origin_guards(self):
        for path in ('/api/import-run', '/api/reset-sample'):
            self.assertEqual(self.request('POST', path, {}, token=False)[0], 403)
            self.assertEqual(self.request('POST', path, {}, origin='http://evil.test')[0], 403)
        self.assertEqual(self.request('POST', '/api/reset-sample', {'confirm': 'wrong'})[0], 400)

    def test_import_and_reset_routes(self):
        bundle = {'source': 'manual', 'source_id': 'route', 'offer_request': INQUIRY,
                  'draft': 'Thank you.', 'option_ids': ['as_offered'], 'steps': [],
                  'evaluation': {'estimate': {'floor_sgd': 0}}}
        status, body = self.request('POST', '/api/import-run', bundle)
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertTrue(result['evaluation_mismatch'])
        self.assertEqual(json.loads(self.request('POST', '/api/import-run', bundle)[1])['deal_id'], result['deal_id'])
        bad = {**bundle, 'source_id': 'bad-route', 'option_ids': ['alien']}
        self.assertEqual(self.request('POST', '/api/import-run', bad)[0], 400)
        status, body = self.request('POST', '/api/reset-sample', {'confirm': 'RESET SAMPLE DATA'})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['counts']['deals'], 1)

    def test_dashboard_has_live_polling(self):
        status, page = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('/api/state', page)
        self.assertIn('visibilitychange', page)
        self.assertIn('4000', page)
        self.assertIn('Live updates', page)
        self.assertIn('Refresh', page)

    def test_client_route_refuses_modified_release(self):
        bundle = {'source': 'manual', 'source_id': 'release-integrity', 'offer_request': INQUIRY,
                  'draft': 'Thank you.', 'option_ids': ['as_offered'], 'steps': []}
        status, body = self.request('POST', '/api/import-run', bundle)
        self.assertEqual(status, 200)
        version_id = json.loads(body)['version_id']
        status, body = self.request('POST', '/api/approve', {'version_id': version_id})
        self.assertEqual(status, 200)
        client_path = json.loads(body)['url']
        self.assertEqual(self.request('GET', client_path, token=False)[0], 200)
        conn = db.connect(self.path)
        try:
            with conn:
                conn.execute('UPDATE proposal_versions SET draft_body=? WHERE id=?',
                             ('Our price floor is S$2,454.90.', version_id))
        finally:
            conn.close()
        status, body = self.request('GET', client_path, token=False)
        self.assertEqual(status, 404)
        self.assertNotIn('2,454.90', body)


if __name__ == '__main__':
    unittest.main()
