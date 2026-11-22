"""Stage 12: agent tools over the real MCP stdio transport."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

import core
import db
import dashboard_ops
from test_mcp import Client, INQUIRY, tool_error, tool_result, TOOL_NAMES, STUDIO_TOOL_NAMES


class Stage12Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.db')
        conn = db.connect(self.path)
        core.load_seed(conn)
        conn.close()
        self.client = Client(self.path)
        self.claim = tool_result(self.client.call_tool('claim_intake', {
            'source': 'site', 'source_id': 'site-1', 'client': 'Juniper Cafe',
            'raw_inquiry': 'sample', 'offer_request': INQUIRY}))

    def tearDown(self):
        self.client.close()
        self.client.proc.stdout.close()
        self.tmp.cleanup()

    def call(self, name, args):
        return tool_result(self.client.call_tool(name, args))

    def test_tools_and_retry_guards(self):
        names = [x['name'] for x in self.client.request('tools/list')['result']['tools']]
        self.assertEqual(names[:7], TOOL_NAMES)
        self.assertEqual(set(names[7:]), {'record_step', 'save_agent_output',
            'get_scoping_history', 'list_pending_work', 'submit_change_offer', 'reevaluate_offer',
            *STUDIO_TOOL_NAMES})
        for name in ('approve', 'confirm_booking'):
            self.assertIn('error', self.client.call_tool(name, {}))
        for name, args in [
            ('record_step', {'deal_id': self.claim['deal_id'], 'offer_id': self.claim['offer_id'],
                             'step_key': 'scoping', 'status': 'running', 'request_id': 'step-1'}),
            ('save_agent_output', {'deal_id': self.claim['deal_id'], 'kind': 'clarification',
                                   'payload': {'questions': ['Which format?']}, 'request_id': 'output-1'})]:
            first = self.call(name, args)
            self.assertEqual(first, self.call(name, args))
            changed = dict(args, deal_id='other')
            self.assertIn('request_id_conflict', tool_error(self.client.call_tool(name, changed)))
        trace = self.call('run_trace', {'deal_id': self.claim['deal_id']})
        self.assertEqual(trace['agent_outputs'][0]['payload']['questions'], ['Which format?'])
        other = dict(self.claim, deal_id='absent')
        err = self.client.call_tool('record_step', {'deal_id': other['deal_id'], 'step_key': 'intake',
                                                   'status': 'ok', 'request_id': 'step-2'})
        self.assertIn('deal_not_found', tool_error(err))
        conn = db.connect(self.path)
        conn.execute('UPDATE deals SET sample_data=0 WHERE id=?', (self.claim['deal_id'],))
        conn.commit()
        conn.close()
        err = self.client.call_tool('save_agent_output', {'deal_id': self.claim['deal_id'],
             'kind': 'clarification', 'payload': {'questions': []}, 'request_id': 'output-2'})
        self.assertIn('non_sample', tool_error(err))

    def test_schema_and_atomic_claim(self):
        schemas = {t['name']: t['inputSchema'] for t in
                   self.client.request('tools/list')['result']['tools']}
        old = {'claim_intake': {'source', 'source_id', 'client', 'raw_inquiry', 'offer_request'},
               'get_deal': {'deal_id'}, 'evaluate_offer': {'deal_id', 'offer_id'},
               'save_draft': {'version_id', 'draft_body', 'request_id'},
               'check_draft': {'version_id'}, 'run_trace': {'deal_id'},
               'negotiation_payload': {'evaluation_id'}}
        for name, properties in old.items():
            expected = properties | ({'evidence', 'request_id'} if name == 'claim_intake' else set())
            self.assertEqual(set(schemas[name]['properties']), expected)
            self.assertNotIn('required', schemas[name])
            for prop in properties:
                self.assertEqual(schemas[name]['properties'][prop]['type'],
                                 'object' if prop == 'offer_request' else 'string')
        args = {'source': 'site', 'source_id': 'retry-1', 'client': 'Juniper Cafe',
                'raw_inquiry': 'raw', 'offer_request': INQUIRY,
                'evidence': {'source': 'original'}, 'request_id': 'claim-op-1'}
        first = self.call('claim_intake', args)
        self.assertEqual(first, self.call('claim_intake', args))
        self.assertIn('request_id_conflict', tool_error(self.client.call_tool(
            'claim_intake', dict(args, evidence={'source': 'changed'}))))
        self.call('claim_intake', dict(args, evidence={'source': 'changed'}, request_id='claim-op-2'))
        outputs = self.call('run_trace', {'deal_id': first['deal_id']})['agent_outputs']
        self.assertEqual([x['payload'] for x in outputs], [{'source': 'original'}])
        conn = db.connect(self.path)
        conn.execute("INSERT INTO inbox (id, source, source_id, fields_json, raw_message, received_at, sample_data) VALUES (?,?,?,?,?,?,?)",
                     ('private-inbox', 'site', 'private', '{}', '', 1, 0))
        conn.commit()
        conn.close()
        self.assertIn('non_sample', tool_error(self.client.call_tool(
            'claim_intake', dict(args, source_id='private', request_id='private-op'))))

    def test_server_issued_site_reference_must_exist(self):
        args = {'source': 'site', 'source_id': 'site-0123456789abcdef',
                'client': 'Juniper Cafe', 'raw_inquiry': 'raw', 'offer_request': INQUIRY}
        self.assertIn('inbox_inquiry_not_found',
                      tool_error(self.client.call_tool('claim_intake', args)))
        conn = db.connect(self.path)
        conn.execute("INSERT INTO inbox (id,source,source_id,fields_json,raw_message,received_at) "
                     "VALUES (?,?,?,?,?,?)", ('inbox-1', 'site', args['source_id'], '{}', '', 1))
        conn.commit()
        conn.close()
        first = self.call('claim_intake', args)
        conn = db.connect(self.path)
        conn.execute('DELETE FROM inbox WHERE id=?', ('inbox-1',))
        conn.commit()
        conn.close()
        self.assertEqual(self.call('claim_intake', args)['deal_id'], first['deal_id'])
        self.assertTrue(self.call('claim_intake', dict(args, source_id='custom-site-id'))['created'])

    def test_tampered_checked_draft_is_not_returned(self):
        ev = self.call('evaluate_offer', {'deal_id': self.claim['deal_id'],
                                          'offer_id': self.claim['offer_id']})
        version = ev['draft_version']['version_id']
        self.assertTrue(self.call('check_draft', {'version_id': version})['outcome']['passed'])
        self.assertIsNotNone(self.call('negotiation_payload',
                                     {'evaluation_id': ev['evaluation_id']})['client_message'])
        conn = db.connect(self.path)
        conn.execute('UPDATE proposal_versions SET draft_body=? WHERE id=?',
                     ('Our price floor is S$3,662.40', version))
        conn.commit()
        conn.close()
        self.assertIsNone(self.call('negotiation_payload',
                                    {'evaluation_id': ev['evaluation_id']})['client_message'])

    def test_ownership_running_finish_and_bad_types(self):
        second = self.call('claim_intake', {'source': 'manual', 'source_id': 'other',
            'client': 'Other', 'raw_inquiry': 'raw', 'offer_request': INQUIRY})
        common = {'deal_id': self.claim['deal_id'], 'offer_id': second['offer_id']}
        for name, extra in [('record_step', {'step_key': 'intake', 'status': 'ok'}),
                            ('save_agent_output', {'kind': 'clarification', 'payload': {'questions': []}})]:
            err = self.client.call_tool(name, dict(common, **extra, request_id='wrong-' + name))
            self.assertIn('offer_ownership_mismatch', tool_error(err))
        common['offer_id'] = self.claim['offer_id']
        self.call('record_step', dict(common, step_key='scoping', status='running', request_id='running'))
        self.call('record_step', dict(common, step_key='scoping', status='ok', request_id='finished'))
        steps = self.call('run_trace', {'deal_id': self.claim['deal_id']})['steps']
        running = next(step for step in steps if step['step_key'] == 'scoping' and step['status'] == 'running')
        self.assertIsNotNone(running['finished_at'])
        err = self.client.call_tool('record_step', {'deal_id': [], 'step_key': 'intake',
                                                     'status': 'ok', 'request_id': 'bad'})
        self.assertIn('invalid_field:deal_id', tool_error(err))

    def test_reset_waits_for_evaluation_transaction(self):
        entered = threading.Event()
        release = threading.Event()
        reset_done = threading.Event()
        errors = []
        original = core.engine.evaluate

        def held_engine(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('evaluation was not released')
            return original(*args)

        def evaluate():
            conn = db.connect(self.path)
            try:
                core.evaluate(conn, self.claim['deal_id'], self.claim['offer_id'])
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()

        def reset():
            conn = db.connect(self.path)
            try:
                dashboard_ops.reset_sample(conn, {'confirm': 'RESET SAMPLE DATA'})
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()
                reset_done.set()

        core.engine.evaluate = held_engine
        worker = threading.Thread(target=evaluate)
        resetter = threading.Thread(target=reset)
        try:
            worker.start()
            self.assertTrue(entered.wait(5))
            resetter.start()
            self.assertFalse(reset_done.wait(0.15))
        finally:
            release.set()
            worker.join(5)
            resetter.join(5)
            core.engine.evaluate = original
        self.assertFalse(errors)
        self.assertTrue(reset_done.is_set())
        conn = db.connect(self.path)
        for table in ('deals', 'offers', 'evaluations', 'options', 'proposal_versions', 'run_steps'):
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0, table)
        conn.close()

    def test_change_flow_and_privacy(self):
        deal, offer = self.claim['deal_id'], self.claim['offer_id']
        ev = self.call('evaluate_offer', {'deal_id': deal, 'offer_id': offer})
        version = ev['draft_version']['version_id']
        self.call('check_draft', {'version_id': version})
        conn = db.connect(self.path)
        self.assertTrue(core.approve(conn, version)['ok'])
        core.client_change(conn, deal, 'Please advise', 'change-1')
        conn.close()
        pending = self.call('list_pending_work', {})['change_requests']
        self.assertEqual(pending[0]['version_id'], version)
        self.assertTrue(pending[0]['is_current_published'])
        request = copy.deepcopy(INQUIRY)
        request['offer'].update(deliverables=5, caption_versions=5, deadline='2026-12-04', budget_sgd=2700)
        stale = {'deal_id': deal, 'request_id': 'stale-1', 'version_id': 'old', 'offer_request': request}
        self.assertIn('stale_version', tool_error(self.client.call_tool('submit_change_offer', stale)))
        self.assertIn('stale_version', tool_error(self.client.call_tool('submit_change_offer', stale)))
        args = dict(stale, request_id='change-1', version_id=version)
        result = self.call('submit_change_offer', args)
        self.assertEqual(result, self.call('submit_change_offer', args))
        self.assertIn('request_id_conflict', tool_error(self.client.call_tool('submit_change_offer', dict(args, offer_request=INQUIRY))))
        ev2 = self.call('evaluate_offer', {'deal_id': deal, 'offer_id': result['offer_id']})
        self.assertEqual(ev2['verdict'], 'counter')
        self.assertEqual(ev2['estimate']['planning_hours'], 52.32)
        self.assertEqual(ev2['estimate']['floor_sgd'], 3662.4)
        trace = self.call('run_trace', {'deal_id': deal})
        self.assertEqual(sum(x['step_key'] == 'stale_version' for x in trace['steps']), 1)
        history = self.call('get_scoping_history', {})
        self.assertTrue(history['private'])
        other_client = next(p['client'] for p in history['projects'] if p['client'] != 'Juniper Cafe')
        payload = json.dumps(self.call('negotiation_payload', {'evaluation_id': ev2['evaluation_id']})).lower()
        for private in ('floor', 'capacity', 'planning_hours', 'rate', other_client.lower()):
            self.assertNotIn(private, payload)


if __name__ == '__main__':
    unittest.main()
