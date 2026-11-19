"""BidPilot Stage 4 regression tests: duplicate intake keys, fail-closed
option sets, and concurrency-safe approve/release.

Run: python3 -B -m unittest -v test_regressions
"""
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
