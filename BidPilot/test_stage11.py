"""BidPilot Stage 11 acceptance tests: booking-capacity reservation ledger.

Runs entirely on isolated temporary databases (never the preview DB). Covers
the required acceptance scenarios A-G:

  A  fresh DB demo numbers + counter path + no ledger before confirmation
  B  one confirmation allocates earliest weeks; a second identical inquiry
     sees genuine recomputed options against residual capacity
  C  two competing confirmations on separate connections to one temp file:
     exactly one wins, loser fails safely, retries are idempotent
  D  phased allocation uses cumulative option metrics (no double shoot)
  E  capacity change after evaluation blocks release; after release blocks
     confirmation; explicit re-evaluation path produces fresh round
  F  dedupe/closed/restart/token-rotation/confirmed-deal amendment never
     duplicate or release reservations; CSVs unchanged
  G  unsupplied weeks/metric mismatches fail closed; forced allocation
     failure rolls back atomically; MCP and client boundary guards

Run: python3 -B -m unittest -v test_stage11
"""
import hashlib
import json
import math
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import capacity as cap
import core
import db
import mcp_server
import server


INQUIRY = {
    "as_of": "2026-11-23",
    "comparable_ids": ["P-031", "P-036", "P-041", "P-044"],
    "offer": {"client": "Juniper Cafe", "format": "product_promo",
              "production_mode": "one_shoot_day", "shoot_days": 1, "locations": 1,
              "interviewees": 0, "scripting": "light", "deliverables": 3,
              "editing_level": "standard", "graphics": "basic_titles",
              "revision_rounds": 2, "caption_versions": 3, "deadline": "2026-12-11",
              "budget_sgd": 2700, "direct_costs_sgd": 0},
}
RAW = "three 30-second Instagram promo videos, one location, captions, by 11 Dec, budget S$2,700"
CHANGE = "five videos, five captioned versions by 4 Dec for S$2,700"

ENGINE_DATA = Path(core.ENGINE_DIR) / "data"
CSV_HASHES = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(ENGINE_DATA.glob("*.csv"))}


def fresh_db(prefix="stage11-"):
    tmp = tempfile.mkdtemp(prefix=prefix)
    path = str(Path(tmp) / "s11.db")
    conn = db.connect(path)
    core.load_seed(conn)
    return path, conn


def flow(conn, source_id, inquiry=INQUIRY, raw=RAW):
    """intake -> evaluate -> check -> release; returns dict with ids + token."""
    deal_id, offer_id, _ = core.claim_or_resume(
        conn, "hashmeta", source_id, inquiry["offer"]["client"], raw, inquiry)
    eval_id, version_id = core.evaluate(conn, deal_id, offer_id)
    outcome = core.check_draft(conn, version_id)
    assert outcome["passed"], outcome
    res = core.approve(conn, version_id)
    assert res["ok"], res
    return {"deal_id": deal_id, "offer_id": offer_id, "eval_id": eval_id,
            "version_id": version_id, "token": res["token"]}


def accept(conn, flowres, option_key=None):
    version = conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                           (flowres["version_id"],)).fetchone()
    rows = core.options_of(conn, version)
    if option_key:
        row = next(r for r in rows if r["option_key"] == option_key)
    else:
        row = rows[0]
    res = core.client_accept(conn, flowres["token"], row["id"])
    assert res["ok"], res
    return row


def est_of(conn, offer_id):
    row = conn.execute("SELECT result_json FROM evaluations WHERE offer_id=?",
                       (offer_id,)).fetchone()
    return db.uj(row["result_json"])["estimate"]


