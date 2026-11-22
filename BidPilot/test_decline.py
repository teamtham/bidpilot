"""Isolated normal MCP dispatch tests, not agent-selection evidence."""
import json

import db
import test_studio_resume


class DeclineClarification(test_studio_resume.StudioResume):
    test_structured_resolution_converges = None
    test_existing_clarification_without_fingerprint_resumes = None
    test_private_chat_identical_revision_resumes = None

    def test_decline_question_once_and_revised_complete_scope(self):
        self.register()
        request = self.request()
        request['offer']['budget_sgd'] = 1800
        request['offer']['production_mode'] = 'edit_only'
        request['offer']['shoot_days'] = 0
        request['offer']['locations'] = 0
        request['production_facts']['production.original_edits'] = 3
        request['production_facts']['production.cutdowns'] = 0
        status, raw = self.http('POST', '/inquire', {'text': 'Three originals, zero cutdowns, selected footage, budget 1800.', 'request_id': 'start'})
        self.assertEqual(status, 200, raw)
        started = json.loads(raw)
        inquiry = started['inquiry_id']
        self.rpc('commit_chat_turn', {'inquiry_id': inquiry, 'through_sequence': 1,
            'expected_revision': 0, 'request_id': 'initial', 'reply': 'Your request is noted.',
            'fact_changes': [{'subject': key, 'value': value, 'status': 'confirmed', 'source_sequence': 1}
                             for key, value in request['production_facts'].items()]})
        original = self.rpc('price_inquiry_scope', {'inquiry_id': inquiry, 'expected_revision': 1, 'offer_request': request})
        self.assertEqual(self.verdict(original['evaluation_id']), 'decline')
        question = {'inquiry_id': inquiry, 'evaluation_id': original['evaluation_id'],
                    'through_sequence': 1, 'expected_revision': 1,
                    'reply': 'Would one main reel and two shorter versions work, or must each tell a separate story?'}
        first = self.rpc('ask_decline_question', question)
        self.assertEqual(self.rpc('ask_decline_question', question), first)
        context = self.rpc('get_conversation_context', {'inquiry_id': inquiry})
        self.assertEqual(context['deal_id'], original['deal_id'])
        self.assertEqual(sum(t['body'] == question['reply'] for t in context['turns']), 1)
        status, raw = self.http('POST', '/i/'+started['token']+'/messages',
                                {'text': 'One original and two cutdowns work.', 'request_id': 'agree'})
        self.assertEqual(status, 200, raw)
        seq = json.loads(raw)['sequence']
        facts = {f['subject']: f for f in context['facts']}
        self.rpc('commit_chat_turn', {'inquiry_id': inquiry, 'through_sequence': seq,
            'expected_revision': 1, 'request_id': 'accepted', 'reply': 'I will bring the revised scope for review.',
            'fact_changes': [{'subject': key, 'value': value, 'status': 'confirmed', 'source_sequence': seq,
                             'supersedes': facts[key]['id']} for key, value in
                            [('production.original_edits', 1), ('production.cutdowns', 2)]]})
        request['production_facts']['production.original_edits'] = 1
        request['production_facts']['production.cutdowns'] = 2
        args = {'inquiry_id': inquiry, 'expected_revision': 2, 'offer_request': request}
        revised = self.rpc('price_inquiry_scope', args)
        self.assertEqual(self.rpc('price_inquiry_scope', args), revised)
        self.assertEqual(revised['deal_id'], original['deal_id'])
        self.assertEqual(self.verdict(original['evaluation_id']), 'decline')
        self.assertEqual(self.verdict(revised['evaluation_id']), 'accept')
        composed = self.rpc('compose_proposal_page', {'version_id': revised['version_id'],
                            'page': {'headline': 'Revised launch package', 'introduction': 'A main story with shorter versions for review.'}})
        checked = self.rpc('check_draft', {'version_id': composed['version_id']})
        self.assertTrue(checked['outcome']['passed'], checked)
        conn = db.connect(self.path)
        try:
            self.assertEqual(conn.execute('SELECT count(*) FROM evaluations').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT count(*) FROM resource_allocations').fetchone()[0], 0)
        finally:
            conn.close()
