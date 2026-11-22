"""Calendar edits must affect the same capacity used for estimates and booking."""
import unittest
import copy
import capacity
import commerce
import core
import db
import server
from test_mcp import INQUIRY


class CalendarDashboardTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(':memory:')
        core.load_seed(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_hours_and_blocks_change_ledger_and_engine_data(self):
        baseline = capacity.ledger(self.conn)['2026-11-27']['budget']
        self.assertEqual(baseline, 1800)
        capacity.change_calendar(self.conn, 'hours', {'hours': 40})
        self.assertEqual(capacity.ledger(self.conn)['2026-11-27']['budget'], 2700)
        self.assertEqual(capacity.change_calendar(self.conn, 'hours', {'hours': 40}), 1)
        panel = server.capacity_panel(self.conn)
        self.assertEqual(panel['working_hours'], 40)
        self.assertEqual(panel['weeks'][0]['working_hours'], 40)
        capacity.change_calendar(self.conn, 'block', {'week': '2026-11-27', 'hours': 5, 'reason': 'Leave'})
        led = capacity.ledger(self.conn)
        self.assertEqual(led['2026-11-27']['budget'], 2250)
        engine_data = capacity.adapted_data(capacity.calendar_data(self.conn), led)
        self.assertEqual(capacity.engine_visible_capacity(engine_data, '2026-11-23', '2026-11-27'), 22.5)
        block_id = self.conn.execute('SELECT id FROM calendar_blocks').fetchone()[0]
        capacity.change_calendar(self.conn, 'remove', {'id': block_id})
        self.assertEqual(capacity.ledger(self.conn)['2026-11-27']['budget'], 2700)
        self.assertEqual(capacity.ledger_revision(self.conn), 3)

    def test_bad_changes_do_not_change_revision(self):
        for action, data in [('hours', {'hours': float('nan')}),
                             ('block', {'week': '2099-01-01', 'hours': 8, 'reason': 'Leave'}),
                             ('remove', {'id': 'missing'})]:
            with self.assertRaises(ValueError):
                capacity.change_calendar(self.conn, action, data)
        self.assertEqual(capacity.ledger_revision(self.conn), 0)

    def test_calendar_change_locks_previously_checked_proposal(self):
        deal_id, offer_id, _ = core.claim_or_resume(self.conn, 'manual', 'calendar-stale', INQUIRY['offer']['client'], 'Inquiry', copy.deepcopy(INQUIRY))
        _, version_id = core.evaluate(self.conn, deal_id, offer_id)
        self.assertTrue(core.check_draft(self.conn, version_id)['passed'])
        capacity.change_calendar(self.conn, 'hours', {'hours': 40})
        result = core.approve(self.conn, version_id)
        self.assertFalse(result['ok'])
        self.assertIn('capacity_changed', result['reasons'])

    def test_composed_work_is_bound_to_checked_version_and_preview(self):
        self.conn.execute('INSERT INTO portfolio_assets VALUES (?,?,?,?,?,?,?,?,?)',
                          ('sample-asset', 'p', 'Sample film', '', '', 'image/png', b'png', 1, 'digest'))
        self.conn.commit()
        inquiry = copy.deepcopy(INQUIRY)
        inquiry['offer'].update(deliverables=5, caption_versions=5, deadline='2026-12-04')
        deal_id, offer_id, _ = core.claim_or_resume(self.conn, 'manual', 'preview-test',
                                                     inquiry['offer']['client'], 'Inquiry', inquiry)
        _, original = core.evaluate(self.conn, deal_id, offer_id)
        updated = commerce.save_page(self.conn, original, {'portfolio': [
            {'asset_id': 'sample-asset', 'title': 'Sample film', 'description': ''}]})['version_id']
        version = server.conn_version(self.conn, updated)
        rows = core.options_of(self.conn, version)
        self.assertGreater(len(rows), 1)
        html = server.render_client_html(self.conn, version, preview=True, preview_ids=[rows[0]['id']])
        self.assertIn('Sample film', html)
        self.assertIn('<h5>' + server.option_label(rows[0]['option_key'], db.uj(rows[0]['public_terms_json'])) + '</h5>', html)
        self.assertNotIn('<h5>' + server.option_label(rows[1]['option_key'], db.uj(rows[1]['public_terms_json'])) + '</h5>', html)
        self.assertEqual(version['outbound_status'], 'unchecked')
