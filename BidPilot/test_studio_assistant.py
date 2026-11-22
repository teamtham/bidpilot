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

    def post(self, path, data):
        code, raw = request(self.base, 'POST', path, data, headers={'Origin':self.base})
        return code, json.loads(raw)

    def start(self, text='Two reels'):
        code, result = self.post('/inquire', {'text': text, 'request_id': 'start'})
        self.assertEqual(code, 200)
        self.i = result['inquiry_id']
        self.url = '/i/' + result['token']
        return result

    def answer(self, reply='What should viewers do after watching?', facts=None, **extra):
        return {'reply': reply, 'fact_changes': facts or [], **extra}

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

    def test_model_context_current_inquiry_only(self):
        self.start()
        other = chat.create_inquiry(self.c, {'text':'OTHER CLIENT SECRET'})
        ctx = chat.context(self.c, {'inquiry_id':self.i})
        ctx['pricing_history'] = [{'private_floor':1234}]
        text = json.dumps(assistant.model_input(self.c,ctx,None))
        self.assertNotIn('OTHER CLIENT',text)
        self.assertNotIn('private_floor',text)
        self.assertNotIn(other['inquiry_id'],text)

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
