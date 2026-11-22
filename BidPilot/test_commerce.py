import unittest
from unittest.mock import patch
import commerce
import db
import portfolio

class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(':memory:')
        self.c.executescript(commerce.SCHEMA)
        if 'page_json' not in [r[1] for r in self.c.execute('PRAGMA table_info(proposal_versions)')]:
            self.c.execute('ALTER TABLE proposal_versions ADD COLUMN page_json TEXT')
        self.c.execute("INSERT INTO deals (id,client,status,sample_data) VALUES ('d','Buyer','confirmed',1)")
        self.c.execute("INSERT INTO offers (id,deal_id,offer_json) VALUES ('o','d',?)", (db.j({'format':'promo'}),))
        self.c.execute("INSERT INTO evaluations (id,offer_id,result_json) VALUES ('e','o',?)", (db.j({'estimate':{},'option_metrics':{}}),))
        self.c.execute("INSERT INTO proposal_versions (id,deal_id,offer_id,version_no,status,draft_body,public_option_ids_json) VALUES ('v','d','o',1,'confirmed','hello','[]')")
        self.c.commit()
        self.guard = patch('commerce.core.release_integrity_errors', return_value=[])
        self.guard.start()
    def tearDown(self):
        self.guard.stop()
        self.c.close()
    def terms(self):
        return {'description':'Additional editing','price_sgd':200,'as_of':'2026-11-23','phases':[{'deadline':'2026-12-04','hours':2},{'deadline':'2026-12-11','hours':3}]}
    def test_page_escape(self):
        self.c.execute('UPDATE proposal_versions SET page_json=? WHERE id="v"', (db.j({'headline':'<script>bad</script>'}),))
        v = commerce._version(self.c,'v')
        self.assertIn('&lt;script&gt;',commerce.proposal_html(self.c,v))
        with self.assertRaises(ValueError): commerce._page({'html':'<script>'})
    @patch('commerce.core.run_outbound_check', return_value={'passed':True})
    def test_order_review_accept_and_reservation(self, check):
        o = commerce.create_order(self.c,'v',None,self.terms())
        with self.assertRaisesRegex(ValueError,'review_mismatch'): commerce.approve_order(self.c,o['order_id'],'wrong')
        commerce.approve_order(self.c,o['order_id'],o['digest'])
        with self.assertRaisesRegex(ValueError,'acceptance_required'): commerce.confirm_order(self.c,o['order_id'])
        with patch('commerce.core.published_by_token',return_value=commerce._version(self.c,'v')): commerce.accept_order(self.c,'token',o['order_id'])
        weeks={'2026-12-04':{'budget':500,'allocated':0,'residual':500},'2026-12-11':{'budget':500,'allocated':0,'residual':500}}
        with patch('commerce.cap.ledger',return_value=weeks),patch('commerce.core.seed_data',return_value={}),patch('commerce.core.seed_hash',return_value='seed'):
            result=commerce.confirm_order(self.c,o['order_id'])
        self.assertTrue(result['ok'])
        self.assertEqual(sum(r[0] for r in self.c.execute('SELECT planning_hundredths FROM allocations')),500)
        with self.assertRaisesRegex(ValueError,'stale_base'): commerce.create_order(self.c,'v',None,self.terms())
    @patch('commerce.core.run_outbound_check', return_value={'passed':True})
    def test_tamper_blocks(self, check):
        o=commerce.create_order(self.c,'v',None,self.terms())
        self.c.execute("UPDATE change_orders SET terms_json='{}' WHERE id=?",(o['order_id'],))
        self.c.commit()
        with self.assertRaises(ValueError): commerce.approve_order(self.c,o['order_id'],o['digest'])
    def test_closeout_history_and_immutable(self):
        offer={'format':'promo','production_mode':'shoot','shoot_days':1,'locations':1,'scripting':'none','editing_level':'standard','graphics':'none','revision_rounds':2,'caption_versions':1}
        self.c.execute('UPDATE offers SET offer_json=? WHERE id="o"',(db.j(offer),))
        self.c.execute('INSERT INTO options (id,public_terms_json) VALUES (?,?)',('opt',db.j({'deliverables':1,'caption_versions':1,'revision_rounds':2,'price_sgd':1000})))
        self.c.execute("INSERT INTO confirmations (id,deal_id,version_id,option_id) VALUES ('c','d','v','opt')")
        self.c.commit()
        actuals={'actual_hours':10,'actual_cost_sgd':200,'end':'2026-12-04','notes':'','reliable':True}
        commerce.closeout(self.c,'v',actuals)
        self.assertEqual(commerce.get_history(self.c)[0]['actual_hours'],10)
        with self.assertRaisesRegex(ValueError,'already_closed_out'): commerce.closeout(self.c,'v',actuals)

    def test_task_actuals_validate_booked_tasks_and_totals(self):
        self.c.execute('INSERT INTO options (id,public_terms_json) VALUES (?,?)',('opt',db.j({'option_id':'original','price_sgd':1000})))
        self.c.execute("INSERT INTO confirmations (id,deal_id,version_id,option_id) VALUES ('c','d','v','opt')")
        self.c.execute('UPDATE evaluations SET result_json=? WHERE id="e"',(db.j({'option_metrics':{'original':{'detailed_tasks':[{'id':'shoot'},{'id':'edit'}]}}}),))
        self.c.commit()
        actuals={'actual_hours':10,'actual_cost_sgd':200,'actual_labor_cost_sgd':450,'end':'2026-12-04','notes':'','reliable':True,
                 'task_actuals':[{'task_id':'shoot','hours':4,'labor_cost_cents':18000,'note':''},{'task_id':'edit','hours':6,'labor_cost_cents':27000,'note':'late assets'}]}
        bad=dict(actuals, actual_labor_cost_sgd=400)
        with self.assertRaisesRegex(ValueError,'task_actual_totals_mismatch'): commerce.closeout(self.c,'v',bad)
        bad=dict(actuals,task_actuals=[dict(actuals['task_actuals'][0],task_id='unknown')])
        with self.assertRaisesRegex(ValueError,'invalid_task_actuals'): commerce.closeout(self.c,'v',bad)
        commerce.closeout(self.c,'v',actuals)
        self.assertEqual(self.c.execute('SELECT count(*) FROM task_actuals').fetchone()[0],2)

    def test_addition_freezes_rates_for_review(self):
        import core
        terms={'deliverables':3,'locations':1,'shoot_days':1,'caption_versions':1,'revision_rounds':2,'price_sgd':2700}
        self.c.execute("INSERT INTO options (id,public_terms_json) VALUES (?,?)",('opt',db.j(terms)))
        self.c.execute("INSERT INTO confirmations (id,deal_id,version_id,option_id) VALUES ('c','d','v','opt')")
        self.c.execute('UPDATE evaluations SET result_json=? WHERE id="e"',(db.j({'estimate':{'buffer_pct':20}}),))
        self.c.commit()
        created=commerce.create_priced_order(self.c,'v',None,'extra_location',1,'2026-11-23','2026-12-04')
        changed=core.seed_data()
        next(r for r in changed['rate_card'] if r['item']=='extra_location')['price_sgd']='9999'
        with patch('commerce.core.seed_data',return_value=changed), patch('commerce.core.run_outbound_check',return_value={'passed':True}):
            commerce.approve_order(self.c,created['order_id'],created['digest'])
        row=self.c.execute('SELECT terms_json FROM change_orders WHERE id=?',(created['order_id'],)).fetchone()
        import json
        self.assertEqual(json.loads(row['terms_json'])['price_sgd'],300)

    def test_creative_request_targets_selected_offered_package(self):
        self.c.execute('UPDATE proposal_versions SET status="published",public_option_ids_json=? WHERE id="v"',(db.j(['opt']),))
        self.c.execute('INSERT INTO options (id,public_terms_json) VALUES (?,?)',('opt',db.j({'deliverables':2})))
        self.c.commit()
        payload={'request_id':'r','option_id':'opt','target_deliverable':2,'asset_id':'a','action':'swap'}
        with patch('commerce.core.published_by_token',return_value=commerce._version(self.c,'v')), patch('commerce.portfolio.public_asset',return_value={'title':'Sample'}):
            commerce.creative_change(self.c,'token',payload)
            commerce.creative_change(self.c,'token',payload)
            with self.assertRaisesRegex(ValueError,'invalid_reference_target'):
                commerce.creative_change(self.c,'token',dict(payload,target_deliverable=3))
        self.assertEqual(self.c.execute('SELECT count(*) FROM commerce_reference_requests').fetchone()[0],1)
        self.assertEqual(self.c.execute('SELECT count(*) FROM messages').fetchone()[0],1)

    def test_portfolio_search_queue_replay_and_revocation(self):
        self.c.execute('INSERT INTO portfolio_assets VALUES (?,?,?,?,?,?,?,?,?)',
                       ('a','p','Warm café','Coffee in soft light','coffee warm','video/mp4',b'video',1,'digest'))
        self.c.commit()
        with patch('core.published_by_token',return_value=commerce._version(self.c,'v')):
            result = portfolio.client_search(self.c,'token',{'request_id':'q','query':'warm coffee'})
            self.assertEqual(result['status'],'pending')
            self.assertEqual(result['assets'][0]['id'],'a')
            portfolio.client_search(self.c,'token',{'request_id':'q','query':'warm coffee'})
            with self.assertRaisesRegex(ValueError,'request_id_conflict'):
                portfolio.client_search(self.c,'token',{'request_id':'q','query':'different'})
            self.assertEqual(len(portfolio.pending(self.c)),1)
            args={'request_id':'q','version_id':'v','results':[{'asset_id':'a','why':'Warm lighting suits your idea.'}]}
            portfolio.commit_search(self.c,args)
            portfolio.commit_search(self.c,args)
            self.assertEqual(portfolio.search_status(self.c,'token','q')['assets'][0]['why'],args['results'][0]['why'])
            self.c.execute('UPDATE portfolio_assets SET shared=0 WHERE id="a"')
            self.assertEqual(portfolio.search_status(self.c,'token','q')['assets'],[])
            self.c.execute('UPDATE proposal_versions SET status="superseded" WHERE id="v"')
            with self.assertRaisesRegex(ValueError,'stale_version'):
                portfolio.commit_search(self.c,args)

    def test_portfolio_search_scans_entire_library(self):
        for i in range(15):
            self.c.execute('INSERT INTO portfolio_assets VALUES (?,?,?,?,?,?,?,?,?)',
                           (str(i),'p','Example '+str(i),'Warm light','warm','video/mp4',b'x',1,str(i)))
        self.assertEqual(len(portfolio.search(self.c,'warm')),15)

    def test_priced_request_replay(self):
        terms={'deliverables':3,'locations':1,'shoot_days':1,'caption_versions':1,'revision_rounds':2,'price_sgd':2700}
        self.c.execute("INSERT INTO options (id,public_terms_json) VALUES (?,?)",('opt',db.j(terms)))
        self.c.execute("INSERT INTO confirmations (id,deal_id,version_id,option_id) VALUES ('c','d','v','opt')")
        self.c.execute('UPDATE evaluations SET result_json=? WHERE id="e"',(db.j({'estimate':{'buffer_pct':20}}),))
        self.c.execute("INSERT INTO messages (id,deal_id,version_id,direction,body,request_id) VALUES ('m','d','v','inbound','Add a location','additional:r')")
        self.c.commit()
        args=('v',None,'extra_location',1,'2026-11-23','2026-12-04','r')
        first=commerce.create_priced_order(self.c,*args)
        self.assertEqual(first,commerce.create_priced_order(self.c,*args))
        with self.assertRaisesRegex(ValueError,'request_id_conflict'):
            commerce.create_priced_order(self.c,'v',None,'extra_location',2,'2026-11-23','2026-12-04','r')
        self.assertEqual(self.c.execute('SELECT count(*) FROM change_orders').fetchone()[0],1)

    @patch('commerce.core.run_outbound_check', return_value={'passed':True})
    def test_detailed_amendment_reserves_resources(self, check):
        import estimating
        self.c.executescript(estimating.SCHEMA)
        self.c.execute("INSERT INTO options (id,public_terms_json) VALUES (?,?)",('opt',db.j({'deliverables':2})))
        self.c.execute("INSERT INTO confirmations (id,deal_id,version_id,option_id) VALUES ('c','d','v','opt')")
        estimating.save_rate_version(self.c,'r1',[{'role':'editor','cost_rate_cents':4000,'sell_rate_cents':9000}])
        estimating.add_availability(self.c,'editor','2026-11-30T09:00','2026-11-30T17:00')
        self.c.commit()
        spec={'rate_version':'r1','items':[{'id':'edit','role':'editor','basis':'studio','quantity':1,'unit_hours':2,'cost_rate_cents':4000,'sell_rate_cents':9000}]}
        tasks=[{'id':'edit','minutes':120,'resources':['editor'],'release_at':'2026-11-30T09:00','deadline':'2026-11-30T17:00'}]
        created=commerce.create_detailed_order(self.c,'v',None,spec,tasks,'2026-11-23','2026-12-04','Additional editing')
        commerce.approve_order(self.c,created['order_id'],created['digest'])
        with patch('commerce.core.published_by_token',return_value=commerce._version(self.c,'v')):
            commerce.accept_order(self.c,'token',created['order_id'])
        weeks={'2026-12-04':{'budget':500,'allocated':0,'residual':500}}
        with patch('commerce.cap.ledger',return_value=weeks),patch('commerce.core.seed_data',return_value={}),patch('commerce.core.seed_hash',return_value='seed'):
            first=commerce.confirm_order(self.c,created['order_id'])
            second=commerce.confirm_order(self.c,created['order_id'])
        self.assertEqual(first['reservation_id'],second['reservation_id'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM resource_allocations').fetchone()[0],1)

    def test_detailed_amendment_fails_closed(self):
        self.c.execute("INSERT INTO options (id,public_terms_json) VALUES (?,?)", ('opt',db.j({'deliverables':2,'locations':1,'shoot_days':1,'caption_versions':0,'revision_rounds':2})))
        self.c.execute("INSERT INTO confirmations (id,deal_id,version_id,option_id) VALUES ('c','d','v','opt')")
        self.c.execute('UPDATE evaluations SET result_json=? WHERE id="e"', (db.j({'estimate':{'version':'detailed-v1'}}),))
        self.c.commit()
        with self.assertRaisesRegex(ValueError, 'detailed_change_requires_explicit_estimate_and_schedule'):
            commerce.create_priced_order(self.c,'v',None,'caption_version',1,'2026-11-23','2026-12-04')
        self.assertFalse(self.c.in_transaction)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM change_orders').fetchone()[0], 0)

    def test_closeout_includes_calculated_changes(self):
        offer={'format':'promo','production_mode':'shoot','shoot_days':1,'locations':1,'scripting':'none','editing_level':'standard','graphics':'none','revision_rounds':2,'caption_versions':0}
        self.c.execute('UPDATE offers SET offer_json=? WHERE id="o"',(db.j(offer),))
        self.c.execute('INSERT INTO options (id,public_terms_json) VALUES (?,?)',('opt',db.j({'deliverables':2,'caption_versions':0,'revision_rounds':2,'price_sgd':1000})))
        self.c.execute("INSERT INTO confirmations (id,deal_id,version_id,option_id) VALUES ('c','d','v','opt')")
        order={'price_sgd':100,'calculation':{'kind':'caption_version','quantity':2}}
        self.c.execute("INSERT INTO change_orders (id,deal_id,base_version_id,terms_json,digest,status,created_at) VALUES ('co','d','v',?,'x','confirmed',1)",(db.j(order),))
        self.c.commit()
        commerce.closeout(self.c,'v',{'actual_hours':10,'actual_cost_sgd':0,'end':'2026-12-04','notes':'','reliable':True})
        history=commerce.get_history(self.c)[0]
        self.assertEqual((history['deliverables'],history['caption_versions'],history['price_sgd']),(2,2,1100))

    def test_extra_location_deterministic_pricing(self):
        import core
        terms={'deliverables':3,'locations':1,'shoot_days':1,'caption_versions':1,'revision_rounds':2,'price_sgd':2700}
        self.c.execute("INSERT INTO options (id,public_terms_json) VALUES (?,?)",('opt',db.j(terms)))
        self.c.execute("INSERT INTO confirmations (id,deal_id,version_id,option_id) VALUES ('c','d','v','opt')")
        self.c.execute('UPDATE evaluations SET result_json=? WHERE id="e"',(db.j({'estimate':{'buffer_pct':20}}),))
        self.c.commit()
        result=commerce.priced_terms(self.c,'v',None,'extra_location',1,'2026-11-23','2026-12-04')
        self.assertEqual(result['price_sgd'],300)
        self.assertEqual(result['phases'][0]['hours'],3.6)
        with self.assertRaisesRegex(ValueError,'captions_exceed_deliverables'):
            commerce.priced_terms(self.c,'v',None,'caption_version',3,'2026-11-23','2026-12-04')
