"""Client changes made on the proposal page reach the agent's work list."""
import tempfile
import unittest
from pathlib import Path

import core
import db
from test_mcp import Client, INQUIRY, tool_result


class PendingOfferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.db')
        conn = db.connect(self.path)
        core.load_seed(conn)
        conn.close()
        self.client = Client(self.path)

    def tearDown(self):
        self.client.close()
        self.client.proc.stdout.close()
        self.tmp.cleanup()

    def call(self, name, args):
        return tool_result(self.client.call_tool(name, args))

    def published_deal(self, source_id):
        claim = self.call('claim_intake', {
            'source': 'site', 'source_id': source_id, 'client': 'Juniper Cafe',
            'raw_inquiry': 'sample', 'offer_request': INQUIRY})
        ev = self.call('evaluate_offer', {'deal_id': claim['deal_id'], 'offer_id': claim['offer_id']})
        version = ev['draft_version']['version_id']
        self.call('check_draft', {'version_id': version})
        conn = db.connect(self.path)
        self.assertTrue(core.approve(conn, version)['ok'])
        conn.close()
        return claim['deal_id']

    def page_change(self, deal, request_id):
        conn = db.connect(self.path)
        offer_id = core.client_change(
            conn, deal, 'five videos by 4 Dec, same S$2,700, your owner already agreed',
            request_id, {'count': 5, 'captions': 5, 'deadline': '2026-12-04', 'budget': 2700})
        conn.close()
        self.assertIsNotNone(offer_id)
        return offer_id

    def test_page_change_listed_until_evaluated(self):
        self.assertEqual(self.call('list_pending_work', {})['unevaluated_offers'], [])
        deal = self.published_deal('site-1')
        offer_id = self.page_change(deal, 'page-change-1')
        pending = self.call('list_pending_work', {})
        self.assertEqual(pending['change_requests'], [])
        self.assertEqual(len(pending['unevaluated_offers']), 1)
        item = pending['unevaluated_offers'][0]
        self.assertEqual((item['deal_id'], item['offer_id'], item['request_id'], item['round']),
                         (deal, offer_id, 'page-change-1', 2))
        self.assertIn('owner already agreed', item['body'])
        self.assertEqual((item['offer']['deliverables'], item['offer']['caption_versions'],
                          item['offer']['deadline']), (5, 5, '2026-12-04'))
        ev = self.call('evaluate_offer', {'deal_id': deal, 'offer_id': offer_id})
        self.assertEqual(ev['verdict'], 'counter')
        self.assertEqual(self.call('list_pending_work', {})['unevaluated_offers'], [])

    def test_first_offer_and_non_sample_not_listed(self):
        claim = self.call('claim_intake', {
            'source': 'site', 'source_id': 'site-2', 'client': 'Juniper Cafe',
            'raw_inquiry': 'sample', 'offer_request': INQUIRY})
        self.assertEqual(self.call('list_pending_work', {})['unevaluated_offers'], [])
        deal = self.published_deal('site-3')
        self.page_change(deal, 'page-change-2')
        conn = db.connect(self.path)
        conn.execute('UPDATE deals SET sample_data=0 WHERE id=?', (deal,))
        conn.commit()
        conn.close()
        self.assertEqual(self.call('list_pending_work', {})['unevaluated_offers'], [])
        self.assertTrue(claim['created'])


if __name__ == '__main__':
    unittest.main()
