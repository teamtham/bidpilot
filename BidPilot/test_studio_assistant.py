"""Synthetic HTTP and ownership contracts. No external model or credentials."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import conversation as chat
import db
import server
import studio_assistant as assistant
from test_http import request


class StudioAssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'app.db'
        self.env = patch.dict(os.environ, {'BIDPILOT_STUDIO_ASSISTANT': 'gemini', 'GEMINI_API_KEY': ''})
        self.env.start()
        self.http, _ = server.make_server(self.path, port=0)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.http.server_port)
        self.c = db.connect(self.path)

    def tearDown(self):
        self.c.close()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.env.stop()
        self.tmp.cleanup()

    def post(self, path, data, fill=True):
        if fill and path.endswith('/submit'):
            self.fill_brief()
        code, raw = request(self.base, 'POST', path, data, headers={'Origin':self.base})
        return code, json.loads(raw)

    FILLERS = {'Format':'project_type', 'Production':'production.mode', 'Shoot days':'shoot_days',
               'Locations':'locations', 'Videos':'video_count', 'Captioned versions':'captions',
               'Scripting':'scripting', 'Revision rounds':'revision_rounds', 'Deadline':'deadline',
               'Editing':'editing'}

    def fill_brief(self):
        """Confirm any required brief field the scenario left empty, so /submit is allowed."""
        ctx = chat.context(self.c, {'inquiry_id': self.i})
        with self.c:
            for name in chat.brief_missing(ctx['facts']):
                if name.startswith('Confirm '):
                    continue
                self.c.execute('INSERT INTO inquiry_facts VALUES (?,?,?,?,?,?,?,?,?)',
                               (chat.uid(), self.i, 0, self.FILLERS[name], '"x"', 'confirmed',
                                ctx['processed_sequence'], None, 0))

    def test_incomplete_brief_cannot_be_submitted(self):
        fact = {'subject':'videos','value':2,'status':'confirmed'}
        with patch.object(assistant, 'generate', return_value=self.answer(facts=[fact])):
            self.start('Two reels')
        ctx = chat.context(self.c, {'inquiry_id':self.i})
        submission = {'revision':ctx['revision'], 'sequence':ctx['processed_sequence']}
        state = json.loads(request(self.base, 'GET', self.url+'/state')[1])
        self.assertEqual(len(state['missing']), 9)
        self.assertNotIn('Videos', state['missing'])
        self.assertNotIn('Your budget', state['missing'])
        self.assertFalse(state['submitted'])
        self.assertIsNone(state['last_receipt'])
        code, result = self.post(self.url+'/submit', submission, fill=False)
        self.assertEqual((code, result['error']), (400, 'brief_incomplete'))
        self.assertEqual(self.c.execute('SELECT count(*) FROM studio_submissions').fetchone()[0], 0)
        self.fill_brief()
        with self.c:
            self.c.execute("UPDATE inquiry_facts SET status='proposed' WHERE subject='deadline' AND inquiry_id=?", (self.i,))
        state = json.loads(request(self.base, 'GET', self.url+'/state')[1])
        self.assertEqual(state['missing'], ['Confirm deadline'])
        self.assertEqual(self.post(self.url+'/submit', submission, fill=False)[1]['error'], 'brief_incomplete')
        with self.c:
            self.c.execute("UPDATE inquiry_facts SET status='excluded' WHERE subject='deadline' AND inquiry_id=?", (self.i,))
        first = self.post(self.url+'/submit', submission, fill=False)[1]
        self.assertTrue(first['submitted'])
        state = json.loads(request(self.base, 'GET', self.url+'/state')[1])
        self.assertEqual((state['missing'], state['submitted'], state['last_receipt']['id']), ([], True, first['receipt_id']))

    def start(self, text='Two reels'):
        code, result = self.post('/inquire', {'text': text, 'request_id': 'start'})
        self.assertEqual(code, 200)
        self.i = result['inquiry_id']
        self.url = '/i/' + result['token']
        return result

    def answer(self, reply='What should viewers do after watching?', facts=None, **extra):
        return {'reply': reply, 'fact_changes': facts or [], **extra}

    def test_fact_shape_error_gets_bounded_repair_without_provenance_override(self):
        self.start('The objective is awareness')
        fact = {'subject':'production.objective','value':'awareness','status':'confirmed'}
        invalid = self.answer(facts=[dict(fact, source_seq=999)])
        with patch.object(assistant, 'generate', side_effect=[invalid, self.answer(facts=[fact])]) as model:
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'complete')
        repair = json.loads(model.call_args.args[0][-1]['text'])
        self.assertEqual(repair['contract_error'], 'invalid_fact')
        self.assertEqual(repair['rejected_fact_index'], 0)
        ctx = chat.context(self.c, {'inquiry_id':self.i})
        self.assertEqual(ctx['facts'][0]['source_seq'], 1)
        self.assertEqual(len(ctx['turns']), 2)
        with patch.object(assistant, 'generate') as model:
            self.post(self.url+'/retry', {})
            model.assert_not_called()

    def test_grounding_repair_identifies_rejected_fact_and_preserves_correction(self):
        fact = {'subject':'production.objective','value':'awareness','status':'confirmed'}
        with patch.object(assistant, 'generate', return_value=self.answer(facts=[fact])):
            self.start('The objective is awareness')
        old = chat.context(self.c, {'inquiry_id':self.i})['facts'][0]['id']
        corrected = dict(fact, value='sales', supersedes=old)
        bad = dict(corrected, value='conversion')
        with patch.object(assistant, 'generate', side_effect=[self.answer(facts=[bad]), self.answer(facts=[corrected])]) as model:
            result = self.post(self.url+'/messages', {'text':'Actually sales','request_id':'correction'})[1]
            self.assertEqual(result['assistant_status'], 'complete')
        repair = json.loads(model.call_args.args[0][-1]['text'])
        self.assertEqual(repair['rejected_fact_index'], 0)
        ctx = chat.context(self.c, {'inquiry_id':self.i})
        self.assertEqual(ctx['facts'][-1]['supersedes'], old)
        self.assertEqual(ctx['facts'][-1]['value'], 'sales')
        self.assertEqual(len(ctx['turns']), 4)

    def test_attempt_diagnostics_repair_retry_correction_once(self):
        self.start('Two reels')
        with patch.object(assistant, 'generate', side_effect=OSError('secret synthetic detail')):
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'retry')
        fact = {'subject': 'videos', 'value': 2, 'status': 'confirmed'}
        bad = self.answer(facts=[{**fact, 'value': 99}])
        with patch.object(assistant, 'generate', side_effect=[bad, self.answer(facts=[fact])]):
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'complete')
        old = chat.context(self.c, {'inquiry_id': self.i})['facts'][0]['id']
        correction = {**fact, 'value': 3, 'supersedes': old}
        with patch.object(assistant, 'generate', return_value=self.answer(facts=[correction])):
            self.post(self.url+'/messages', {'text': 'Actually three reels', 'request_id': 'correct'})
            self.post(self.url+'/retry', {})
        ctx = chat.context(self.c, {'inquiry_id': self.i})
        self.assertEqual(len(ctx['turns']), 4)
        self.assertEqual(ctx['facts'][-1]['supersedes'], old)
        events = [dict(r) for r in self.c.execute('SELECT * FROM gemini_attempt_events')]
        encoded = json.dumps(events)
        self.assertNotIn('secret synthetic detail', encoded)
        self.assertNotIn('Two reels', encoded)
        self.assertIn('transport_error', encoded)
        self.assertIn('unverified_number', encoded)
        self.assertEqual(sum(e['stage']=='status' and e['category']=='complete' for e in events), 2)
        self.assertEqual(sum(e['stage']=='repair' for e in events), 1)

    def test_browser_normal_paths_render_retry_without_poll(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            page=browser.new_page()
            errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(self.base+'/inquire?new=1')
            page.locator('#text').fill('Two reels')
            page.locator('#send').click()
            page.wait_for_url('**/i/*')
            page.locator('#retryassistant').wait_for(state='visible')
            self.assertIn('Two reels',page.locator('#turns').inner_text())
            self.assertIn('message is saved',page.locator('#status').inner_text())
            with patch.object(assistant,'generate',return_value=self.answer()):
                page.locator('#retryassistant').click()
                page.get_by_text('What should viewers do after watching?',exact=True).wait_for()
                page.locator('#text').fill('A launch film')
                page.locator('#send').click()
                page.wait_for_function("document.querySelector('#text').value === ''")
            page.reload()
            page.get_by_text('A launch film',exact=True).wait_for()
            self.assertEqual(errors,[])
            browser.close()

    def test_transport_keeps_certificate_verification(self):
        import io
        import ssl
        payload = {'candidates': [{'finishReason': 'STOP', 'content': {
            'parts': [{'text': json.dumps(self.answer())}]}}]}
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'synthetic-test-key'}), \
                patch.object(assistant, 'urlopen', return_value=io.StringIO(json.dumps(payload))) as transport:
            self.assertEqual(assistant.generate([{'text': 'Synthetic input'}]), self.answer())
        context = transport.call_args.kwargs['context']
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(transport.call_args.kwargs['timeout'], 25)

    def test_default_missing_key_preserves_retry_http(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(assistant.configured_provider(), 'gemini')
        result = self.start()
        self.assertEqual(result['assistant_status'], 'retry')
        self.assertEqual(len(chat.context(self.c, {'inquiry_id': self.i})['turns']), 1)
        self.assertEqual(chat.pending(self.c), [])
        with patch.object(assistant, 'generate', return_value=self.answer()):
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'complete')
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'complete')
        ctx = chat.context(self.c, {'inquiry_id': self.i})
        self.assertEqual(len(ctx['turns']), 2)
        code, raw = request(self.base, 'GET', self.url+'/state')
        self.assertEqual(code, 200)
        state = json.loads(raw)
        self.assertFalse(state['pending'])
        self.assertIsNone(state['assistant_error'])
        self.assertNotIn('pricing_history', state)

    def test_failures_are_atomic_and_do_not_expose_exception(self):
        self.start()
        for answer in [self.answer('We will deliver on Friday'), self.answer(facts=[{'subject':'videos','value':99,'status':'confirmed'}]), {'reply':None,'fact_changes':[]}, self.answer(facts=[{'subject':'production.objective','value':'Invented','status':'confirmed'}])]:
            with patch.object(assistant, 'generate', return_value=answer):
                self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'retry')
            self.assertEqual(len(chat.context(self.c, {'inquiry_id':self.i})['turns']), 1)
        with patch.object(assistant, 'generate', side_effect=OSError('sensitive upstream detail')):
            result = self.post(self.url+'/retry', {})[1]
            self.assertNotIn('sensitive', json.dumps(result))

    def test_completed_turn_clears_stale_failure_notice_on_reload(self):
        with patch.object(assistant, 'generate', return_value=self.answer()):
            self.start()
        with self.c:
            self.c.execute('UPDATE gemini_chats SET error=? WHERE inquiry_id=?',
                           ('Studio Assistant could not reply. Your message is saved. Please retry.', self.i))
        with patch.object(assistant, 'generate') as model:
            code, raw = request(self.base, 'GET', self.url+'/state')
            state = json.loads(raw)
            self.assertEqual(code, 200)
            self.assertIsNone(state['assistant_error'])
            self.assertEqual(state['turns'][-1]['body'], self.answer()['reply'])
            model.assert_not_called()
        self.assertIsNone(self.c.execute('SELECT error FROM gemini_chats WHERE inquiry_id=?',
                                        (self.i,)).fetchone()[0])

    def test_corrections_email_and_immutable_handoff(self):
        fact = {'subject':'videos','value':2,'status':'confirmed'}
        with patch.object(assistant, 'generate', return_value=self.answer(facts=[fact], proposal_email={'email':'buyer@example.test','confirmed':True})):
            self.start('Two reels, buyer@example.test')
        ctx = chat.context(self.c, {'inquiry_id':self.i})
        old = ctx['facts'][0]['id']
        submission = {'revision':ctx['revision'], 'sequence':ctx['processed_sequence']}
        first = self.post(self.url+'/submit', submission)[1]
        self.assertTrue(first['submitted'])
        self.assertEqual(first, self.post(self.url+'/submit', submission)[1])
        self.assertEqual(len(assistant.pending(self.c)), 1)
        snapshot = self.c.execute('SELECT snapshot_json FROM studio_submissions').fetchone()[0]
        with patch.object(assistant, 'generate', return_value=self.answer(facts=[{**fact,'value':3,'supersedes':old}], proposal_email={'email':'new@example.test','confirmed':True})):
            self.assertEqual(self.post(self.url+'/messages', {'text':'Actually three reels, new@example.test','request_id':'correction'})[1]['assistant_status'], 'complete')
        self.assertEqual(assistant.pending(self.c), [])
        ctx = chat.context(self.c, {'inquiry_id':self.i})
        self.assertEqual(ctx['facts'][-1]['supersedes'], old)
        self.assertEqual(ctx['proposal_email']['email'], 'new@example.test')
        second = self.post(self.url+'/submit', {'revision':ctx['revision'],'sequence':ctx['processed_sequence']})[1]
        self.assertNotEqual(first['receipt_id'], second['receipt_id'])
        self.assertEqual(snapshot, self.c.execute('SELECT snapshot_json FROM studio_submissions WHERE id=?',(first['receipt_id'],)).fetchone()[0])

    def test_submitted_brief_prices_once_through_existing_mcp(self):
        import inquiry_quote
        import mcp_server
        from test_http import INQUIRY
        with patch.object(assistant,'generate',return_value=self.answer()):
            self.start('Juniper Cafe needs two reels')
        ctx=chat.context(self.c,{'inquiry_id':self.i})
        args={'inquiry_id':self.i,'expected_revision':ctx['revision'],'offer_request':INQUIRY}
        with self.assertRaisesRegex(ValueError,'brief_not_submitted'):
            inquiry_quote.price(self.c,args)
        self.post(self.url+'/submit',{'revision':ctx['revision'],'sequence':ctx['processed_sequence']})
        with patch.object(mcp_server,'_connect',side_effect=lambda:db.connect(self.path)):
            self.assertEqual(len(mcp_server.t_list_pending_work({})['studio_briefs']),1)
        first=inquiry_quote.price(self.c,args)
        second=inquiry_quote.price(self.c,args)
        self.assertEqual(first,second)
        self.assertEqual(assistant.pending(self.c),[])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM offers').fetchone()[0],1)

    def test_concurrent_retry_and_switch_keeps_one_owner(self):
        self.start()
        entered, resume = threading.Event(), threading.Event()
        def generate(parts):
            entered.set()
            self.assertTrue(resume.wait(5))
            return self.answer()
        results = []
        with patch.object(assistant, 'generate', side_effect=generate):
            worker = threading.Thread(target=lambda: results.append(self.post(self.url+'/retry', {})[1]))
            worker.start()
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'busy')
            with patch.dict(os.environ, {'BIDPILOT_STUDIO_ASSISTANT':'qwenwork'}):
                self.assertEqual(assistant.route(self.c,self.i),'gemini')
                with self.assertRaisesRegex(ValueError,'wrong_chat_provider'):
                    chat.claim_work(self.c, {'inquiry_id':self.i,'worker_id':'legacy'})
                resume.set()
                worker.join(5)
                self.assertEqual(results[0]['assistant_status'],'complete')
                self.assertEqual(assistant.route(self.c,self.i),'qwenwork')
                chat.add_message(self.c,self.i,{'text':'A new question','request_id':'legacy'})
                claim = chat.claim_work(self.c, {'inquiry_id':self.i,'worker_id':'legacy'})
                with patch.dict(os.environ, {'BIDPILOT_STUDIO_ASSISTANT':'gemini'}):
                    self.assertEqual(assistant.route(self.c,self.i),'qwenwork')
                chat.commit(self.c, {'inquiry_id':self.i,'through_sequence':claim['through_sequence'],'expected_revision':1,'reply':'What is the objective?','request_id':'legacy-reply'})
        self.assertEqual(len(chat.context(self.c,{'inquiry_id':self.i})['turns']),4)

    def test_default_transport_browser_immediate_followup(self):
        from io import BytesIO
        from playwright.sync_api import sync_playwright
        os.environ.pop('BIDPILOT_STUDIO_ASSISTANT', None)
        os.environ['GEMINI_API_KEY'] = 'synthetic-transport-key'
        replies = iter(['What is the objective?', 'Where will viewers watch it?'])
        def transport(req, timeout, context):
            self.assertTrue(context.check_hostname)
            payload = json.loads(req.data)
            self.assertEqual(timeout, 25)
            self.assertNotIn('synthetic-transport-key', json.dumps(payload))
            self.assertIn('systemInstruction', payload)
            return BytesIO(json.dumps({'candidates': [{'finishReason': 'STOP',
                'content': {'parts': [{'text': json.dumps(self.answer(next(replies)))}]}}]}).encode())
        with patch.object(assistant, 'urlopen', side_effect=transport) as api:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.goto(self.base + '/inquire?new=1')
                page.locator('#text').fill('A launch film')
                page.locator('#send').click()
                page.wait_for_url('**/i/*')
                page.get_by_text('What is the objective?', exact=True).wait_for()
                page.evaluate('window.setInterval = () => 0')
                page.locator('#text').fill('Build awareness')
                page.locator('#send').click()
                page.get_by_text('Where will viewers watch it?', exact=True).wait_for()
                page.reload()
                page.get_by_text('Build awareness', exact=True).wait_for()
                self.assertEqual(api.call_count, 2)
                browser.close()

    def test_corrected_submission_mcp_draft_callback_and_stale_scope(self):
        import copy
        import mcp_server
        import qwenwork_callback
        from test_http import INQUIRY
        fact = {'subject': 'production.objective', 'value': 'awareness', 'status': 'confirmed'}
        with patch.object(assistant, 'generate', return_value=self.answer(facts=[fact])):
            self.start('Juniper Cafe wants awareness')
        ctx = chat.context(self.c, {'inquiry_id': self.i})
        old_revision = ctx['revision']
        fact = {**fact, 'value': 'sales', 'supersedes': ctx['facts'][0]['id']}
        with patch.object(assistant, 'generate', return_value=self.answer(facts=[fact])):
            self.post(self.url+'/messages', {'text': 'Actually sales', 'request_id': 'correct'})
        ctx = chat.context(self.c, {'inquiry_id': self.i})
        submission = {'revision': ctx['revision'], 'sequence': ctx['processed_sequence']}
        first = self.post(self.url+'/submit', submission)
        self.assertEqual(first, self.post(self.url+'/submit', submission))
        with patch.object(mcp_server, '_connect', side_effect=lambda: db.connect(self.path)):
            pending = mcp_server.t_list_pending_work({})['studio_briefs']
            self.assertEqual(len(pending), 1)
            snapshot = json.loads(pending[0]['snapshot_json'])
            self.assertEqual(snapshot['facts'][-1]['value'], 'sales')
            with self.assertRaisesRegex(ValueError, 'wrong_chat_provider'):
                chat.claim_work(self.c, {'inquiry_id': self.i, 'worker_id': 'qwen'})
            with self.assertRaisesRegex(ValueError, 'wrong_chat_provider'):
                chat.commit(self.c, {'inquiry_id': self.i, 'through_sequence': ctx['processed_sequence'],
                    'expected_revision': ctx['revision'], 'reply': 'Thanks', 'request_id': 'qwen'})
            import inquiry_quote
            args = {'inquiry_id': self.i, 'expected_revision': old_revision, 'offer_request': copy.deepcopy(INQUIRY)}
            with self.assertRaisesRegex(ValueError, 'stale_revision'):
                inquiry_quote.price(self.c, args)
            args['expected_revision'] = ctx['revision']
            priced = inquiry_quote.price(self.c, args)
            self.assertEqual(priced, inquiry_quote.price(self.c, args))
            for stage in ('intake', 'scoping', 'engine', 'draft'):
                mcp_server.t_record_step({'deal_id': priced['deal_id'], 'offer_id': priced['offer_id'],
                    'step_key': stage, 'status': 'ok', 'request_id': 'integration-step-'+stage})
            drafted = mcp_server.t_save_draft({'version_id': priced['version_id'],
                'draft_body': 'Here is the proposal for your review.', 'request_id': 'integration-draft'})
            self.assertIn('draft_version', drafted)
            payload = {'job_id': 'integration-callback', 'inquiry_id': self.i,
                       'result': {'draft_body': 'Please review the proposal with Alex.'}}
            code, result = qwenwork_callback.process(self.c, json.dumps(payload).encode())
            self.assertEqual((code, result['outcome']), (200, 'draft_saved'))
            self.assertTrue(result['founder_approval_required'])
            self.assertEqual(mcp_server.t_list_pending_work({})['studio_briefs'], [])
            with patch.object(assistant, 'generate', return_value=self.answer()):
                self.post(self.url+'/messages', {'text': 'Another change', 'request_id': 'late'})
            with self.assertRaisesRegex(ValueError, 'stale_revision'):
                inquiry_quote.price(self.c, args)
            payload['job_id'] = 'stale-callback'
            self.assertEqual(qwenwork_callback.process(self.c, json.dumps(payload).encode())[1]['outcome'], 'held_stale_scope')
            self.assertEqual(self.c.execute('SELECT COUNT(*) FROM offers').fetchone()[0], 1)
            self.assertEqual(self.c.execute('SELECT COUNT(*) FROM approvals').fetchone()[0], 0)

    def test_explicit_qwenwork_no_gemini_and_same_link_switch(self):
        with patch.dict(os.environ, {'BIDPILOT_STUDIO_ASSISTANT': 'qwenwork'}):
            with patch.object(assistant, 'generate', side_effect=AssertionError('Gemini must not run')):
                result = self.start()
                self.assertEqual(result['provider'], 'qwenwork')
                claim = chat.claim_work(self.c, {'inquiry_id': self.i, 'worker_id': 'qwen'})
                chat.commit(self.c, {'inquiry_id': self.i, 'through_sequence': claim['through_sequence'],
                    'expected_revision': 0, 'reply': 'What is the objective?', 'request_id': 'legacy'})
        with patch.object(assistant, 'generate', return_value=self.answer('Where will viewers watch it?')):
            self.post(self.url+'/messages', {'text': 'Awareness', 'request_id': 'switch'})
        code, raw = request(self.base, 'GET', self.url+'/state')
        state = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertEqual(state['provider'], 'gemini')
        self.assertEqual([t['body'] for t in state['turns']],
                         ['Two reels', 'What is the objective?', 'Awareness', 'Where will viewers watch it?'])

    def test_model_context_current_inquiry_only(self):
        self.start()
        other = chat.create_inquiry(self.c, {'text':'OTHER CLIENT SECRET'})
        ctx = chat.context(self.c, {'inquiry_id':self.i})
        ctx['pricing_history'] = [{'private_floor':1234}]
        text = json.dumps(assistant.model_input(self.c,ctx,None))
        self.assertNotIn('OTHER CLIENT',text)
        self.assertNotIn('private_floor',text)
        self.assertNotIn(other['inquiry_id'],text)

    def test_shared_skills_and_cleared_portfolio_lookup(self):
        guidance = assistant.chat_guidance()
        for skill, headings in assistant.CHAT_SECTIONS.items():
            for heading in headings:
                self.assertIn(f'{skill} / {heading}', guidance)
        self.assertIn('production.clips_preselected', guidance)
        self.assertNotIn('P-041', guidance)
        self.assertNotIn('compose_proposal_page', guidance)
        self.c.execute("UPDATE portfolio_assets SET shared=0 WHERE id='bakery'")
        self.c.commit()
        with patch.object(assistant, 'generate', side_effect=[
                {'portfolio_query': 'food cafe promo', 'frame_seconds': 0},
                self.answer('Opening morning uses warm coffee imagery. Which elements appeal to you?')
        ]) as model:
            result = self.start('Can I see an example for a coffee launch?')
        self.assertEqual(result['assistant_status'], 'complete')
        response_parts = model.call_args.args[0]
        text = ' '.join(p['text'] for p in response_parts if 'text' in p)
        self.assertIn('Opening morning', text)
        self.assertNotIn('From the oven', text)
        self.assertNotIn('P-041', text)
        self.assertTrue(any('inlineData' in p for p in response_parts))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM portfolio_search_requests').fetchone()[0], 0)

    def test_reference_moment_and_grounded_cue_handoff(self):
        with patch.object(assistant, 'generate', return_value=self.answer()):
            self.start()
        self.c.execute('INSERT INTO reference_assets VALUES (?,?,?,?,?,?,?)',
                       ('asset', self.i, 'cut.mp4', 'video/mp4', 'hash', 'unused', 1))
        self.c.execute("INSERT INTO reference_runs(id,asset_id,status,created_at) VALUES ('run','asset','done',1)")
        for t in range(8):
            self.c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (f'obs-{t}', 'run', t, t+1, 'frame', 'Sample', 'decoder',
                            'Approximate', f'frame-{t}.jpg', None, 1))
        self.c.execute("INSERT INTO media_notifications VALUES ('run',?,'gemini')",(self.i,))
        self.c.commit()
        cue = dict(observation_id='obs-7', body='A cut to the product', uncertainty='Sampled frame')
        with patch.object(chat, 'reference_frame', return_value={'mime':'image/jpeg','data_base64':'Zm9v'}), \
                patch.object(assistant, 'generate', return_value=self.answer(production_cues=[cue])) as model:
            _, result = self.post(self.url+'/messages', {'text':'Use this moment', 'request_id':'clip',
                'references':[{'asset_id':'asset','start':7.5,'end':7.5,'observation_id':'obs-7'}]})
        self.assertEqual(result['assistant_status'], 'complete')
        self.assertEqual(model.call_args.args[0][1]['text'], 'Frame from observation obs-7')
        saved = self.c.execute("SELECT * FROM reference_observations WHERE kind='production_cue'").fetchone()
        self.assertEqual((saved['method'], saved['supersedes']), ('Gemini agent cue', 'obs-7'))
        self.assertEqual(chat.evidence(self.c, {'analysis_id':'run'})['observations'][-1]['body'], cue['body'])

    def seed_reference(self, notification='gemini'):
        self.c.execute('INSERT INTO reference_assets VALUES (?,?,?,?,?,?,?)',
                       ('asset', self.i, 'cut.mp4', 'video/mp4', 'hash', 'unused', 1))
        self.c.execute("INSERT INTO reference_runs(id,asset_id,status,created_at) VALUES ('run','asset','done',1)")
        for t in range(8):
            self.c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (f'obs-{t}', 'run', t, t+1, 'frame', 'Sample', 'decoder',
                            'Approximate', f'frame-{t}.jpg', None, 1))
        self.c.execute("INSERT INTO media_notifications VALUES ('run',?,?)", (self.i, notification))
        self.c.commit()

    def test_selected_moment_without_pending_media_repairs_cues_to_facts(self):
        with patch.object(assistant, 'generate', return_value=self.answer()):
            self.start()
        self.seed_reference(notification='done')
        cue = dict(observation_id='obs-7', body='A cut to the product', uncertainty='Sampled frame')
        preference = {'subject': 'production.edit_style', 'value': 'this moment', 'status': 'confirmed'}
        with patch.object(chat, 'reference_frame', return_value={'mime':'image/jpeg','data_base64':'Zm9v'}), \
                patch.object(assistant, 'generate', side_effect=[
                    self.answer(production_cues=[cue]), self.answer(facts=[preference])]) as model:
            _, result = self.post(self.url+'/messages', {'text':'Use this moment', 'request_id':'clip',
                'references':[{'asset_id':'asset','start':7.5,'end':7.5}]})
        self.assertEqual(result['assistant_status'], 'complete')
        repair = json.loads(model.call_args.args[0][-1]['text'])
        self.assertEqual(repair['contract_error'], 'invalid_reference_cue')
        self.assertIs(repair['production_cues_allowed'], False)
        ctx = chat.context(self.c, {'inquiry_id': self.i})
        self.assertEqual(len(ctx['turns']), 4)
        self.assertEqual(ctx['facts'][-1]['value'], 'this moment')
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM reference_observations WHERE kind='production_cue'").fetchone()[0], 0)
        with patch.object(assistant, 'generate') as model:
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'complete')
            model.assert_not_called()

    def test_cue_with_unknown_observation_repairs_with_valid_ids(self):
        with patch.object(assistant, 'generate', return_value=self.answer()):
            self.start()
        self.seed_reference()
        bad = dict(observation_id='obs-missing', body='A cut to the product', uncertainty='Sampled frame')
        good = dict(bad, observation_id='obs-7')
        with patch.object(chat, 'reference_frame', return_value={'mime':'image/jpeg','data_base64':'Zm9v'}), \
                patch.object(assistant, 'generate', side_effect=[
                    self.answer(production_cues=[bad]), self.answer(production_cues=[good])]) as model:
            _, result = self.post(self.url+'/messages', {'text':'Use this moment', 'request_id':'clip',
                'references':[{'asset_id':'asset','start':7.5,'end':7.5}]})
        self.assertEqual(result['assistant_status'], 'complete')
        repair = json.loads(model.call_args.args[0][-1]['text'])
        self.assertEqual(repair['contract_error'], 'invalid_reference_cue')
        self.assertIs(repair['production_cues_allowed'], True)
        self.assertIn('obs-7', repair['valid_observation_ids'])
        self.assertNotIn('obs-missing', repair['valid_observation_ids'])
        saved = self.c.execute("SELECT * FROM reference_observations WHERE kind='production_cue'").fetchone()
        self.assertEqual(saved['supersedes'], 'obs-7')

    def test_grounding_repair_drops_reemitted_inherited_facts(self):
        first = {'subject': 'production.caption_requirements', 'value': 'Add captions to every reel.', 'status': 'confirmed'}
        with patch.object(assistant, 'generate', return_value=self.answer(facts=[first])):
            self.start('Add captions to every reel.')
        new = {'subject': 'production.objective', 'value': 'awareness', 'status': 'confirmed'}
        with patch.object(assistant, 'generate', side_effect=[
                self.answer(facts=[dict(first), new]), self.answer(facts=[new])]) as model:
            _, result = self.post(self.url+'/messages', {'text':'The objective is awareness', 'request_id':'next'})
        self.assertEqual(result['assistant_status'], 'complete')
        repair = json.loads(model.call_args.args[0][-1]['text'])
        self.assertEqual(repair['contract_error'], 'ungrounded_fact')
        self.assertEqual(repair['rejected_fact_index'], 0)
        self.assertEqual(repair['latest_buyer_message'], 'The objective is awareness')
        self.assertIn('production.caption_requirements', repair['already_saved_facts'])
        ctx = chat.context(self.c, {'inquiry_id': self.i})
        self.assertEqual(len(ctx['facts']), 2)
        self.assertEqual(len(ctx['turns']), 4)

    def test_fact_source_repair_lists_existing_fact_ids(self):
        fact = {'subject': 'production.objective', 'value': 'awareness', 'status': 'confirmed'}
        with patch.object(assistant, 'generate', return_value=self.answer(facts=[fact])):
            self.start('The objective is awareness')
        old = chat.context(self.c, {'inquiry_id': self.i})['facts'][0]['id']
        bad = dict(fact, value='sales', supersedes='obs-7')
        corrected = dict(fact, value='sales', supersedes=old)
        with patch.object(assistant, 'generate', side_effect=[
                self.answer(facts=[bad]), self.answer(facts=[corrected])]) as model:
            _, result = self.post(self.url+'/messages', {'text':'Actually sales', 'request_id':'correction'})
        self.assertEqual(result['assistant_status'], 'complete')
        repair = json.loads(model.call_args.args[0][-1]['text'])
        self.assertEqual(repair['contract_error'], 'invalid_fact_source')
        self.assertEqual(repair['existing_fact_ids'], {'production.objective': [old]})
        ctx = chat.context(self.c, {'inquiry_id': self.i})
        self.assertEqual(ctx['facts'][-1]['supersedes'], old)
        self.assertEqual(len(ctx['turns']), 4)

    def test_transient_rate_limit_retries_once_then_succeeds(self):
        from io import BytesIO
        from urllib.error import HTTPError
        payload = {'candidates': [{'finishReason': 'STOP', 'content': {
            'parts': [{'text': json.dumps(self.answer())}]}}]}
        limited = HTTPError('https://synthetic.invalid', 429, 'Too Many Requests', {}, None)
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'synthetic-test-key'}), \
                patch.object(assistant.time, 'sleep') as sleep, \
                patch.object(assistant, 'urlopen', side_effect=[limited, BytesIO(json.dumps(payload).encode())]) as transport:
            self.assertEqual(assistant.generate([{'text': 'Synthetic input'}]), self.answer())
        self.assertEqual(transport.call_count, 2)
        sleep.assert_called_once()
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'synthetic-test-key'}), \
                patch.object(assistant.time, 'sleep'), \
                patch.object(assistant, 'urlopen', side_effect=HTTPError('https://synthetic.invalid', 400, 'Bad Request', {}, None)) as transport:
            with self.assertRaises(HTTPError):
                assistant.generate([{'text': 'Synthetic input'}])
        self.assertEqual(transport.call_count, 1)

    def test_billing_or_quota_refusal_shows_friendly_unavailable_message(self):
        from io import BytesIO
        from urllib.error import HTTPError
        billing = HTTPError('https://synthetic.invalid', 403, 'Forbidden', {},
                            BytesIO(b'{"error":{"message":"Your prepayment credits are depleted. Check billing."}}'))
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'synthetic-test-key'}), \
                patch.object(assistant.time, 'sleep'), \
                patch.object(assistant, 'urlopen', side_effect=billing):
            with self.assertRaisesRegex(ValueError, 'model_unavailable'):
                assistant.generate([{'text': 'Synthetic input'}])
        self.start('Two reels')
        with patch.object(assistant, 'generate', side_effect=ValueError('model_unavailable')):
            status, body = self.post(self.url+'/retry', {})
        self.assertEqual(body['assistant_status'], 'retry')
        self.assertIn('temporarily unavailable', body['assistant_error'])
        self.assertIn('Your message is saved', body['assistant_error'])

    def test_persistent_rate_limit_reports_http_category_without_content(self):
        from urllib.error import HTTPError
        self.start('Two reels')
        with patch.object(assistant, 'generate', side_effect=HTTPError('https://synthetic.invalid', 429, 'Too Many Requests', {}, None)):
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'retry')
        events = [dict(r) for r in self.c.execute('SELECT * FROM gemini_attempt_events')]
        encoded = json.dumps(events)
        self.assertIn('transport_http_429', encoded)
        self.assertNotIn('Two reels', encoded)
        self.assertNotIn('synthetic.invalid', encoded)
        self.assertEqual(len(chat.context(self.c, {'inquiry_id': self.i})['turns']), 1)

    def test_founder_submitted_video_receipt_is_private_and_frozen(self):
        self.test_reference_moment_and_grounded_cue_handoff()
        with patch.object(assistant, 'generate', return_value=self.answer()):
            self.post(self.url+'/messages', {'text':'<script>alert(1)</script> Prefer the later cut',
                'request_id':'timestamp-only', 'references':[{'asset_id':'asset','start':6.25,'end':6.5}]})
        ctx = chat.context(self.c, {'inquiry_id':self.i})
        assistant.submit_brief(self.c, self.i, ctx['revision'], ctx['processed_sequence'])
        self.assertEqual(server.state(self.c)['deals'], [])
        snapshot = json.loads(self.c.execute('SELECT snapshot_json FROM studio_submissions').fetchone()[0])
        self.assertEqual(snapshot['reference_evidence'][0]['observations'][-1]['kind'], 'production_cue')
        self.assertEqual(request(self.base, 'GET', '/submitted-briefs')[0], 403)
        headers = {'X-Owner-Token':self.http.owner_token}
        code, html = request(self.base, 'GET', '/submitted-briefs', headers=headers)
        self.assertEqual(code, 200)
        for text in ('6.25 seconds', '6.5 seconds', '7.5 seconds', 'no observation linkage supplied',
                     'source observation: obs-7', 'Uncertainty: Sampled frame', '&lt;script&gt;'):
            self.assertIn(text, html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertNotIn('buyer@example', html)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(extra_http_headers=headers, viewport={'width':1100,'height':900})
            page.goto(self.base+'/submitted-briefs')
            self.assertEqual(page.get_by_role('heading', name='Submitted briefs', exact=True).count(), 1)
            self.assertEqual(page.locator('script').count(), 0)
            self.assertTrue(page.get_by_text('Buyer-selected timestamp; no observation linkage supplied.', exact=False).is_visible())
            if os.environ.get('FOUNDER_BRIEF_SCREENSHOT'):
                page.screenshot(path=os.environ['FOUNDER_BRIEF_SCREENSHOT'], full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
            browser.close()
        self.assertIn('/submitted-briefs', request(self.base, 'GET', '/dashboard', headers=headers)[1])
        with patch.object(assistant, 'generate', return_value=self.answer()):
            self.post(self.url+'/messages', {'text':'Unsubmitted new preference', 'request_id':'late'})
        with self.c:
            self.c.execute("UPDATE reference_observations SET body='Later analysis' WHERE kind='production_cue'")
        html = request(self.base, 'GET', '/submitted-briefs', headers=headers)[1]
        self.assertIn('Later buyer changes exist', html)
        self.assertNotIn('Unsubmitted new preference', html)
        self.assertNotIn('Later analysis', html)
        client = request(self.base, 'GET', self.url+'/state')[1]
        self.assertNotIn('reference_evidence', client)
        self.assertNotIn('Immutable submitted revision', client)
        self.assertNotIn('snapshot_json', client)
        with self.c:
            legacy = dict(snapshot)
            legacy.pop('reference_evidence')
            self.c.execute('UPDATE studio_submissions SET snapshot_json=?', (json.dumps(legacy),))
        html = request(self.base, 'GET', '/submitted-briefs', headers=headers)[1]
        self.assertIn('Legacy receipt', html)
        self.assertNotIn('Later analysis', html)

    def test_founder_receipts_show_fact_correction_history(self):
        self.test_corrections_email_and_immutable_handoff()
        html = server.submitted_briefs_page(self.c)
        self.assertIn('[superseded]', html)
        self.assertIn('; replaces ', html)
        self.assertIn('Source buyer turn ', html)
        self.assertEqual(html.count('<h2>Submitted brief</h2>'), 2)

    def test_normalized_deadline_facts_require_a_matching_buyer_date(self):
        changes = [{'subject':'production.deadline_kind','value':'first_cut','status':'confirmed'},
                   {'subject':'production.first_cut_date','value':'2026-12-11','status':'confirmed'}]
        with patch.object(assistant, 'generate', return_value=self.answer(facts=changes)):
            result = self.start('Please send the first cut on 11 December 2026.')
        self.assertEqual(result['assistant_status'], 'complete')
        with patch.object(assistant, 'generate', return_value=self.answer(facts=changes)):
            _, result = self.post(self.url+'/messages', {'text':'The first cut date is undecided.', 'request_id':'new'})
        self.assertEqual(result['assistant_status'], 'retry')

    def test_declined_submission_gets_one_buyer_safe_followup(self):
        with patch.object(assistant, 'generate', return_value=self.answer()):
            self.start()
        ctx = chat.context(self.c, {'inquiry_id':self.i})
        rev = ctx['revision']
        self.post(self.url+'/submit', {'revision':rev, 'sequence':ctx['processed_sequence']})
        with self.c:
            self.c.execute("INSERT INTO deals(id,source,source_id,status) VALUES ('d','site','s','declined')")
            self.c.execute("UPDATE inquiries SET deal_id='d' WHERE id=?", (self.i,))
            self.c.execute("INSERT INTO priced_briefs VALUES ('o',?,?, 'digest')", (self.i, rev))
            self.c.execute("INSERT INTO evaluations(id,offer_id,verdict,reason_codes_json,result_json) VALUES ('e','o','decline',?,?)",
                           (json.dumps(['BELOW_FLOOR']), json.dumps({'private_estimate': 7891})))
        _, raw = request(self.base, 'GET', self.url+'/state')
        self.assertTrue(json.loads(raw)['review_question'])
        with patch.object(assistant, 'generate', side_effect=OSError('synthetic provider failure')):
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'retry')
        _, raw = request(self.base, 'GET', self.url+'/state')
        self.assertIsNotNone(json.loads(raw)['assistant_error'])
        with patch.object(assistant, 'generate', return_value=self.answer('Which edits are essential?')) as model:
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'complete')
            self.assertEqual(self.post(self.url+'/retry', {})[1]['assistant_status'], 'complete')
            self.assertEqual(model.call_count, 1)
            self.assertNotIn('7891', json.dumps(model.call_args.args))
        self.assertEqual(chat.context(self.c, {'inquiry_id':self.i})['revision'], rev)

    def test_media_only_update_and_stale_lease(self):
        with patch.object(assistant,'generate',return_value=self.answer()):
            self.start()
        self.c.execute('INSERT INTO reference_assets VALUES (?,?,?,?,?,?,?)', ('asset',self.i,'sample.mp4','video/mp4','hash','unused',1))
        self.c.execute("INSERT INTO reference_runs(id,asset_id,status,error,created_at) VALUES ('run','asset','failed','Synthetic decode failure',1)")
        self.c.execute("INSERT INTO media_notifications VALUES ('run',?,'gemini')",(self.i,))
        self.c.commit()
        with patch.object(assistant,'generate',return_value=self.answer('I could not decode the reference. Which aspect should guide the edit?')):
            self.assertEqual(self.post(self.url+'/retry',{})[1]['assistant_status'],'complete')
        self.assertEqual(self.c.execute('SELECT status FROM media_notifications').fetchone()[0],'done')
        with self.assertRaisesRegex(ValueError,'stale_lease'):
            chat.commit(self.c,{'inquiry_id':self.i,'through_sequence':1,'expected_revision':2,'request_id':'stale','reply':'Thanks'},gemini_lease='expired')


if __name__ == '__main__':
    unittest.main()
