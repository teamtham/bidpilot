import copy
import sqlite3
import unittest

import core
import db
import estimating


def base_spec():
    return {'rate_version': 'r1', 'buffer_pct': 20, 'floor_rate_cents': 7000,
            'items': [
                {'id': 'brief', 'role': 'producer', 'basis': 'studio', 'quantity': 1, 'unit_hours': 3,
                 'cost_rate_cents': 4500, 'sell_rate_cents': 9000},
                {'id': 'shoot', 'role': 'producer', 'basis': 'studio', 'quantity': 1, 'unit_hours': 5,
                 'cost_rate_cents': 4500, 'sell_rate_cents': 9000},
                {'id': 'select', 'role': 'editor', 'basis': 'studio', 'quantity': 1, 'unit_hours': 2,
                 'cost_rate_cents': 4500, 'sell_rate_cents': 9000},
                {'id': 'masters', 'role': 'editor', 'basis': 'studio', 'quantity': 3, 'unit_hours': 4,
                 'cost_rate_cents': 4500, 'sell_rate_cents': 9000},
                {'id': 'review', 'role': 'editor', 'basis': 'studio', 'quantity': 1, 'unit_hours': 3,
                 'cost_rate_cents': 4500, 'sell_rate_cents': 9000}],
            'expenses': [{'cost_cents': 12000, 'sell_cents': 12000, 'description': 'rental'}],
            'tasks': [
                {'id': 'prep', 'minutes': 180, 'resources': ['producer'],
                 'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'},
                {'id': 'filming', 'kind': 'shoot', 'minutes': 300, 'resources': ['producer', 'location'],
                 'depends_on': ['prep'], 'release_at': '2026-11-23T09:00', 'deadline': '2026-11-27T17:00'},
                {'id': 'edit', 'minutes': 1500, 'resources': ['producer'], 'depends_on': ['filming'],
                 'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'}]}


def base_offer():
    return {'client': 'Synthetic Cafe', 'format': 'product_promo', 'production_mode': 'one_shoot_day',
            'shoot_days': 1, 'locations': 1, 'scripting': 'light', 'deliverables': 3,
            'editing_level': 'standard', 'graphics': 'basic_titles', 'revision_rounds': 2,
            'caption_versions': 0, 'interviewees': 0, 'deadline': '2026-12-11',
            'budget_sgd': 20000, 'direct_costs_sgd': 120}


AS_OF = '2026-11-23'