class Stage11Tests(unittest.TestCase):
    # ---- A: fresh database, original numbers, no ledger before confirm -----
    def test_a_fresh_demo_numbers_and_no_ledger(self):
        _path, conn = fresh_db()
        f = flow(conn, "s11-a-1")
        est = est_of(conn, f["offer_id"])
        self.assertEqual((est["planning_hours"], est["capacity_hours"]),
                         (35.07, 50.40))
        self.assertEqual((est["list_sgd"], est["floor_sgd"]), (2700.00, 2454.90))
        # explicit five-caption counter path on the same deal
        offer2 = core.client_change(conn, f["deal_id"], CHANGE, "s11-a-chg")
        self.assertIsNotNone(offer2)
        _e, v2 = core.evaluate(conn, f["deal_id"], offer2)
        est2 = est_of(conn, offer2)
        self.assertEqual((est2["planning_hours"], est2["capacity_hours"]),
                         (52.32, 28.80))
        kinds = {r["option_key"] for r in core.options_of(
            conn, conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                               (v2,)).fetchone())}
        self.assertEqual(kinds, {"hold_date", "hold_budget", "hold_scope", "phased"})
        # nothing reserved anywhere before confirmation
        for table in ("reservations", "allocations", "capacity_revisions"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            self.assertEqual(n, 0, table)
        # the released-but-unaccepted original and its counter are one deal
        n_offers = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        self.assertEqual(n_offers, 2)

    # ---- B: first confirmation allocates earliest eligible weeks ----------
    def test_b_first_booking_then_second_inquiry_sees_residual(self):
        path, conn = fresh_db()
        f1 = flow(conn, "s11-b-1")
        accept(conn, f1)
        res = core.confirm_booking(conn, f1["version_id"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["allocation"], [
            {"week": "2026-11-27", "hours": 18.00},
            {"week": "2026-12-04", "hours": 10.80},
            {"week": "2026-12-11", "hours": 6.27}])
        led = cap.ledger(conn, as_of="2026-11-23", deadline="2026-12-11")
        self.assertEqual(led["2026-12-11"]["residual"], 1533)  # 15.33 h
        # engine adapter shows exactly the residual, never more
        visible = cap.engine_visible_capacity(
            cap.adapted_data(core.seed_data(), cap.ledger(conn)),
            "2026-11-23", "2026-12-11")
        self.assertEqual(visible, 15.33)
        # a distinct identical subsequent inquiry: recomputed genuine options
        conn.close()
        conn = db.connect(path)
        f2 = flow(conn, "s11-b-2")
        est2 = est_of(conn, f2["offer_id"])
        self.assertEqual(est2["capacity_hours"], 15.33)
        self.assertGreater(est2["planning_hours"], est2["capacity_hours"])
        row = conn.execute("SELECT verdict, result_json FROM evaluations WHERE offer_id=?",
                           (f2["offer_id"],)).fetchone()
        result = db.uj(row["result_json"])
        self.assertIn(row["verdict"], ("counter", "decline"))
        if row["verdict"] == "counter":
            metrics = result["option_metrics"]
            for option in result["options"]:
                m = metrics.get(option["option_id"])
                if m:
                    self.assertLessEqual(m["planning_hours"], m["capacity_hours"])

    # ---- C: competing confirmations on separate connections ----------------
    def test_c_concurrent_confirmation_single_winner(self):
        path, conn = fresh_db()
        f1 = flow(conn, "s11-c-1")
        f2 = flow(conn, "s11-c-2")
        accept(conn, f1)
        accept(conn, f2)
        conn.close()
        outcomes = {}

        def confirm(version_id, key):
            c = sqlite3.connect(path, timeout=10)
            c.row_factory = sqlite3.Row
            try:
                outcomes[key] = core.confirm_booking(c, version_id)
            finally:
                c.close()

        threads = [threading.Thread(target=confirm, args=(f1["version_id"], "one")),
                   threading.Thread(target=confirm, args=(f2["version_id"], "two"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [k for k, r in outcomes.items() if r["ok"]]
        self.assertEqual(len(winners), 1, outcomes)
        loser = "two" if winners == ["one"] else "one"
        # loser fails safely with capacity_changed (or a bounded busy result
        # that turns into a deterministic capacity rejection on retry)
        self.assertIn(outcomes[loser].get("reasons", []),
                      [["capacity_changed", "review_required"], ["busy"]])
        c = db.connect(path)
        if outcomes[loser]["reasons"] == ["busy"]:
            retry = core.confirm_booking(c, f"loser-version")
        retry = core.confirm_booking(
            c, (f1 if loser == "one" else f2)["version_id"])
        self.assertFalse(retry["ok"])
        self.assertEqual(retry["reasons"], ["capacity_changed", "review_required"])
        # acceptance of the loser is still pending, nothing confirmed
        v = c.execute("SELECT status FROM proposal_versions WHERE id=?",
                      ((f1 if loser == "one" else f2)["version_id"],)).fetchone()
        self.assertEqual(v["status"], "published")
        # retry winner: same successful reservation, no duplicate side effects
        again = core.confirm_booking(c, (f1 if winners == ["one"] else f2)["version_id"])
        self.assertTrue(again["ok"] and again["already"])
        self.assertEqual(c.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0], 1)
        total = c.execute(
            "SELECT SUM(planning_hundredths) FROM allocations").fetchone()[0]
        self.assertEqual(total, 3507)
        for w in cap.ledger(c).values():
            self.assertGreaterEqual(w["residual"], 0)
        c.close()

    # ---- D: phased cumulative allocation, single shoot ---------------------
    def test_d_phased_allocation_exact(self):
        path, conn = fresh_db()
        f = flow(conn, "s11-d-1")
        offer2 = core.client_change(conn, f["deal_id"], CHANGE, "s11-d-chg")
        _e, v2 = core.evaluate(conn, f["deal_id"], offer2)
        core.check_draft(conn, v2)
        res2 = core.approve(conn, v2)
        self.assertTrue(res2["ok"], res2)
        phased = accept(conn, {"version_id": v2, "token": res2["token"]}, "phased")
        conf = core.confirm_booking(conn, v2)
        self.assertTrue(conf["ok"], conf)
        alloc = {a["week"]: a["hours"] for a in conf["allocation"]}
        self.assertEqual(alloc, {"2026-11-27": 18.00, "2026-12-04": 10.80,
                                 "2026-12-11": 21.60, "2026-12-18": 1.92})
        self.assertEqual(math.fsum(alloc.values()), 52.32)  # One shared shoot.
        # exact milestones persisted; week 4 Dec milestone is the first phase
        rows = conn.execute("SELECT week_ending, planning_hundredths, milestone"
                            " FROM allocations ORDER BY week_ending").fetchall()
        self.assertEqual([r["planning_hundredths"] for r in rows],
                         [1800, 1080, 2160, 192])
        self.assertEqual(rows[1]["milestone"], "2026-12-04")
        # a later competing deal sees the actual weekly reductions
        conn.close()
        conn = db.connect(path)
        led = cap.ledger(conn, as_of="2026-11-23", deadline="2026-12-18")
        self.assertEqual([led[w]["residual"] / 100 for w in sorted(led)],
                         [0.0, 0.0, 0.0, 19.68])

    # ---- E: stale capacity blocks release and confirmation -----------------
    def test_e_capacity_change_blocks_release_then_confirmation(self):
        path, conn = fresh_db()
        # deal 1 evaluated and checked, NOT yet released
        deal_id, offer_id, _ = core.claim_or_resume(
            conn, "hashmeta", "s11-e-1", "Juniper Cafe", RAW, INQUIRY)
        _eval_id, version_id = core.evaluate(conn, deal_id, offer_id)
        self.assertTrue(core.check_draft(conn, version_id)["passed"])
        f1 = {"deal_id": deal_id, "offer_id": offer_id, "eval_id": _eval_id,
              "version_id": version_id}
        # another deal books first, consuming the shared capacity
        f2 = flow(conn, "s11-e-2")
        accept(conn, f2)
        self.assertTrue(core.confirm_booking(conn, f2["version_id"])["ok"])
        # unsafe previously quoted options can no longer release
        res = core.approve(conn, f1["version_id"])
        self.assertFalse(res["ok"])
        self.assertIn("capacity_changed", res["reasons"])
        self.assertIn("review_required", res["reasons"])
        self.assertIn("unsafe_options:as_offered", res["reasons"])
        # the explicit re-evaluation path yields a fresh round
        new_offer = core.reevaluate(conn, f1["offer_id"])
        _e, v3 = core.evaluate(conn, f1["deal_id"], new_offer)
        est3 = est_of(conn, new_offer)
        self.assertEqual(est3["capacity_hours"], 15.33)
        core.check_draft(conn, v3)
        res3 = core.approve(conn, v3)
        # fresh safe options (or none) — but never the stale accept set
        row = conn.execute("SELECT verdict FROM evaluations WHERE offer_id=?",
                           (new_offer,)).fetchone()
        self.assertIn(row["verdict"], ("counter", "decline"))
        # original stale version stays unreleased historical evidence
        self.assertEqual(conn.execute(
            "SELECT status FROM proposal_versions WHERE id=?",
            (version_id,)).fetchone()["status"], "checked")
        # capacity change after release but before confirmation also fails
        # (fresh scenario so both deals genuinely fit at release time)
        conn.close()
        path, conn = fresh_db("stage11-e2-")
        f3 = flow(conn, "s11-e-3")
        f4 = flow(conn, "s11-e-4")
        accept(conn, f3)
        accept(conn, f4)
        self.assertTrue(core.confirm_booking(conn, f4["version_id"])["ok"])
        conf3 = core.confirm_booking(conn, f3["version_id"])
        self.assertFalse(conf3["ok"])
        self.assertEqual(conf3["reasons"], ["capacity_changed", "review_required"])
        self.assertIn("next_action", conf3)
        # acceptance retained pending; no public terms mutated
        v = conn.execute("SELECT status, draft_body FROM proposal_versions WHERE id=?",
                         (f3["version_id"],)).fetchone()
        self.assertEqual(v["status"], "published")
        est3b = est_of(conn, f3["offer_id"])
        self.assertEqual((est3b["planning_hours"], est3b["capacity_hours"]),
                         (35.07, 50.40))  # stored evidence immutable

    # ---- F: no duplication or release across lifecycle events -------------
    def test_f_no_duplicate_or_released_reservations(self):
        path, conn = fresh_db()
        f = flow(conn, "s11-f-1")
        accept(conn, f)
        conf = core.confirm_booking(conn, f["version_id"])
        self.assertTrue(conf["ok"])
        # idempotent retry: same reservation, no new rows
        again = core.confirm_booking(conn, f["version_id"])
        self.assertTrue(again["already"])
        self.assertEqual(again["reservation_id"], conf["reservation_id"])
        # duplicate intake resumes the deal: no second offer, no reservation
        deal_id, offer_id, fresh = core.claim_or_resume(
            conn, "hashmeta", "s11-f-1", "Juniper Cafe", RAW, INQUIRY)
        self.assertFalse(fresh)
        self.assertEqual(offer_id, f["offer_id"])
        # token rotation keeps the reservation
        rot = core.rotate_token(conn, f["version_id"])
        self.assertTrue(rot["ok"])
        # process restart: new connection, ledger persists
        conn.close()
        conn = db.connect(path)
        led = cap.ledger(conn)
        self.assertEqual(sum(w["allocated"] for w in led.values()), 3507)
        # closed deal = administrative closure only; reservation stays active
        self.assertTrue(core.close_deal(conn, f["deal_id"])["ok"])
        led = cap.ledger(conn)
        self.assertEqual(sum(w["allocated"] for w in led.values()), 3507)
        r = conn.execute("SELECT status FROM reservations WHERE deal_id=?",
                         (f["deal_id"],)).fetchone()
        self.assertEqual(r["status"], "active")
        # amendment of a confirmed deal: clear refusal, reservation untouched
        with self.assertRaises(ValueError):
            core.client_change(conn, f["deal_id"], CHANGE, "s11-f-chg")
        with self.assertRaises(ValueError):
            core.reevaluate(conn, f["offer_id"])
        res = core.approve(conn, f["version_id"])
        self.assertFalse(res["ok"])
        self.assertIn("manual_review_required", " ".join(res["reasons"]))
        self.assertEqual(sum(w["allocated"] for w in cap.ledger(conn).values()), 3507)
        # rejected/unaccepted versions never reserved anything
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM reservations").fetchone()[0], 1)
        # CSVs unchanged on disk
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(ENGINE_DATA.glob("*.csv"))}
        self.assertEqual(hashes, CSV_HASHES)
        # seed mirror still matches the CSVs
        self.assertEqual(core.seed_errors(conn, conn.execute(
            "SELECT * FROM proposal_versions WHERE id=?",
            (f["version_id"],)).fetchone()), [])

    # ---- G: fail-closed, rollback, boundary guards ------------------------
    def test_g_fail_closed_and_rollback(self):
        path, conn = fresh_db()
        # unsupported deadline beyond the supplied calendar fails closed
        f = flow(conn, "s11-g-1")
        accept(conn, f)
        conn.execute(
            "UPDATE options SET public_terms_json=? WHERE evaluation_id=?"
            " AND option_key='as_offered'",
            (db.j({"option_id": "as_offered", "kind": "as_offered", "deliverables": 3,
                   "caption_versions": 3, "revision_rounds": 2, "shoot_days": 1,
                   "locations": 1, "deadline": "2027-01-15", "price_sgd": 2700.0,
                   "phases": []}), f["eval_id"]))
        conn.commit()
        res = core.confirm_booking(conn, f["version_id"])
        self.assertFalse(res["ok"])
        # tampered option terms (unsupported date) fail closed, never extend
        # the calendar implicitly and never reserve anything
        self.assertTrue(set(res["reasons"]) & {
            "option_metric_mismatch", "option_terms_mismatch:as_offered"}, res)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM reservations").fetchone()[0], 0)
        # unsupplied weeks contribute zero budget
        led = cap.ledger(conn, as_of="2026-11-23", deadline="2027-01-15")
        self.assertEqual(led.get("2027-01-15"), None)
        # forced allocation-insert failure rolls the whole transaction back
        conn.execute(
            "CREATE TRIGGER fail_alloc BEFORE INSERT ON allocations"
            " BEGIN SELECT RAISE(ABORT, 'forced'); END")
        conn.commit()
        f2 = flow(conn, "s11-g-2")
        accept(conn, f2)
        res2 = core.confirm_booking(conn, f2["version_id"])
        self.assertEqual(res2["reasons"], ["atomic_failure"])
        for sql in ("SELECT COUNT(*) FROM reservations",
                    "SELECT COUNT(*) FROM allocations",
                    "SELECT COUNT(*) FROM confirmations",
                    "SELECT COUNT(*) FROM capacity_revisions"):
            self.assertEqual(conn.execute(sql).fetchone()[0], 0, sql)
        v = conn.execute("SELECT status FROM proposal_versions WHERE id=?",
                         (f2["version_id"],)).fetchone()
        self.assertEqual(v["status"], "published")
        d = conn.execute("SELECT status FROM deals WHERE id=?",
                         (f2["deal_id"],)).fetchone()
        self.assertEqual(d["status"], "published")

    def test_g_mcp_and_client_boundary(self):
        _path, conn = fresh_db()
        f = flow(conn, "s11-g-3")
        accept(conn, f)
        core.confirm_booking(conn, f["version_id"])
        # MCP tool surface: no booking/reservation/capacity writes at all
        names = {name for name, _fn, _desc in mcp_server.TOOLS}
        for banned in ("confirm_booking", "cancel_reservation", "reserve",
                       "modify_reservation", "release_capacity"):
            self.assertNotIn(banned, names)
        self.assertEqual(len(names), 12)
        # client projection: no private ledger/metric terms cross the boundary
        version = conn.execute("SELECT * FROM proposal_versions WHERE id=?",
                               (f["version_id"],)).fetchone()
        html = server.render_client_html(conn, version, preview=False)
        for word in ("allocation", "reservation", "planning hours", "capacity",
                     "floor", "hundredths"):
            self.assertNotIn(word, html.lower())
        # MCP negotiation payload stays outward-safe (no capacity numbers);
        # the MCP tool descriptions expose no reservation/capacity writes
        descs = " ".join(desc for _n, _fn, desc in mcp_server.TOOLS).lower()
        for word in ("reservation", "allocat", "confirm ", "booking"):
            self.assertNotIn(word, descs)


if __name__ == "__main__":
    unittest.main()
