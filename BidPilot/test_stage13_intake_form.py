"""Public intake stays separate from deals until an agent claims it."""
import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
import re
from pathlib import Path

import db
import server
from test_mcp import Client, INQUIRY, tool_result

FIELDS = dict(name_business='Juniper Cafe', reply_email='sample@example.test',
              project_brief='Sample product promo', video_format='product_promo',
              production_mode='one_shoot_day', shoot_days=1, locations=1,
              interviewees=0, scripting='light', deliverables=3,
              editing_level='standard', graphics='basic_titles', revision_rounds=2,
              caption_versions=3, caption_every_deliverable=True,
              deadline='2026-12-11', budget_sgd=2700, travel_kit_talent='',
              request_id='stage13-request')

class IntakeFormTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.db')
        self.http, self.owner = server.make_server(self.path)
        self.port = self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        server._quote_times.clear()
        self.client = Client(self.path)

    def tearDown(self):
        self.client.close()
        self.client.proc.stdout.close()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = (response.status, response.read().decode())
        conn.close()
        return result

    def post(self, payload, origin=None):
        return self.request('POST', '/quote', payload, {'Content-Type':'application/json',
            'Origin': origin or f'http://127.0.0.1:{self.port}'})

    def test_form_auth_and_invalid(self):
        status, page = self.request('GET', '/quote')
        self.assertEqual(status, 200)
        self.assertNotIn(server.SAMPLE_BADGE, page)
        for key in FIELDS:
            self.assertIn(f'name="{key}"', page)
        self.assertIn('Caption every deliverable', page)
        self.assertNotIn('demo preset', page)
        self.assertNotIn('direct_costs', page)
        self.assertEqual(self.request('GET', '/')[0], 403)
        self.assertEqual(self.request('GET', '/api/state')[0], 403)
        self.assertEqual(self.post('{}', 'http://evil.test')[0], 403)
        self.assertEqual(self.post('{}', f'https://127.0.0.1:{self.port}')[0], 403)
        self.assertEqual(self.post('{')[0], 400)
        self.assertEqual(self.post(' ' * 16385)[0], 400)
        self.assertEqual(self.post(json.dumps({**FIELDS, 'shoot_days': 'one'}))[0], 400)
        self.assertEqual(self.post(json.dumps({**FIELDS, 'budget_sgd': 10 ** 400}))[0], 400)
        self.assertEqual(self.post(json.dumps({**FIELDS, 'project_brief': '\ud800'}))[0], 400)

    def test_form_encoded_and_owner_inbox(self):
        fields = {**FIELDS, 'request_id': 'html-request'}
        fields['caption_every_deliverable'] = 'on'
        body = urllib.parse.urlencode(fields)
        status, receipt = self.request('POST', '/quote', body, {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': f'http://127.0.0.1:{self.port}'})
        self.assertEqual(status, 200)
        self.assertIn('Request received', receipt)
        self.assertNotIn('$', receipt)
        self.assertNotIn('2026-', receipt)
        status, state_body = self.request('GET', '/api/state', headers={'X-Owner-Token': self.owner})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(state_body)['inbox']), 1)
        self.assertFalse(json.loads(state_body)['inbox'][0]['claimed'])

    def test_receipt_claim_and_retry(self):
        payload = json.dumps(FIELDS)
        status, body = self.post(payload)
        self.assertEqual(status, 200)
        self.assertNotIn('$', body)
        self.assertNotIn('hour', body.lower())
        receipt = json.loads(body)
        self.assertEqual(json.loads(self.post(payload)[1]), receipt)
        self.assertEqual(self.post(json.dumps({**FIELDS, 'project_brief': 'changed'}))[0], 409)
        conn = db.connect(self.path)
        self.assertEqual(conn.execute('SELECT count(*) FROM inbox').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT count(*) FROM deals').fetchone()[0], 0)
        conn.close()
        pending = tool_result(self.client.call_tool('list_pending_work', {}))
        self.assertIn(receipt['source_id'], [x['source_id'] for x in pending['inquiries']])
        args = dict(source='site', source_id=receipt['source_id'], client='Juniper Cafe',
                    raw_inquiry=FIELDS['project_brief'], offer_request=INQUIRY)
        first = tool_result(self.client.call_tool('claim_intake', args))
        second = tool_result(self.client.call_tool('claim_intake', args))
        self.assertEqual(first['deal_id'], second['deal_id'])
        pending = tool_result(self.client.call_tool('list_pending_work', {}))
        self.assertNotIn(receipt['source_id'], [x['source_id'] for x in pending['inquiries']])

    def test_form_resume_proof_and_same_deal(self):
        _, page = self.request('GET','/quote')
        req = re.search(r'name="request_id" value="([^"]+)', page).group(1)
        proof = re.search(r'name="resume_proof" value="([^"]+)', page).group(1)
        fields = {**FIELDS, 'request_id':req, 'resume_proof':proof, 'caption_every_deliverable':'on'}
        headers = {'Content-Type':'application/x-www-form-urlencoded','Origin':f'http://127.0.0.1:{self.port}'}
        status, receipt = self.request('POST','/quote',urllib.parse.urlencode(fields),headers)
        self.assertEqual(status,200)
        self.assertIn('/i/'+proof,receipt)
        self.assertEqual(self.request('GET','/i/'+proof+'/state')[0],200)
        forged = json.loads(self.post(json.dumps({**FIELDS,'request_id':req,'resume_proof':'a'*43}))[1])
        self.assertNotIn('inquiry_url',forged)
        conn=db.connect(self.path)
        self.assertNotIn(proof, '\n'.join(str(tuple(r)) for r in conn.execute('SELECT * FROM mcp_operations')))
        row=conn.execute('SELECT r.source_id,r.inquiry_id FROM inquiry_receipts r').fetchone()
        source_id,inquiry_id=row['source_id'],row['inquiry_id']
        conn.execute('UPDATE inquiries SET processed_seq=1 WHERE id=?',(inquiry_id,))
        conn.execute("UPDATE inquiry_jobs SET status='done' WHERE inquiry_id=?",(inquiry_id,))
        conn.commit(); conn.close()
        priced=tool_result(self.client.call_tool('price_inquiry_scope',{'inquiry_id':inquiry_id,'expected_revision':0,'offer_request':INQUIRY}))
        claimed=tool_result(self.client.call_tool('claim_intake',dict(source='site',source_id=source_id,client='Juniper Cafe',raw_inquiry=FIELDS['project_brief'],offer_request=INQUIRY)))
        self.assertEqual(priced['deal_id'],claimed['deal_id'])

if __name__ == '__main__':
    unittest.main()