class AlternativeGenerationTests(unittest.TestCase):
    def test_quantity_master_to_derivative_lowers_hours_and_keeps_deliverables(self):
        result = estimating.generate_scope_alternatives(base_spec(), base_offer(), AS_OF, [
            {'kind': 'master_to_derivative', 'master_item_id': 'masters', 'derivative_quantity': 1,
             'unit_hours': 1.5, 'basis': 'producer estimate 2026-11-21'}])
        self.assertEqual(result['rejected'], [])
        alt = result['alternatives'][0]
        self.assertEqual(alt['planning_hours_delta'], -3.0)
        items = {i['id']: i for i in alt['variant']['spec']['items']}
        self.assertEqual(items['masters']['quantity'], 2)
        self.assertEqual(items['masters-derivatives']['depends_on'], ['masters'])
        self.assertEqual(alt['variant']['offer'], {})
        self.assertEqual(base_offer()['deliverables'], 3)
        self.assertTrue(any(o['kind'] == 'master_to_derivative' for o in result['opportunities']))

    def test_distinct_derivatives_rewire_item_and_task_links(self):
        spec = base_spec()
        spec['items'] = [i for i in spec['items'] if i['id'] != 'masters'] + [
            {'id': 'edit-' + str(n), 'role': 'editor', 'basis': 'studio', 'quantity': 1, 'unit_hours': 4,
             'cost_rate_cents': 4500, 'sell_rate_cents': 9000} for n in (1, 2, 3)]
        spec['items'].append({'id': 'captions', 'role': 'editor', 'basis': 'studio', 'quantity': 3,
                              'unit_hours': 0.5, 'cost_rate_cents': 4500, 'sell_rate_cents': 9000,
                              'depends_on': ['edit-3']})
        spec['tasks'][0]['item_ids'] = ['brief', 'select']
        spec['tasks'][1]['item_ids'] = ['shoot']
        spec['tasks'][2]['item_ids'] = ['edit-1', 'edit-2', 'edit-3', 'captions', 'review']
        result = estimating.generate_scope_alternatives(spec, base_offer(), AS_OF, [
            {'kind': 'master_to_derivative', 'master_item_id': 'edit-1',
             'derivative_item_ids': ['edit-2', 'edit-3'], 'unit_hours': 1.5,
             'basis': 'producer estimate 2026-11-21'}])
        self.assertEqual(result['rejected'], [])
        variant = result['alternatives'][0]['variant']['spec']
        items = {i['id']: i for i in variant['items']}
        self.assertNotIn('edit-2', items)
        self.assertEqual(items['captions']['depends_on'], ['edit-1-derivatives'])
        self.assertEqual(variant['tasks'][2]['item_ids'], ['edit-1', 'edit-1-derivatives', 'captions', 'review'])
        estimating.detailed_estimate(variant)

    def test_combine_shoots_merges_setups_tasks_and_dependencies(self):
        spec = base_spec()
        spec['setups'] = [{'id': 's1', 'kind': 'shoot', 'location': 'cafe', 'date': '2026-11-25', 'min_call_hours': 4},
                          {'id': 's2', 'kind': 'shoot', 'location': 'cafe', 'date': '2026-11-25', 'min_call_hours': 4}]
        spec['items'][1]['setup_id'] = 's1'
        spec['items'].append({'id': 'shoot-b', 'role': 'producer', 'basis': 'studio', 'quantity': 1,
                              'unit_hours': 2, 'cost_rate_cents': 4500, 'sell_rate_cents': 9000, 'setup_id': 's2'})
        spec['tasks'][1]['setup_id'] = 's1'
        spec['tasks'].insert(2, {'id': 'filming-b', 'kind': 'shoot', 'minutes': 240,
                                 'resources': ['producer', 'location'], 'setup_id': 's2',
                                 'release_at': '2026-11-25T09:00', 'deadline': '2026-11-25T17:00'})
        spec['tasks'][3]['depends_on'] = ['filming', 'filming-b']
        result = estimating.generate_scope_alternatives(spec, base_offer(), AS_OF, [
            {'kind': 'combine_shoots', 'setup_ids': ['s1', 's2'], 'combined_minutes': 420,
             'basis': 'producer estimate 2026-11-21'}])
        self.assertEqual(result['rejected'], [])
        variant = result['alternatives'][0]['variant']['spec']
        self.assertEqual(len(variant['setups']), 1)
        shoots = [t for t in variant['tasks'] if t.get('kind') == 'shoot']
        self.assertEqual(len(shoots), 1)
        self.assertEqual(shoots[0]['minutes'], 420)
        edit = next(t for t in variant['tasks'] if t['id'] == 'edit')
        self.assertEqual(edit['depends_on'], [shoots[0]['id']])
        estimating.detailed_estimate(variant)
        # Different locations cannot merge.
        spec['setups'][1]['location'] = 'rooftop'
        blocked = estimating.generate_scope_alternatives(spec, base_offer(), AS_OF, [
            {'kind': 'combine_shoots', 'setup_ids': ['s1', 's2'], 'combined_minutes': 420,
             'basis': 'producer estimate 2026-11-21'}])
        self.assertEqual(blocked['alternatives'], [])
        self.assertEqual(blocked['rejected'][0]['reason'], 'shoot_merge_requires_same_location_and_date')

    def test_phase_delivery_and_dependency_closure(self):
        spec = base_spec()
        spec['tasks'][0]['deadline'] = '2026-11-27T17:00'
        result = estimating.generate_scope_alternatives(spec, base_offer(), AS_OF, [
            {'kind': 'phase_delivery', 'first_deliverables': 2, 'first_deadline': '2026-11-27',
             'first_task_ids': ['prep', 'filming']}])
        self.assertEqual(result['alternatives'], [])
        self.assertEqual(result['rejected'][0]['reason'], 'phase_deliverable_coverage_required')
        blocked = estimating.generate_scope_alternatives(spec, base_offer(), AS_OF, [
            {'kind': 'phase_delivery', 'first_deliverables': 2, 'first_deadline': '2026-11-27',
             'first_task_ids': ['edit']}])
        self.assertEqual(blocked['rejected'][0]['reason'], 'phase_dependency_not_included')

    def test_supplied_footage_removes_shoot_and_reprices_expenses(self):
        spec = base_spec()
        spec['setups'] = [{'id': 's1', 'kind': 'shoot', 'location': 'cafe', 'date': '2026-11-25'}]
        spec['items'][1]['setup_id'] = 's1'
        spec['tasks'][1]['setup_id'] = 's1'
        result = estimating.generate_scope_alternatives(spec, base_offer(), AS_OF, [
            {'kind': 'use_supplied_footage', 'item_ids': ['shoot'], 'unit_hours': 1,
             'basis': 'buyer confirmed footage 2026-11-21', 'remove_expenses': ['rental']}])
        self.assertEqual(result['rejected'], [])
        alt = result['alternatives'][0]
        variant = alt['variant']['spec']
        shoot_item = next(i for i in variant['items'] if i['id'] == 'shoot')
        self.assertTrue(shoot_item['supplied'])
        self.assertNotIn('setup_id', shoot_item)
        self.assertEqual(variant['setups'], [])
        self.assertFalse(any(t.get('kind') == 'shoot' for t in variant['tasks']))
        self.assertEqual(alt['variant']['offer']['direct_costs_sgd'], 0)
        self.assertEqual(variant['expenses'], [])

    def test_missing_effort_or_basis_is_rejected_not_invented(self):
        for bad in ({'kind': 'master_to_derivative', 'master_item_id': 'masters', 'derivative_quantity': 1,
                     'basis': 'producer estimate 2026-11-21'},
                    {'kind': 'master_to_derivative', 'master_item_id': 'masters', 'derivative_quantity': 1,
                     'unit_hours': 1.5}):
            result = estimating.generate_scope_alternatives(base_spec(), base_offer(), AS_OF, [bad])
            self.assertEqual(result['alternatives'], [])
            self.assertEqual(len(result['rejected']), 1)

    def test_generated_variants_price_through_the_engine(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA + estimating.SCHEMA)
        estimating.save_rate_version(conn, 'r1', [
            {'role': 'producer', 'cost_rate_cents': 4500, 'sell_rate_cents': 9000},
            {'role': 'editor', 'cost_rate_cents': 4500, 'sell_rate_cents': 9000}])
        estimating.add_availability(conn, 'producer', '2026-11-23T09:00', '2026-12-11T17:00')
        estimating.add_availability(conn, 'location', '2026-11-23T09:00', '2026-12-11T17:00')
        spec = base_spec()
        spec['tasks'][0]['deadline'] = '2026-11-27T17:00'
        generated = estimating.generate_scope_alternatives(spec, base_offer(), AS_OF, [
            {'kind': 'master_to_derivative', 'master_item_id': 'masters', 'derivative_quantity': 1,
             'unit_hours': 1.5, 'basis': 'producer estimate 2026-11-21'},
            {'kind': 'phase_delivery', 'first_deliverables': 2, 'first_deadline': '2026-11-27',
             'first_task_ids': ['prep', 'filming']}])
        self.assertEqual(generated['rejected'][0]['reason'], 'phase_deliverable_coverage_required')
        request = {'as_of': AS_OF, 'offer': base_offer(), 'comparable_ids': [],
                   'detailed_spec': spec,
                   'detailed_alternatives': [a['variant'] for a in generated['alternatives']]}
        result = estimating.evaluate(conn, request, core.seed_data())
        self.assertIn(result['verdict'], ('accept', 'accept_with_approval'))
        kinds = {o['kind'] for o in result['options']}
        self.assertIn('alternative_1', kinds)
        self.assertNotIn('alternative_2', kinds)
        conn.close()


class PhaseAllocationPricingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA + estimating.SCHEMA)
        estimating.save_rate_version(self.conn, 'r1', [
            {'role': 'editor', 'cost_rate_cents': 4500, 'sell_rate_cents': 9000}])
        estimating.add_availability(self.conn, 'editor', '2026-11-23T09:00', '2026-12-04T17:00')

    def request(self, a_hours=1, b_hours=1, early_minutes=60, late_minutes=60,
                dedicated=False, buffer_pct=0):
        spec = {'rate_version': 'r1', 'buffer_pct': buffer_pct, 'items': [
            {'id': ident, 'role': 'editor', 'basis': 'synthetic producer estimate',
             'quantity': 1, 'unit_hours': hours, 'cost_rate_cents': 4500,
             'sell_rate_cents': 9000, 'deliverables': [video]}
            for ident, video, hours in [('a', 'video-1', a_hours), ('b', 'video-2', b_hours)]],
            'tasks': [
                {'id': 'early', 'item_ids': ['a'] if dedicated else ['a', 'b'],
                 'minutes': early_minutes, 'resources': ['editor'],
                 'release_at': '2026-11-23T09:00', 'deadline': '2026-11-27T17:00'},
                {'id': 'late', 'item_ids': ['b'] if dedicated else ['a', 'b'],
                 'minutes': late_minutes, 'resources': ['editor'],
                 'release_at': '2026-11-30T09:00', 'deadline': '2026-12-04T17:00'}]}
        offer = dict(base_offer(), deliverables=2, deadline='2026-12-04', direct_costs_sgd=0)
        return {'as_of': AS_OF, 'offer': offer, 'comparable_ids': [], 'detailed_spec': spec,
                'detailed_alternatives': [{'offer': {}, 'spec': copy.deepcopy(spec), 'phases': [
                    {'deliverables': 1, 'deliverable_ids': ['video-1'],
                     'deadline': '2026-11-27', 'task_ids': ['early']},
                    {'deliverables': 1, 'deadline': '2026-12-04'}]}]}

    def price(self, request):
        return estimating.evaluate(self.conn, request, core.seed_data())

    def test_equal_split_rejects_friday_promise(self):
        result = self.price(self.request())
        self.assertEqual(result['verdict'], 'clarify')
        self.assertEqual(result['invalid_fields'], ['detailed_alternatives.0'])
        self.assertEqual(result['options'], [])

    def test_dedicated_early_block_permits_friday_promise(self):
        result = self.price(self.request(dedicated=True))
        self.assertEqual(result['verdict'], 'accept')
        self.assertEqual(result['options'][-1]['phases'][0],
                         {'deliverables': 1, 'deadline': '2026-11-27'})

    def test_original_unphased_split_schedule_remains_feasible(self):
        request = self.request()
        request['detailed_alternatives'] = []
        result = self.price(request)
        self.assertEqual(result['verdict'], 'accept')
        self.assertEqual(result['estimate']['planning_hours'], 2)

    def test_unequal_split_preserves_original_proportions(self):
        # A:B = 1:2. A receives 30, not 90 minutes from the early block.
        self.assertEqual(self.price(self.request(b_hours=2, early_minutes=90,
                                                late_minutes=90))['verdict'], 'clarify')
        # An early 180-minute shared block really covers all 60 minutes for A.
        result = self.price(self.request(b_hours=2, early_minutes=180, late_minutes=60))
        self.assertEqual(result['verdict'], 'accept')
        self.assertIn('phases', result['options'][-1])

    def test_phase_uses_full_schedule_buffer_and_minute_ceiling(self):
        for minutes, verdict in [(63, 'clarify'), (64, 'accept')]:
            with self.subTest(minutes=minutes):
                result = self.price(self.request(a_hours=1.01, early_minutes=minutes,
                    late_minutes=63, dedicated=True, buffer_pct=5))
                self.assertEqual(result['verdict'], verdict)


if __name__ == '__main__':
    unittest.main()