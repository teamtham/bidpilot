import copy
import sqlite3
import unittest

import core
import db
import estimating


class EstimatingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA + estimating.SCHEMA)
        self.conn.execute("INSERT INTO deals (id, sample_data) VALUES ('d',1)")
        estimating.save_rate_version(self.conn, 'r1', [{'role':'producer', 'cost_rate_cents':4500, 'sell_rate_cents':9000}])
        self.spec = {'rate_version': 'r1', 'buffer_pct': 20, 'floor_rate_cents': 7000,
                     'items': [{'id': str(i), 'role': 'producer', 'basis': 'studio', 'quantity': q,
                                'unit_hours': h, 'cost_rate_cents': 4500, 'sell_rate_cents': 9000}
                               for i, (q, h) in enumerate([(1,3),(1,5),(1,2),(3,4),(1,1),(3,.5),(1,3)])],
                     'expenses': [{'cost_cents':12000, 'sell_cents':12000}]}

    def tearDown(self):
        self.conn.close()

    def test_worked_fixture_and_derivative(self):
        result = estimating.detailed_estimate(self.spec)
        self.assertEqual((result['base_hours'], result['planning_hours'], result['production_cost_cents'], result['floor_cents']),
                         (27.5, 33, 160500, 243000))
        changed = copy.deepcopy(self.spec)
        changed['items'].append({'id':'cutdown', 'role':'producer', 'basis':'override', 'quantity':1,
                                 'unit_hours':2, 'cost_rate_cents':4500, 'sell_rate_cents':9000})
        self.assertEqual(estimating.detailed_estimate(changed)['base_hours'], 29.5)

    def test_resource_conflict_and_reservation(self):
        estimating.add_availability(self.conn, 'producer', '2026-11-23T09:00', '2026-11-23T17:00')
        tasks = [{'id':'shoot', 'kind':'shoot', 'minutes':300, 'resources':['producer'],
                  'release_at':'2026-11-23T09:00', 'deadline':'2026-11-23T17:00'}]
        self.assertTrue(estimating.schedule(self.conn, tasks)['feasible'])
        estimating.reserve_schedule(self.conn, 'r1', tasks)
        self.assertFalse(estimating.schedule(self.conn, tasks)['feasible'])
        with self.assertRaises(ValueError):
            estimating.reserve_schedule(self.conn, 'r2', tasks)

    def test_hypothetical_resource_comparison_is_read_only(self):
        estimating.add_availability(self.conn, 'producer', '2026-11-23T09:00', '2026-11-23T17:00')
        shoot = [{'id':'shoot', 'kind':'shoot', 'minutes':300, 'resources':['producer'],
                  'release_at':'2026-11-23T09:00', 'deadline':'2026-11-23T17:00'}]
        candidate = estimating.schedule(self.conn, shoot)
        self.assertTrue(candidate['feasible'])
        overlapping = estimating.schedule(self.conn, shoot, candidate['allocations'])
        self.assertFalse(overlapping['feasible'])
        self.assertEqual(overlapping['blocked_task_id'], 'shoot')
        self.assertTrue(estimating.schedule(self.conn, shoot)['feasible'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM resource_allocations').fetchone()[0], 0)

    def test_history_requires_two_and_excludes_future(self):
        offer = {'format':'product_promo','production_mode':'one_shoot_day','shoot_days':1,
                 'locations':1,'scripting':'light','editing_level':'standard','graphics':'basic_titles',
                 'revision_rounds':2,'caption_versions':0}
        rows = [dict(offer, project_id='a', deliverables=2, actual_hours=20, price_sgd=2000, end='2025-01-01'),
                dict(offer, project_id='b', deliverables=4, actual_hours=40, price_sgd=4000, end='2025-02-01')]
        self.assertIsNone(estimating.history_benchmark(offer, rows[:1]))
        self.assertEqual(estimating.history_benchmark(offer, rows)['median_price_per_deliverable_sgd'], 1000)
        self.assertIsNone(estimating.history_benchmark(offer, rows, '2025-01-15'))

    def test_fee_only_history_keeps_brief_and_excludes_hourly_metric(self):
        project={'client':'Cafe','format':'product_promo','production_mode':'one_shoot_day','shoot_days':1,
                 'locations':1,'scripting':'light','deliverables':3,'editing_level':'standard','graphics':'basic_titles',
                 'revision_rounds':2,'caption_versions':0,'price_sgd':3000,'end':'2025-01-01','project_brief':'Coffee launch'}
        a=estimating.add_past_project(self.conn,project)
        b=estimating.add_past_project(self.conn,project)
        import json
        rows=[json.loads(r[0]) for r in self.conn.execute('SELECT project_json FROM founder_past_projects')]
        result=estimating.history_benchmark(project,rows,'2026-01-01')
        self.assertEqual(result['sample_count'],2)
        self.assertIsNone(result['median_price_per_actual_hour_sgd'])
        self.assertIsNone(rows[0]['actual_hours'])
        self.assertEqual(rows[0]['project_brief'],'Coffee launch')

    def test_captioned_quote_uses_unbundled_seed_fees_and_founder_actuals(self):
        import json
        from pathlib import Path
        from unittest.mock import patch
        fixture = json.loads((Path(__file__).parent / 'bidpilot-engine/test_cases.json').read_text())[0]
        request = copy.deepcopy(fixture['input'])
        request['pricing_mode'] = 'historical_replay'
        seed = core.seed_data()['past_projects']
        columns = ['project_id','client','format','production_mode','shoot_days','locations','scripting',
                   'deliverables','editing_level','graphics','revision_rounds','caption_versions',
                   'est_hours','actual_hours','price_sgd','start','end']
        for row in seed:
            self.conn.execute('INSERT INTO seed_past_projects VALUES (' + ','.join('?' for _ in columns) + ')',
                              [row[k] for k in columns])
        added = {'client':'Fresh Cafe', 'format':'product_promo', 'production_mode':'one_shoot_day',
                 'shoot_days':1, 'locations':1, 'scripting':'light', 'deliverables':3,
                 'editing_level':'standard', 'graphics':'basic_titles', 'revision_rounds':2,
                 'caption_versions':3, 'actual_hours':30, 'price_sgd':3000, 'end':'2026-01-01'}
        founder = estimating.add_past_project(self.conn, added)
        with patch('commerce.get_history', return_value=[]):
            result = estimating.evaluate(self.conn, request, core.seed_data())
        self.assertEqual(result['historical_pricing']['status'], 'applied')
        evidence = result['historical_pricing']
        self.assertEqual(evidence['sample_count'], 5)
        self.assertEqual(len(evidence['leave_one_out']), 5)
        self.assertIn(founder['project_id'], [x['excluded_project_id'] for x in evidence['leave_one_out']])
        # Added fee: (3000 - 750 setup - 150 for its own captions) / 3 = 700.
        # Baseline uncaptioned projects use zero caption deductions, not 150.
        self.assertEqual(evidence['unit_price_sgd'], 600)

    def test_unregistered_or_modified_rates_rejected(self):
        altered = copy.deepcopy(self.spec)
        altered['items'][0]['sell_rate_cents'] = 9999
        with self.assertRaisesRegex(ValueError, 'catalog_rate_mismatch'):
            estimating.save_estimate(self.conn, 'd', 'r', altered)
        altered['rate_version'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'unknown_rate_version'):
            estimating.save_estimate(self.conn, 'd', 'r', altered)

    def test_backtest(self):
        result = estimating.backtest([{'project_id':'a','predicted_hours':8,'actual_hours':10},
                                      {'project_id':'b','predicted_hours':12,'actual_hours':10}])['overall']
        self.assertEqual(result['median_absolute_hours_error'], 2)
        self.assertEqual(result['underestimated_share'], .5)

    def test_held_out_model_comparison_and_campaign_split(self):
        from scripts.backtest_estimates import run
        rows = [{'project_id':'train', 'campaign_id':'c1', 'quoted_at':'2025-01-01'},
                {'project_id':'test', 'campaign_id':'c2', 'quoted_at':'2026-01-01',
                 'actual_hours':10, 'group':'edit-only',
                 'predictions':{'ratecard':8, 'history':9, 'detailed':11}}]
        result = run(rows, '2025-12-31')
        self.assertEqual(result['held_out_projects'], 1)
        self.assertEqual(result['models']['ratecard']['overall']['median_absolute_hours_error'], 2)
        self.assertEqual(result['models']['history']['groups']['edit-only']['underestimated_share'], 1)
        self.assertEqual(result['models']['detailed']['overall']['underestimated_share'], 0)
        rows[1]['campaign_id'] = 'c1'
        with self.assertRaisesRegex(ValueError, 'campaign_crosses_cutoff'):
            run(rows, '2025-12-31')

    def test_legacy_list_below_floor_is_never_accepted(self):
        import json
        from pathlib import Path
        fixture = json.loads((Path(__file__).parent / 'bidpilot-engine/test_cases.json').read_text())[0]
        request = copy.deepcopy(fixture['input'])
        request['pricing_mode'] = 'historical_replay'
        request['offer']['budget_sgd'] = 10000
        data = core.seed_data()
        for row in data['rate_card']:
            row['price_sgd'] = 0
        result = estimating.evaluate(self.conn, request, data)
        self.assertNotIn(result['verdict'], ('accept', 'accept_with_approval'))
        self.assertTrue(all(option['price_sgd'] >= result['option_metrics'][option['option_id']]['floor_sgd'] for option in result['options']))

    def test_detailed_quote_and_recheck(self):
        import json
        from pathlib import Path
        fixture = json.loads((Path(__file__).parent / 'bidpilot-engine/test_cases.json').read_text())[0]
        request = copy.deepcopy(fixture['input'])
        request['pricing_mode'] = 'historical_replay'
        request['offer']['direct_costs_sgd'] = 120
        request['offer']['budget_sgd'] = 4000
        spec = copy.deepcopy(self.spec)
        spec['tasks'] = [{'id':'work', 'minutes':1980, 'resources':['producer'],
                          'release_at':'2026-11-23T09:00', 'deadline':'2026-12-11T17:00'}]
        request['detailed_spec'] = spec
        estimating.add_availability(self.conn, 'producer', '2026-11-23T09:00', '2026-12-11T17:00')
        result = estimating.evaluate(self.conn, request, core.seed_data())
        self.assertEqual(result['verdict'], 'accept')
        terms = result['options'][0]
        self.assertTrue(estimating.resource_check(self.conn, result, terms)['feasible'])
        estimating.reserve_for_option(self.conn, 'one', result, terms)
        self.assertTrue(estimating.resource_check(self.conn, result, terms)['feasible'])
        # Occupy the remainder so a second 33-hour allocation cannot fit.
        self.conn.execute("DELETE FROM resource_availability")
        estimating.add_availability(self.conn, 'producer', '2026-11-23T09:00', '2026-11-24T18:00')
        self.assertFalse(estimating.resource_check(self.conn, result, terms)['feasible'])

    def detailed_request(self, spec):
        import json
        from pathlib import Path
        fixture = json.loads((Path(__file__).parent / 'bidpilot-engine/test_cases.json').read_text())[0]
        request = copy.deepcopy(fixture['input'])
        request['pricing_mode'] = 'historical_replay'
        request['offer']['direct_costs_sgd'] = 120
        request['offer']['budget_sgd'] = 4000
        request['detailed_spec'] = spec
        return request

    def linked_tasks(self, spec):
        import math
        tasks = []
        for item in spec['items']:
            minutes = math.ceil(item['quantity'] * item['unit_hours'] * (1 + spec.get('buffer_pct', 0) / 100) * 60)
            tasks.append({'id': 'task-' + item['id'], 'item_ids': [item['id']], 'minutes': minutes,
                          'resources': ['producer'], 'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'})
        return tasks

    def test_per_task_coverage_detects_underallocated_line(self):
        spec = copy.deepcopy(self.spec)
        spec['tasks'] = self.linked_tasks(spec)
        # Shrink one line's task but pad the total so the aggregate check passes.
        spec['tasks'][3]['minutes'] = 60
        spec['tasks'].append({'id': 'slack', 'minutes': 900, 'resources': ['producer'],
                              'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'})
        estimating.add_availability(self.conn, 'producer', '2026-11-23T09:00', '2026-12-11T17:00')
        result = estimating.evaluate(self.conn, self.detailed_request(spec), core.seed_data())
        self.assertEqual(result['verdict'], 'clarify')
        self.assertIn('detailed_spec:task_underallocated:3', result['invalid_fields'])
        spec['tasks'][3]['minutes'] = 864
        result = estimating.evaluate(self.conn, self.detailed_request(spec), core.seed_data())
        self.assertEqual(result['verdict'], 'accept')

    def test_shared_shoot_setup_and_supplied_footage(self):
        spec = copy.deepcopy(self.spec)
        spec['setups'] = [{'id': 'shoot-1', 'kind': 'shoot', 'location': 'cafe', 'date': '2026-11-24', 'min_call_hours': 4},
                          {'id': 'post-1', 'kind': 'post'}]
        spec['items'][1]['setup_id'] = 'shoot-1'          # one shared shoot for all three reels
        spec['items'][3]['setup_id'] = 'post-1'           # three masters
        spec['items'].append({'id': 'ingest', 'role': 'producer', 'basis': 'producer override 2026-11-21',
                              'quantity': 1, 'unit_hours': 1, 'cost_rate_cents': 4500, 'sell_rate_cents': 9000,
                              'supplied': True, 'deliverables': ['reel-1', 'reel-2', 'reel-3']})
        spec['tasks'] = self.linked_tasks(spec)
        shoot_task = spec['tasks'][1]
        shoot_task.update({'kind': 'shoot', 'setup_id': 'shoot-1', 'contiguous': True})
        estimating.add_availability(self.conn, 'producer', '2026-11-23T09:00', '2026-12-11T17:00')
        result = estimating.evaluate(self.conn, self.detailed_request(spec), core.seed_data())
        self.assertEqual(result['verdict'], 'accept', result)
        lines = {line['id']: line for line in result['estimate']['lines']}
        self.assertTrue(lines['ingest']['supplied'])
        self.assertEqual(lines['1']['setup_id'], 'shoot-1')
        # A second cost-bearing task on the same shoot setup is double counting.
        broken = copy.deepcopy(spec)
        extra = dict(shoot_task); extra['id'] = 'shoot-again'; extra['item_ids'] = ['1']
        broken['tasks'].append(extra)
        result = estimating.evaluate(self.conn, self.detailed_request(broken), core.seed_data())
        self.assertIn('detailed_spec:shoot_setup_needs_one_task:shoot-1', result['invalid_fields'])
        # Supplied footage can never join a shoot setup.
        bad = copy.deepcopy(spec)
        bad['items'][-1]['setup_id'] = 'shoot-1'
        with self.assertRaisesRegex(ValueError, 'supplied_footage_cannot_use_shoot_setup'):
            estimating.detailed_estimate(bad)
        # Minimum call period is enforced on the shared shoot task.
        short = copy.deepcopy(spec)
        short['tasks'][1]['minutes'] = 120
        short['tasks'].append({'id': 'shoot-extra', 'item_ids': ['1'], 'minutes': 240,
                               'resources': ['producer'],
                               'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'})
        result = estimating.evaluate(self.conn, self.detailed_request(short), core.seed_data())
        self.assertIn('detailed_spec:shoot_setup_below_min_call:shoot-1', result['invalid_fields'])

    def test_item_dependencies_guard_alternatives_and_phases(self):
        estimating.add_availability(self.conn, 'producer', '2026-11-23T09:00', '2026-12-11T17:00')
        spec = copy.deepcopy(self.spec)
        spec['tasks'] = [{'id': 'work', 'minutes': 1980, 'resources': ['producer'],
                          'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'}]
        request = self.detailed_request(spec)
        # Valid alternative: two masters instead of three, independently priced.
        fewer = copy.deepcopy(spec)
        fewer['items'][3]['quantity'] = 2
        fewer['tasks'] = [{'id': 'work', 'minutes': 1692, 'resources': ['producer'],
                           'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'}]
        request['detailed_alternatives'] = [{'offer': {'deliverables': 2, 'caption_versions': 2}, 'spec': fewer}]
        result = estimating.evaluate(self.conn, request, core.seed_data())
        self.assertEqual(result['verdict'], 'accept')
        self.assertIn('alternative_1', result['option_metrics'])
        # A derivative whose master was removed must be rejected, not priced.
        orphan = copy.deepcopy(spec)
        orphan['items'].append({'id': 'cutdown', 'role': 'producer', 'basis': 'override',
                                'quantity': 1, 'unit_hours': 2, 'cost_rate_cents': 4500,
                                'sell_rate_cents': 9000, 'depends_on': ['master-that-was-removed']})
        request['detailed_alternatives'] = [{'offer': {'deliverables': 4}, 'spec': orphan}]
        result = estimating.evaluate(self.conn, request, core.seed_data())
        self.assertEqual(result['verdict'], 'clarify')
        self.assertIn('detailed_alternatives.0', result['invalid_fields'])
        # Phased delivery: first-phase tasks and their dependencies stay together.
        phased = copy.deepcopy(spec)
        phased['tasks'] = [{'id': 'prep', 'minutes': 216, 'resources': ['producer'],
                            'release_at': '2026-11-23T09:00', 'deadline': '2026-12-01T17:00'},
                           {'id': 'edit', 'minutes': 1764, 'resources': ['producer'], 'depends_on': ['prep'],
                            'release_at': '2026-11-23T09:00', 'deadline': '2026-12-11T17:00'}]
        request['detailed_alternatives'] = [{'offer': {}, 'spec': phased,
                                             'phases': [{'deliverables': 1, 'deadline': '2026-12-01', 'task_ids': ['prep']},
                                                        {'deliverables': 2, 'deadline': '2026-12-11'}]}]
        result = estimating.evaluate(self.conn, request, core.seed_data())
        self.assertEqual(result['verdict'], 'clarify', result)
        self.assertIn('detailed_alternatives.0', result['invalid_fields'])
        # A phase dependency left outside the phase is rejected.
        request['detailed_alternatives'][0]['phases'][0]['task_ids'] = ['edit']
        result = estimating.evaluate(self.conn, request, core.seed_data())
        self.assertEqual(result['verdict'], 'clarify')

    def test_producer_overrides_and_setup_controls(self):
        args = {'deal_id': 'd', 'scope_revision_id': 'rev1', 'item_id': '0', 'field': 'unit_hours',
                'value': 4, 'basis_date': '2026-11-21', 'note': 'Measured on the last cafe job',
                'request_id': 'override-1'}
        saved = estimating.save_producer_override(self.conn, args)
        self.assertEqual(estimating.save_producer_override(self.conn, args), saved)
        with self.assertRaisesRegex(ValueError, 'request_id_conflict'):
            estimating.save_producer_override(self.conn, {**args, 'value': 5})
        with self.assertRaisesRegex(ValueError, 'invalid_basis_date'):
            estimating.save_producer_override(self.conn, {**args, 'request_id': 'o2', 'basis_date': 'yesterday'})
        updated, applied = estimating.apply_producer_overrides(self.conn, 'd', 'rev1', self.spec)
        self.assertEqual(updated['items'][0]['unit_hours'], 4)
        self.assertEqual(updated['items'][0]['basis'], 'producer override 2026-11-21')
        self.assertEqual(applied[0]['override_id'], saved['override_id'])
        self.assertEqual(self.spec['items'][0]['unit_hours'], 3)  # original untouched
        out = estimating._estimate_scope(self.conn, {'deal_id': 'd', 'scope_revision_id': 'rev1',
                                                     'spec': self.spec, 'apply_overrides': True})
        self.assertEqual(out['estimate']['lines'][0]['hours'], 4)
        self.assertEqual(out['applied_overrides'][0]['item_id'], '0')
        # Merge/split producer controls on setups.
        spec = copy.deepcopy(self.spec)
        spec['setups'] = [{'id': 'a', 'kind': 'post'}, {'id': 'b', 'kind': 'post'}]
        spec['items'][0]['setup_id'] = 'a'; spec['items'][1]['setup_id'] = 'b'
        merged = estimating.merge_setups(spec, ['a', 'b'], 'shared')
        self.assertEqual({i['setup_id'] for i in merged['items'][:2]}, {'shared'})
        with self.assertRaisesRegex(ValueError, 'cannot_merge_different_setup_kinds'):
            estimating.merge_setups({**spec, 'setups': [{'id': 'a', 'kind': 'post'}, {'id': 'b', 'kind': 'shoot'}]}, ['a', 'b'], 'x')
        shoot_spec = {'setups': [{'id': 's1', 'kind': 'shoot', 'location': 'cafe'},
                                 {'id': 's2', 'kind': 'shoot', 'location': 'rooftop'}], 'items': []}
        with self.assertRaisesRegex(ValueError, 'shoot_merge_requires_same_location_and_date'):
            estimating.merge_setups(shoot_spec, ['s1', 's2'], 's')
        split = estimating.split_setup(merged, 'shared',
                                       [{'setup_id': 'shared', 'item_ids': ['0']},
                                        {'setup_id': 'new', 'item_ids': ['1']}])
        self.assertEqual(split['items'][1]['setup_id'], 'new')
        with self.assertRaisesRegex(ValueError, 'split_must_partition_setup_items'):
            estimating.split_setup(merged, 'shared', [{'setup_id': 'shared', 'item_ids': ['0']},
                                                      {'setup_id': 'new', 'item_ids': ['0']}])

    def test_evidence_links_must_belong_to_the_inquiry(self):
        import conversation
        self.conn.executescript(conversation.SCHEMA)
        self.conn.execute("INSERT INTO inquiries(id,token_hash,created_at) VALUES ('inq','h',1)")
        self.conn.execute("INSERT INTO inquiry_facts VALUES ('fact-1','inq',1,'videos','3','confirmed',1,NULL,1)")
        spec = copy.deepcopy(self.spec)
        spec['items'][0]['evidence'] = [{'kind': 'fact', 'id': 'fact-1'}]
        estimating.validate_evidence_links(self.conn, spec, 'inq')
        spec['items'][0]['evidence'] = [{'kind': 'observation', 'id': 'not-mine'}]
        with self.assertRaisesRegex(ValueError, 'evidence_not_in_inquiry'):
            estimating.validate_evidence_links(self.conn, spec, 'inq')

    def register_template(self):
        estimating.save_promo_template(self.conn, {
            'version': 'acceptance-template', 'basis_date': '2026-11-21',
            'note': 'Synthetic producer approval for executable acceptance only',
            'supported': {'editing_level': 'standard', 'graphics': 'basic_titles'},
            'rate': {'cost_rate_cents': 4500, 'sell_rate_cents': 9000}, 'buffer_pct': 20,
            'hours': {'prep': 1, 'shoot': 5, 'ingest': 1, 'review_selected': 1,
                      'review_per_footage_minute': .05, 'original_edit': 4, 'cutdown': 1,
                      'captions': .5, 'review_round': .5, 'delivery': .25}})

    def test_social_reel_is_priced_from_past_promos_not_sent_to_manual_estimate(self):
        """A café social reel is a promo-family job: the engine has no separate
        reel rate card, so it must price from the studio's past promos instead of
        stopping at MANUAL_ESTIMATE_REQUIRED."""
        request = self.detailed_request(self.spec)
        request.pop('detailed_spec')
        request.pop('pricing_mode', None)
        promo = estimating.evaluate(self.conn, copy.deepcopy(request), core.seed_data())
        request['offer']['format'] = 'social_reel'
        reel = estimating.evaluate(self.conn, copy.deepcopy(request), core.seed_data())
        self.assertNotIn('MANUAL_ESTIMATE_REQUIRED', reel['reason_codes'], reel)
        self.assertTrue(reel['options'])
        self.assertEqual(reel['estimate']['list_sgd'], promo['estimate']['list_sgd'])
        self.assertEqual(request['offer']['format'], 'social_reel')  # caller's request untouched
        # Interviews and custom animation still need the owner's own estimate.
        request['offer']['interviewees'] = 2
        self.assertEqual(estimating.evaluate(self.conn, copy.deepcopy(request), core.seed_data())['reason_codes'],
                         ['MANUAL_ESTIMATE_REQUIRED'])

    def test_live_default_prices_from_past_jobs_with_or_without_a_template(self):
        """The default (QwenWork pipeline) path prices from similar past jobs. No
        template is required, and a registered one does not change the price."""
        request = self.detailed_request(self.spec)
        request.pop('detailed_spec')
        request.pop('pricing_mode', None)
        without = estimating.evaluate(self.conn, copy.deepcopy(request), core.seed_data())
        self.assertNotIn('MANUAL_ESTIMATE_REQUIRED', without['reason_codes'])
        self.assertNotEqual(without['verdict'], 'clarify', without)
        self.assertGreater(without['estimate']['list_sgd'], 0)
        self.assertTrue(without['options'])
        self.register_template()
        withtpl = estimating.evaluate(self.conn, copy.deepcopy(request), core.seed_data())
        self.assertEqual(withtpl['estimate']['list_sgd'], without['estimate']['list_sgd'])
        self.assertNotEqual(withtpl['estimate'].get('version'), 'detailed-v1')

    def test_double_credit_rejected_even_with_unlinked_slack(self):
        spec = copy.deepcopy(self.spec)
        spec['items'] = [dict(spec['items'][0], id='a', quantity=1, unit_hours=1),
                         dict(spec['items'][0], id='b', quantity=1, unit_hours=1)]
        spec['buffer_pct'] = 0
        tasks = [{'id': 'both', 'item_ids': ['a', 'b'], 'minutes': 60},
                 {'id': 'slack', 'minutes': 60}]
        with self.assertRaisesRegex(ValueError, 'task_underallocated'):
            estimating._validate_task_coverage(spec, estimating.detailed_estimate(spec), tasks)
        tasks[0]['minutes'] = 120
        estimating._validate_task_coverage(spec, estimating.detailed_estimate(spec), tasks)

    def test_finished_phase_requires_specific_edit_caption_review_delivery(self):
        self.register_template()
        request = self.detailed_request(self.spec)
        request['production_facts'] = {'production.original_edits': 1, 'production.cutdowns': 2,
                                      'production.clips_preselected': True}
        spec = estimating.promo_spec(self.conn, request)
        phase = {'deliverables': 1, 'task_ids': ['prep', 'shoot', 'ingest', 'footage-review']}
        with self.assertRaisesRegex(ValueError, 'phase_unfinished_deliverables'):
            estimating._validate_finished_phase(spec, phase, 3)
        phase['task_ids'] += ['original-video-1', 'captions-video-1', 'review-video-1', 'delivery-video-1']
        estimating._validate_finished_phase(spec, phase, 3)

    def test_persist(self):
        out = estimating.save_estimate(self.conn, 'd', 'revision1', self.spec)
        self.assertEqual(self.conn.execute('SELECT rate_version FROM detailed_estimates WHERE id=?', (out['estimate_id'],)).fetchone()[0], 'r1')

if __name__ == '__main__':
    unittest.main()
