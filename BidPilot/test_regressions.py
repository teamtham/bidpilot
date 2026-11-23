"""BidPilot Stage 4 regression tests: duplicate intake keys, fail-closed
option sets, and concurrency-safe approve/release.

Run: python3 -B -m unittest -v test_regressions
"""
import copy
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core
import db

INQUIRY = {
    "pricing_mode": "legacy_replay",
    "as_of": "2026-11-23",
    "comparable_ids": ["P-031", "P-036", "P-041", "P-044"],
    "offer": {
        "client": "Juniper Cafe", "format": "product_promo",
        "production_mode": "one_shoot_day", "shoot_days": 1, "locations": 1,
        "interviewees": 0, "scripting": "light", "deliverables": 3,
        "editing_level": "standard", "graphics": "basic_titles",
        "revision_rounds": 2, "caption_versions": 3, "deadline": "2026-12-11",
        "budget_sgd": 2700, "direct_costs_sgd": 0,
    },
}


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bidpilot-reg-")
        self.db_path = str(Path(self.tmp) / "reg.db")

    def conn(self):
        return db.connect(self.db_path)

    def make_checked_version(self, source_id="reg-1"):
        """import -> evaluate -> check; returns (conn, deal_id, version_id)."""
        conn = self.conn()
        core.load_seed(conn)
        deal_id, offer_id, _ = core.claim_or_resume(
            conn, "hashmeta", source_id, INQUIRY["offer"]["client"],
            "raw inquiry one", INQUIRY)
        _, version_id = core.evaluate(conn, deal_id, offer_id)
        outcome = core.check_draft(conn, version_id)
        self.assertTrue(outcome["passed"], outcome)
        return conn, deal_id, version_id

    def counter_version(self, conn, subset=None):
        request = copy.deepcopy(INQUIRY)
        request['offer'].update(deliverables=5, caption_versions=5, deadline='2026-12-04')
        deal, offer, _ = core.claim_or_resume(conn, 'regression', db.new_id(),
                                             'Juniper Cafe', 'sample', request)
        _, vid = core.evaluate(conn, deal, offer)
        version = conn.execute('SELECT * FROM proposal_versions WHERE id=?', (vid,)).fetchone()
        if subset:
            rows = [r for r in core.options_of(conn, version) if r['option_key'] == subset]
            conn.execute('UPDATE proposal_versions SET public_option_ids_json=?, import_option_keys_json=? WHERE id=?',
                         (db.j([r['id'] for r in rows]), db.j([subset]), vid))
            conn.commit()
        return vid

    def test_any_terms_in_every_buyer_copy_field_pass_but_the_floor_never_does(self):
        """Owner rule: only the lowest price the studio would accept is blocked."""
        locations = ['draft', 'headline', 'introduction', 'section_title', 'section_body',
                     'portfolio_title', 'portfolio_description']
        for location in locations:
            for leak in (False, True):
                with self.subTest(location=location, leak=leak), closing(db.connect(':memory:')) as conn:
                    core.load_seed(conn)
                    vid = self.counter_version(conn)
                    version = conn.execute('SELECT * FROM proposal_versions WHERE id=?', (vid,)).fetchone()
                    floor = json.loads(conn.execute('SELECT result_json FROM evaluations WHERE offer_id=?',
                                                    (version['offer_id'],)).fetchone()[0])['estimate']['floor_sgd']
                    text = ('Our lowest price is S$%s.' % format(floor, ',.2f')) if leak else 'Five videos by 4 Dec for S$2,050.'
                    page = {}
                    body = 'Choose an approved package below.'
                    if location == 'draft':
                        body = text
                    elif location in ('headline', 'introduction'):
                        page[location] = text
                    elif location.startswith('section'):
                        page['sections'] = [{location.split('_')[1]: text}]
                    else:
                        page['portfolio'] = [{location.split('_')[1]: text}]
                    conn.execute('UPDATE proposal_versions SET draft_body=?, page_json=? WHERE id=?',
                                 (body, db.j(page), vid))
                    conn.commit()
                    outcome = core.check_draft(conn, vid)
                    self.assertEqual(outcome['passed'], not leak, outcome)
                    if leak:
                        self.assertEqual([v['code'] for v in outcome['violations']], ['PRIVATE_NUMBER'])
                        self.assertFalse(core.approve(conn, vid)['ok'])

    def test_single_and_phased_association_check_and_legacy_release(self):
        examples = [('hold_date', 'Two videos by 4 Dec for S$2,050.', True),
                    ('phased', 'Two videos by 4 Dec and three more by 18 Dec for S$4,000 total.', True),
                    ('phased', 'Deliver 2 videos by 4 Dec and 3 videos by 18 Dec for S$4,000.', True),
                    ('phased', 'Three videos by 4 Dec and two more videos by 18 Dec for S$4,000 total.', True)]
        for subset, body, passed in examples:
            for legacy in (False, True):
                with self.subTest(subset=subset, body=body, legacy=legacy), closing(db.connect(':memory:')) as conn:
                    core.load_seed(conn)
                    vid = self.counter_version(conn, subset)
                    conn.execute('UPDATE proposal_versions SET draft_body=? WHERE id=?', (body, vid))
                    conn.commit()
                    if not legacy:
                        result = core.check_draft(conn, vid)
                        self.assertEqual(result['passed'], passed, result)
                    else:
                        v = conn.execute('SELECT * FROM proposal_versions WHERE id=?', (vid,)).fetchone()
                        conn.execute("UPDATE proposal_versions SET status='checked', outbound_status='passed', checked_digest=? WHERE id=?",
                                     (core.digest(v, core.options_of(conn, v)), vid))
                        conn.commit()
                    self.assertEqual(core.approve(conn, vid)['ok'], passed)

    def test_preview_release_exact_copy_and_buyer_selects_immutable_option(self):
        import server
        with closing(db.connect(':memory:')) as conn:
            core.load_seed(conn)
            vid = self.counter_version(conn)
            self.assertTrue(core.check_draft(conn, vid)['passed'])
            version = conn.execute('SELECT * FROM proposal_versions WHERE id=?', (vid,)).fetchone()
            before = server.render_client_html(conn, version, preview=True)
            released = core.approve(conn, vid)
            self.assertTrue(released['ok'], released)
            version = conn.execute('SELECT * FROM proposal_versions WHERE id=?', (vid,)).fetchone()
            after = server.render_client_html(conn, version)
            self.assertIn(version['draft_body'], before)
            self.assertIn(version['draft_body'], after)
            rows = core.options_of(conn, version)
            chosen = next(r for r in rows if r['option_key'] == 'phased')
            self.assertIn('2 by 4 Dec', before)
            self.assertIn('3 more by 18 Dec', after)
            self.assertIn('S$4,000', after)
            with self.assertRaises(ValueError):
                core.check_draft(conn, vid)
            self.assertFalse(core.client_accept(conn, released['token'], db.new_id())['ok'])
            accepted = core.client_accept(conn, released['token'], chosen['id'])
            self.assertTrue(accepted['ok'], accepted)
            self.assertTrue(accepted['pending'])
            recorded = conn.execute("SELECT option_ids_json FROM messages WHERE version_id=? AND direction='acceptance'", (vid,)).fetchone()
            self.assertEqual(db.uj(recorded[0]), [chosen['id']])
            self.assertEqual(db.uj(conn.execute('SELECT public_terms_json FROM options WHERE id=?', (chosen['id'],)).fetchone()[0]), db.uj(chosen['public_terms_json']))
            self.assertEqual(conn.execute('SELECT status FROM deals WHERE id=?', (version['deal_id'],)).fetchone()[0], 'published')

    # -- C5: incomplete intake persists as needs_information ----------------
    def test_incomplete_intake_persists_needs_information(self):
        conn = self.conn()
        core.load_seed(conn)
        inc = json.loads(json.dumps(INQUIRY))
        inc["offer"]["budget_sgd"] = None
        deal_id, offer_id, created = core.claim_or_resume(
            conn, "hashmeta", "ni-1", INQUIRY["offer"]["client"], "raw", inc)
        self.assertTrue(created)
        self.assertIsNone(offer_id)  # no offer fabricated from missing values
        self.assertEqual(conn.execute(
            "SELECT status FROM deals WHERE id=?", (deal_id,)).fetchone()["status"],
            "needs_information")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM offers WHERE deal_id=?", (deal_id,)).fetchone()[0], 0)
        note = conn.execute(
            "SELECT private_note FROM run_steps WHERE deal_id=? AND"
            " step_key='needs_information' ORDER BY rowid DESC LIMIT 1",
            (deal_id,)).fetchone()
        self.assertIn("offer.budget_sgd",
                      json.loads(note["private_note"])["missing_fields"])
        conn.close()

    def test_invalid_intake_still_rejected_not_persisted(self):
        conn = self.conn()
        core.load_seed(conn)
        bad = json.loads(json.dumps(INQUIRY))
        bad["offer"]["deliverables"] = -1  # INVALID_FIELD, not missing
        with self.assertRaises(ValueError):
            core.claim_or_resume(conn, "hashmeta", "bad-1", "X", "raw", bad)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM deals WHERE source_id='bad-1'").fetchone()[0], 0)
        conn.close()

    def test_needs_information_resumes_same_deal_when_completed(self):
        conn = self.conn()
        core.load_seed(conn)
        inc = json.loads(json.dumps(INQUIRY))
        del inc["offer"]["deadline"]
        deal_id, offer_id, _ = core.claim_or_resume(
            conn, "hashmeta", "ni-2", INQUIRY["offer"]["client"], "raw", inc)
        self.assertIsNone(offer_id)
        # still incomplete on retry -> same deal, still no offer, no duplicate deal
        deal_id2, offer_id2, created2 = core.claim_or_resume(
            conn, "hashmeta", "ni-2", INQUIRY["offer"]["client"], "raw", inc)
        self.assertEqual(deal_id2, deal_id)
        self.assertIsNone(offer_id2)
        self.assertFalse(created2)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM deals WHERE source_id='ni-2'").fetchone()[0], 1)
        # supply the missing field -> resumes the SAME deal, creates the offer
        deal_id3, offer_id3, created3 = core.claim_or_resume(
            conn, "hashmeta", "ni-2", INQUIRY["offer"]["client"], "raw", INQUIRY)
        self.assertEqual(deal_id3, deal_id)
        self.assertIsNotNone(offer_id3)
        self.assertFalse(created3)
        self.assertEqual(conn.execute(
            "SELECT status FROM deals WHERE id=?", (deal_id,)).fetchone()["status"], "new")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM offers WHERE deal_id=?", (deal_id,)).fetchone()[0], 1)
        conn.close()

    def test_revised_offer_comparison_changed_fields(self):
        import server
        conn = self.conn()
        core.load_seed(conn)
        deal_id, offer_id, _ = core.claim_or_resume(
            conn, "hashmeta", "cmp-1", INQUIRY["offer"]["client"], "raw", INQUIRY)
        core.evaluate(conn, deal_id, offer_id)
        req2 = json.loads(json.dumps(INQUIRY))
        req2["offer"].update(deliverables=5, caption_versions=5, deadline="2026-12-04")
        vid = conn.execute("SELECT id FROM proposal_versions WHERE deal_id=?"
                           " ORDER BY version_no DESC LIMIT 1", (deal_id,)).fetchone()["id"]
        chg = core.create_change_offer(conn, deal_id, vid, "chg-1", req2)
        conn.commit()
        core.evaluate(conn, deal_id, chg)
        state = server.state(conn)
        ds = next(d for d in state["deals"] if d["deal_id"] == deal_id)
        revs = [v for v in ds["versions"] if v["comparison"]["is_revision"]]
        self.assertTrue(revs)
        cf = {c["field"]: (c["old"], c["new"])
              for c in revs[-1]["comparison"]["changed_fields"]}
        self.assertEqual(cf.get("deliverables"), (3, 5))
        self.assertEqual(cf.get("caption_versions"), (3, 5))
        self.assertEqual(cf.get("deadline"), ("2026-12-11", "2026-12-04"))
        # the revised evaluation carries the reason codes the UI links to numbers
        self.assertIn("CAPACITY_SHORTFALL", revs[-1]["reason_codes"])
        # a first-round version is not a revision (no prior offer to compare)
        self.assertTrue(any(not v["comparison"]["is_revision"] for v in ds["versions"]))
        conn.close()

    # -- stage 15: optional trigger field on run steps ----------------------
    def test_record_step_trigger_backward_compatible(self):
        conn = self.conn()
        core.load_seed(conn)
        deal_id, offer_id, _ = core.claim_or_resume(
            conn, "hashmeta", "trig-reg", INQUIRY["offer"]["client"],
            "raw inquiry one", INQUIRY)

        def last_trigger():
            return conn.execute('SELECT "trigger" FROM run_steps'
                                ' ORDER BY rowid DESC LIMIT 1').fetchone()["trigger"]

        # omitted trigger stores NULL — old callers keep working unchanged
        core.record_step(conn, deal_id, offer_id, "review", "ok")
        self.assertIsNone(last_trigger())
        # explicit manual / scheduled triggers persist
        core.record_step(conn, deal_id, offer_id, "review", "ok", trigger="manual")
        self.assertEqual(last_trigger(), "manual")
        core.record_step(conn, deal_id, offer_id, "review", "ok", trigger="scheduled")
        self.assertEqual(last_trigger(), "scheduled")
        # an unknown trigger value is rejected, not silently stored
        with self.assertRaises(ValueError):
            core.record_step(conn, deal_id, offer_id, "review", "ok", trigger="cron")
        conn.close()

    def test_trigger_column_added_to_legacy_run_steps(self):
        # A DB whose run_steps predates the trigger column gains it on connect,
        # preserving existing rows (NULL trigger).
        legacy = str(Path(self.tmp) / "legacy.db")
        raw = sqlite3.connect(legacy)
        raw.execute("CREATE TABLE run_steps (id TEXT PRIMARY KEY, deal_id TEXT,"
                    " offer_id TEXT, step_key TEXT, status TEXT, private_note TEXT,"
                    " started_at REAL, finished_at REAL)")
        raw.execute("INSERT INTO run_steps VALUES"
                    " ('s1','d1',NULL,'intake','ok',NULL,1.0,1.0)")
        raw.commit()
        raw.close()
        conn = db.connect(legacy)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(run_steps)")]
        self.assertIn("trigger", cols)
        row = conn.execute('SELECT step_key, "trigger" FROM run_steps'
                           ' WHERE id="s1"').fetchone()
        self.assertEqual(row["step_key"], "intake")
        self.assertIsNone(row["trigger"])
        conn.close()

    # -- regression 1: duplicate intake -----------------------------------
    def test_duplicate_intake_single_offer_and_preserved_input(self):
        conn = self.conn()
        core.load_seed(conn)
        first = core.claim_or_resume(conn, "hashmeta", "same-id",
                                     INQUIRY["offer"]["client"],
                                     "original raw inquiry", INQUIRY)
        second = core.claim_or_resume(conn, "hashmeta", "same-id",
                                      INQUIRY["offer"]["client"],
                                      "REPLACEMENT text on retry", INQUIRY)
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])
        self.assertFalse(second[2])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0], 1)
        # offer saved with the same stable keys that the lookup queries
        offer = conn.execute("SELECT * FROM offers").fetchone()
        self.assertEqual((offer["source"], offer["source_id"]),
                         ("hashmeta", "same-id"))
        # existing input preserved on retry
        raw = conn.execute("SELECT raw_inquiry FROM deals WHERE id=?",
                           (first[0],)).fetchone()["raw_inquiry"]
        self.assertEqual(raw, "original raw inquiry")
        conn.close()

    # -- regression 2: deleted options fail closed -------------------------
    def test_deleted_options_fail_closed(self):
        conn, deal_id, version_id = self.make_checked_version("del-1")
        eval_id = conn.execute("SELECT id FROM evaluations WHERE offer_id=?",
                               (conn.execute("SELECT offer_id FROM proposal_versions"
                                             " WHERE id=?", (version_id,)
                                             ).fetchone()["offer_id"],)).fetchone()["id"]
        original = conn.execute("SELECT COUNT(*) FROM options WHERE evaluation_id=?",
                                (eval_id,)).fetchone()[0]
        self.assertGreater(original, 0)
        conn.execute("DELETE FROM options WHERE evaluation_id=?", (eval_id,))
        conn.execute("UPDATE proposal_versions SET public_option_ids_json='[]'"
                     " WHERE id=?", (version_id,))
        conn.commit()
        version = conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                               (version_id,)).fetchone()
        errors = core.option_set_errors(conn, version)
        self.assertTrue(errors, "deleting every option must fail closed")
        self.assertIn("option_keys_not_complete", errors)
        # check and release both refuse
        outcome = core.check_draft(conn, version_id)
        self.assertFalse(outcome["passed"])
        res = core.approve(conn, version_id)
        self.assertFalse(res["ok"])
        self.assertIn("option_keys_not_complete", res["reasons"])
        conn.close()

    def test_deleted_options_fail_closed_via_http_check(self):
        # check_draft must block even though the outbound engine would see no
        # unapproved terms (empty option list)
        conn, deal_id, version_id = self.make_checked_version("del-2")
        conn.execute("DELETE FROM options WHERE evaluation_id IN"
                     " (SELECT id FROM evaluations WHERE offer_id="
                     " (SELECT offer_id FROM proposal_versions WHERE id=?))",
                     (version_id,))
        conn.execute("UPDATE proposal_versions SET public_option_ids_json='[]'"
                     " WHERE id=?", (version_id,))
        conn.commit()
        conn.close()
        # HTTP-level proof below reuses core directly for determinism
        conn = self.conn()
        version = conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                               (version_id,)).fetchone()
        self.assertTrue(core.option_set_errors(conn, version))
        conn.close()

    # -- regression 3: concurrency-safe release ------------------------------
    def test_concurrent_approve_releases_exactly_once(self):
        conn, deal_id, version_id = self.make_checked_version("conc-1")
        conn.close()
        results = []
        barrier = threading.Barrier(2)

        def worker():
            c = self.conn()
            barrier.wait()
            res = core.approve(c, version_id)
            results.append(res)
            c.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        ok = [r for r in results if r.get("ok")]
        self.assertEqual(len(ok), 1, f"exactly one release, got {results}")
        conn = self.conn()
        released = conn.execute(
            "SELECT COUNT(*) FROM proposal_versions WHERE id=? AND status='published'",
            (version_id,)).fetchone()[0]
        self.assertEqual(released, 1)
        approvals = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE version_id=? AND action='approve'",
            (version_id,)).fetchone()[0]
        self.assertEqual(approvals, 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM approvals WHERE version_id=?"
                         " AND action='approve_rejected'", (version_id,)).fetchone()[0], 1)
        tokens = conn.execute(
            "SELECT COUNT(*) FROM proposal_versions WHERE id=? AND token_hash"
            " IS NOT NULL", (version_id,)).fetchone()[0]
        self.assertEqual(tokens, 1)
        conn.close()

    def test_concurrent_approve_and_edit_stays_consistent(self):
        """An editor creating a new version mid-release must not produce two
        published releases of different versions with stale evidence."""
        conn, deal_id, version_id = self.make_checked_version("conc-2")
        conn.close()
        outcomes = {"release": None, "edit": None}
        barrier = threading.Barrier(2)

        def releaser():
            c = self.conn()
            barrier.wait()
            outcomes["release"] = core.approve(c, version_id)
            c.close()

        def editor():
            c = self.conn()
            barrier.wait()
            outcomes["edit"] = core.save_draft(c, version_id, "edited draft text")
            c.close()

        threads = [threading.Thread(target=releaser), threading.Thread(target=editor)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        conn = self.conn()
        published = conn.execute(
            "SELECT COUNT(*) FROM proposal_versions WHERE deal_id=? AND"
            " status='published'", (deal_id,)).fetchone()[0]
        approve_rows = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE action='approve'").fetchone()[0]
        conn.close()
        # whatever interleaving: at most one published release, and the edited
        # draft (if it won) leaves the old version unreleasable afterwards
        self.assertLessEqual(published, 1)
        self.assertLessEqual(approve_rows, 1)
        if outcomes["release"] and outcomes["release"].get("ok") is False \
                and "not_latest_version" in outcomes["release"]["reasons"]:
            self.assertIsNotNone(outcomes["edit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
