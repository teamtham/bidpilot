"""In-process HTTP and MCP journey; no network socket needed."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import conversation
import core
import db
import mcp_server
import server
from test_http import INQUIRY, RAW1


class Socket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()
    def makefile(self, mode, *args, **kwargs):
        return self.input if 'r' in mode else self.output
    def sendall(self, data):
        self.output.write(data)
    def close(self):
        pass


class Journey(unittest.TestCase):
    def test_configured_buyer_origin_is_separate_from_founder(self):
        handler = object.__new__(server.Handler)
        handler.server = types.SimpleNamespace(public_origin="https://brief.example", owner_token="test-owner")
        handler.headers = {"Host": "brief.example", "Origin": "https://brief.example", "X-Owner-Token": "test-owner"}
        self.assertTrue(handler.same_origin())
        self.assertFalse(handler.owner_ok())
        handler.headers["Origin"] = "https://different.example"
        self.assertFalse(handler.same_origin())
        for value in ("https://brief.example/path", "http://brief.example", "https://brief.example:66000"):
            with self.assertRaises(ValueError):
                server.https_origin(value)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'journey.db')
        c = db.connect(self.path)
        core.load_seed(c)
        c.close()
        self.host = types.SimpleNamespace(db_path=self.path, owner_token='owner', server_name='localhost', server_port=80)
        self.db_patch = patch.object(mcp_server, 'DB_PATH', self.path)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def http(self, method, path, body=None, owner=False):
        content = json.dumps(body).encode() if body is not None else b''
        headers = [f'{method} {path} HTTP/1.0', 'Host: localhost', 'Origin: http://localhost', f'Content-Length: {len(content)}', 'Content-Type: application/json']
        if owner: headers.append('X-Owner-Token: owner')
        sock = Socket(('\r\n'.join(headers)+'\r\n\r\n').encode() + content)
        server.Handler(sock, ('127.0.0.1', 1), self.host)
        raw = sock.output.getvalue()
        head, body = raw.split(b'\r\n\r\n',1)
        return int(head.split(b' ', 2)[1]), body

    def rpc(self, name, args):
        msg={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}}
        response=mcp_server.handle(msg)
        self.assertNotIn('error',response,response)
        result=response['result']
        self.assertFalse(result.get('isError'),result)
        return json.loads(result['content'][0]['text'])

    def test_inquiry_to_confirmed_booking(self):
        for url in ('/', '/dashboard', '/api/state', '/estimating', '/preview/nope'):
            status,_=self.http('GET',url)
            self.assertEqual(status,403,url)
        status,raw=self.http('POST','/api/approve',{'version_id':'anything'})
        self.assertEqual(status,403)
        status,raw=self.http('POST','/inquire',{'text':RAW1,'request_id':'start'})
        self.assertEqual(status,200,raw)
        started=json.loads(raw); inquiry=started['inquiry_id']; base='/i/'+started['token']
        status,raw=self.http('GET',base+'/state'); self.assertEqual(status,200,raw)
        self.assertEqual(json.loads(raw)['turns'][0]['body'],RAW1)
        self.rpc('commit_chat_turn',{'inquiry_id':inquiry,'through_sequence':1,'expected_revision':0,'request_id':'reply', 'reply':'Thanks for sharing your project.', 'fact_changes':[]})
        sample=Path(__file__).parent/'sample-portfolio'/'cafe.mp4'
        status,raw=self.http('POST',base+'/upload',{'filename':'cafe.mp4','mime':'video/mp4','data':base64.b64encode(sample.read_bytes()).decode()})
        self.assertEqual(status,200,raw); asset=json.loads(raw)
        self.assertTrue(conversation.process_media_once(self.path))
        evidence=self.rpc('get_reference_evidence',{'analysis_id':asset['analysis_id']})
        self.assertEqual(evidence['status'],'done',evidence)
        pending=self.rpc('list_pending_work',{})
        self.assertIn(asset['analysis_id'],json.dumps(pending))
        self.rpc('acknowledge_reference_update',{'inquiry_id':inquiry,'analysis_id':asset['analysis_id']})
        priced=self.rpc('price_inquiry_scope',{'inquiry_id':inquiry,'expected_revision':0,'offer_request':INQUIRY})
        composed=self.rpc('compose_proposal_page',{'version_id':priced['version_id'],'page':{'headline':'Your project','introduction':'A proposal for your review.'}})
        version=composed['version_id']
        status,raw=self.http('POST','/api/check',{'version_id':version},True); self.assertTrue(json.loads(raw)['ok'],raw)
        status,raw=self.http('POST','/api/approve',{'version_id':version},True); self.assertEqual(status,200,raw)
        status,raw=self.http('GET',base+'/state'); self.assertEqual(status,200,raw)
        link=json.loads(raw)['proposal_url']
        status,raw=self.http('GET',link); self.assertEqual(status,200,raw)
        self.assertIn(b'Your project',raw)
        c=db.connect(self.path)
        ids=json.loads(c.execute('SELECT public_option_ids_json FROM proposal_versions WHERE id=?',(version,)).fetchone()[0]); c.close()
        status,raw=self.http('POST',link+'/accept',{'option_id':ids[0]}); self.assertEqual(status,200,raw)
        status,raw=self.http('POST','/api/confirm-booking',{'version_id':version},True); self.assertEqual(status,200,raw)
        self.assertTrue(json.loads(raw)['ok'],raw)

    def test_conversation_scoping_to_detailed_proposal(self):
        brief = ('We are Morning Table, a fictional cafe. We want three short Instagram promos '
                 'for our breakfast counter. We already have espresso footage.')
        status, raw = self.http('POST', '/inquire', {'text': brief, 'request_id': 'start'})
        self.assertEqual(status, 200, raw)
        started = json.loads(raw); inquiry = started['inquiry_id']; base = '/i/' + started['token']
        # Turn 1: the agent records buyer-grounded facts and asks a scoping question.
        self.rpc('commit_chat_turn', {
            'inquiry_id': inquiry, 'through_sequence': 1, 'expected_revision': 0,
            'request_id': 'reply-1', 'reply': 'Lovely brief. Do you have footage of the counter and food, or should we plan a short shoot?',
            'fact_changes': [
                {'subject': 'videos', 'value': 3, 'status': 'confirmed', 'source_sequence': 1},
                {'subject': 'platform', 'value': 'instagram', 'status': 'confirmed', 'source_sequence': 1},
                {'subject': 'client_supplied_footage', 'value': 'espresso footage already shot', 'status': 'confirmed', 'source_sequence': 1}]})
        # Reference upload, analysis, and a grounded production cue.
        sample = Path(__file__).parent / 'sample-portfolio' / 'cafe.mp4'
        status, raw = self.http('POST', base + '/upload', {'filename': 'cafe.mp4', 'mime': 'video/mp4',
                                                           'data': base64.b64encode(sample.read_bytes()).decode()})
        self.assertEqual(status, 200, raw); asset = json.loads(raw)
        self.assertTrue(conversation.process_media_once(self.path))
        evidence = self.rpc('get_reference_evidence', {'analysis_id': asset['analysis_id']})
        self.assertEqual(evidence['status'], 'done', evidence)
        self.assertEqual(evidence['result']['transcription']['status'], 'no_audio')
        frame = next(o for o in evidence['observations'] if o['kind'] == 'frame')
        self.rpc('commit_chat_turn', {
            'inquiry_id': inquiry, 'through_sequence': 1, 'expected_revision': 1,
            'request_id': 'reply-media', 'reply': 'The reference opens with a warm serif title card; we can match that opening beat.',
            'fact_changes': [], 'media_analysis_id': asset['analysis_id'],
            'production_cues': [{'observation_id': frame['id'],
                                 'body': 'Open each promo with a warm cream-and-terracotta serif title card like the reference.',
                                 'uncertainty': 'Style inferred from one sampled frame; palette to be confirmed.'}]})
        # Buyer correction: captions on all three. The brief revision advances.
        status, raw = self.http('POST', base + '/messages', {'text': 'Please add captions to all three promos.', 'request_id': 'm2'})
        self.assertEqual(status, 200, raw)
        seq2 = json.loads(raw)['sequence']
        self.rpc('commit_chat_turn', {
            'inquiry_id': inquiry, 'through_sequence': seq2, 'expected_revision': 1,
            'request_id': 'reply-2', 'reply': 'Captions on all three promos — noted.',
            'fact_changes': [{'subject': 'captions', 'value': 3, 'status': 'confirmed', 'source_sequence': seq2}]})
        context = self.rpc('get_conversation_context', {'inquiry_id': inquiry})
        self.assertEqual(context['revision'], 2)
        videos_fact = next(f['id'] for f in context['facts'] if f['subject'] == 'videos')
        cue = next(o for o in self.rpc('get_reference_evidence', {'analysis_id': asset['analysis_id']})['observations']
                   if o['kind'] == 'production_cue')
        # Founder-approved rate card and calendar, then the detailed scope:
        # one shared shoot, supplied espresso footage, three masters, one
        # shared caption design with per-version caption timing.
        status, raw = self.http('POST', '/api/estimating/rates',
                                {'version': 'studio-journey', 'rates': [
                                    {'role': 'producer', 'cost_rate_cents': 4500, 'sell_rate_cents': 9000},
                                    {'role': 'editor', 'cost_rate_cents': 4500, 'sell_rate_cents': 9000}]}, True)
        self.assertEqual(status, 200, raw)
        status, raw = self.http('POST', '/api/estimating/availability',
                                {'resource': 'producer', 'start_at': '2026-11-23T09:00', 'end_at': '2026-12-11T17:00'}, True)
        self.assertEqual(status, 200, raw)
        def item(ident, role, q, h, **extra):
            return {'id': ident, 'role': role, 'basis': 'producer override 2026-11-21',
                    'quantity': q, 'unit_hours': h, 'cost_rate_cents': 4500, 'sell_rate_cents': 9000, **extra}
        spec = {
            'rate_version': 'studio-journey', 'buffer_pct': 20, 'floor_rate_cents': 7000,
            'setups': [{'id': 'shoot-1', 'kind': 'shoot', 'location': 'cafe', 'date': '2026-11-24', 'min_call_hours': 4},
                       {'id': 'post-1', 'kind': 'post'}],
            'items': [
                item('brief', 'producer', 1, 3),
                item('shoot', 'producer', 1, 5, setup_id='shoot-1', evidence=[{'kind': 'fact', 'id': videos_fact}]),
                item('ingest', 'producer', 1, 1, supplied=True),
                item('masters', 'editor', 3, 4, setup_id='post-1', deliverables=['reel-1', 'reel-2', 'reel-3'],
                     evidence=[{'kind': 'observation', 'id': cue['id']}]),
                item('caption-design', 'editor', 1, 1, setup_id='post-1'),
                item('captions', 'editor', 3, 0.5, setup_id='post-1'),
                item('review', 'editor', 1, 3, setup_id='post-1')],
            'expenses': [],
            'tasks': [
                {'id': 'prep', 'item_ids': ['brief'], 'minutes': 216, 'resources': ['producer'],
                 'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'},
                {'id': 'filming', 'kind': 'shoot', 'setup_id': 'shoot-1', 'item_ids': ['shoot'], 'minutes': 360,
                 'resources': ['producer'], 'depends_on': ['prep'],
                 'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'},
                {'id': 'ingest-task', 'item_ids': ['ingest'], 'minutes': 72, 'resources': ['producer'],
                 'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'},
                {'id': 'edit', 'item_ids': ['masters', 'caption-design', 'captions', 'review'], 'minutes': 1260,
                 'resources': ['producer'], 'depends_on': ['filming', 'ingest-task'],
                 'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'}]}
        offer_request = {'as_of': '2026-11-23', 'comparable_ids': [], 'offer': {
            'client': 'Morning Table', 'format': 'product_promo', 'production_mode': 'one_shoot_day',
            'shoot_days': 1, 'locations': 1, 'interviewees': 0, 'scripting': 'light', 'deliverables': 3,
            'editing_level': 'standard', 'graphics': 'basic_titles', 'revision_rounds': 2, 'caption_versions': 3,
            'deadline': '2026-12-11', 'budget_sgd': 4000, 'direct_costs_sgd': 0}, 'detailed_spec': spec}
        priced = self.rpc('price_inquiry_scope', {'inquiry_id': inquiry, 'expected_revision': 2,
                                                  'offer_request': offer_request})
        self.assertEqual(priced['scope_revision'], 2)
        composed = self.rpc('compose_proposal_page', {'version_id': priced['version_id'], 'page': {
            'headline': 'Your breakfast counter launch',
            'introduction': 'Short Instagram promos in the warm title style of your reference.',
            'sections': [{'title': 'The plan', 'body': 'A shared shoot for the counter and food scenes, your espresso footage reused, and captions on every promo.'}]}})
        version = composed['version_id']
        checked = self.rpc('check_draft', {'version_id': version})
        self.assertTrue(checked['outcome']['passed'], checked)
        # A new buyer message blocks release until the agent answers it.
        status, raw = self.http('POST', base + '/messages', {'text': 'Can we talk about timing too?', 'request_id': 'm3'})
        self.assertEqual(status, 200, raw)
        seq3 = json.loads(raw)['sequence']
        status, raw = self.http('POST', '/api/approve', {'version_id': version}, True)
        self.assertFalse(json.loads(raw)['ok'], raw)
        self.assertIn(b'buyer_reply_pending', raw)
        self.rpc('commit_chat_turn', {'inquiry_id': inquiry, 'through_sequence': seq3, 'expected_revision': 2,
                                      'request_id': 'reply-3',
                                      'reply': 'Happy to talk timing — the proposal includes a delivery date for your review.',
                                      'fact_changes': []})
        status, raw = self.http('POST', '/api/approve', {'version_id': version}, True)
        self.assertEqual(status, 200, raw)
        self.assertTrue(json.loads(raw)['ok'], raw)
        # The proposal appears in the same inquiry; acceptance and booking
        # recheck and reserve the detailed resource schedule.
        status, raw = self.http('GET', base + '/state'); self.assertEqual(status, 200, raw)
        link = json.loads(raw)['proposal_url']
        status, raw = self.http('GET', link); self.assertEqual(status, 200, raw)
        self.assertIn(b'Your breakfast counter launch', raw)
        c = db.connect(self.path)
        ids = json.loads(c.execute('SELECT public_option_ids_json FROM proposal_versions WHERE id=?', (version,)).fetchone()[0]); c.close()
        status, raw = self.http('POST', link + '/accept', {'option_id': ids[0]}); self.assertEqual(status, 200, raw)
        status, raw = self.http('POST', '/api/confirm-booking', {'version_id': version}, True)
        self.assertEqual(status, 200, raw)
        self.assertTrue(json.loads(raw)['ok'], raw)
        c = db.connect(self.path)
        reserved = c.execute('SELECT COUNT(*) FROM resource_allocations').fetchone()[0]; c.close()
        self.assertGreater(reserved, 0)

if __name__=='__main__': unittest.main()
