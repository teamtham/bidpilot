"""Booking-capacity reservation ledger (sample-only prototype).

Separates the immutable baseline calendar (CSVs, never modified) from durable
booking allocations. All arithmetic uses integer hundredths of planning hours.
The 10% capacity reserve is applied exactly once per week: a week's buffered
budget is round(free * (1 - reserve_pct/100), 2) in hundredths, matching the
engine's own per-aggregate rounding for this calendar.

The engine itself is unchanged: evaluations see residual capacity through a
conservative data adapter (copied tables only) that raises each week's
``unscheduled_owed_hours`` just enough that the engine's buffered capacity for
that week rounds to at most the true residual (never more). Final exact-cent
allocation feasibility inside the confirmation transaction is authoritative.
"""
import copy
import json
import math
from datetime import date, timedelta

import db

# ---- baseline calendar ------------------------------------------------------


def _iso(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


def reserve_pct(data):
    for row in data["business_rules"]:
        if row["key"] == "capacity_buffer_pct":
            return float(row["value"])
    raise ValueError("capacity_buffer_pct missing from business rules")


def week_budget_cents(row, pct):
    """Buffered weekly planning budget in integer hundredths (reserve once)."""
    free = max(0.0, float(row["working_hours"]) - float(row["calendar_busy_hours"])
               - float(row["unscheduled_owed_hours"]))
    return int(round(round(free * (1 - pct / 100), 2) * 100))


def eligible(week_ending, as_of, deadline):
    """Original engine eligibility: full week ending strictly after as_of+4, by deadline."""
    return _iso(as_of) + timedelta(days=4) <= _iso(week_ending) <= _iso(deadline)


def baseline_weeks(data):
    """[(week_ending, budget_cents)] for every supplied calendar week, CSV order."""
    pct = reserve_pct(data)
    return [(row["week_ending"], week_budget_cents(row, pct))
            for row in sorted(data["calendar_weeks"], key=lambda r: r["week_ending"])]


def calendar_data(conn, data=None):
    """Apply owner calendar settings to a copy of the immutable seed data."""
    out = copy.deepcopy(data or _seed())
    setting = conn.execute('SELECT working_hours FROM calendar_settings WHERE id=1').fetchone()
    blocks = {row['week_ending']: row['hours'] for row in conn.execute(
        'SELECT week_ending, SUM(hours) AS hours FROM calendar_blocks GROUP BY week_ending')}
    for row in out['calendar_weeks']:
        if setting:
            row['working_hours'] = setting['working_hours']
        row['calendar_busy_hours'] = float(row['calendar_busy_hours']) + blocks.get(row['week_ending'], 0)
    return out


def change_calendar(conn, action, data):
    """Save a calendar change and revision atomically; never invent capacity."""
    import core
    conn.execute('BEGIN IMMEDIATE')
    try:
        if action == 'hours':
            hours = data.get('hours')
            if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or not 0 < hours <= 80:
                raise ValueError('Enter working hours between 0 and 80.')
            current = conn.execute('SELECT working_hours FROM calendar_settings WHERE id=1').fetchone()
            if current and current['working_hours'] == hours:
                revision = ledger_revision(conn)
                conn.commit()
                return revision
            conn.execute('INSERT INTO calendar_settings VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET working_hours=excluded.working_hours', (hours,))
        elif action == 'block':
            week, hours, reason = data.get('week'), data.get('hours'), data.get('reason')
            if week not in dict(baseline_weeks(_seed())):
                raise ValueError('Choose a supported calendar week.')
            if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or not 0 < hours <= 80:
                raise ValueError('Enter block hours between 0 and 80.')
            if reason not in ('Leave', 'Admin', 'Outside booking'):
                raise ValueError('Choose a reason for the block.')
            conn.execute('INSERT INTO calendar_blocks VALUES (?,?,?,?,?)', (db.new_id(), week, hours, reason, db.now()))
        elif action == 'remove':
            if conn.execute('DELETE FROM calendar_blocks WHERE id=?', (data.get('id'),)).rowcount != 1:
                raise ValueError('That block no longer exists.')
        else:
            raise ValueError('Unknown calendar action.')
        revision = ledger_revision(conn) + 1
        weeks = ledger(conn)
        snapshot = {'revision': revision, 'weeks': [{'week': w, **v} for w, v in sorted(weeks.items())]}
        conn.execute('INSERT INTO capacity_revisions VALUES (?,?,?,?)',
                     (revision, core.seed_hash(core.seed_data()), db.j(snapshot), db.now()))
        conn.commit()
        return revision
    except Exception:
        conn.rollback()
        raise


# ---- durable ledger ---------------------------------------------------------


def allocated_cents(conn):
    """Active confirmed allocations per week, in hundredths."""
    rows = conn.execute(
        "SELECT a.week_ending AS w, SUM(a.planning_hundredths) AS c"
        " FROM allocations a JOIN reservations r ON r.id = a.reservation_id"
        " WHERE r.status = 'active' GROUP BY a.week_ending").fetchall()
    return {r["w"]: r["c"] or 0 for r in rows}


def ledger(conn, data=None, as_of=None, deadline=None):
    """{week_ending: {budget, allocated, residual}} in hundredths.

    With as_of/deadline, only eligible weeks are included.
    """
    weeks = baseline_weeks(calendar_data(conn, data))
    alloc = allocated_cents(conn)
    out = {}
    for week, budget in weeks:
        if as_of is not None and not eligible(week, as_of, deadline):
            continue
        used = alloc.get(week, 0)
        out[week] = {"budget": budget, "allocated": used, "residual": budget - used}
    return out


def ledger_revision(conn):
    row = conn.execute("SELECT MAX(revision) AS r FROM capacity_revisions").fetchone()
    return (row["r"] or 0) if row else 0


def _seed():
    import core
    return core.seed_data()


# ---- deterministic allocation -----------------------------------------------


def plan(ledger_weeks, demands):
    """Allocate demands=[(hundredths, deadline)] earliest eligible week first.

    Returns [(week_ending, hundredths, milestone_deadline)] excluding zero
    fills, or None when a demand cannot be met within its deadline and
    residual budgets.
    """
    residual = {w: v["residual"] for w, v in ledger_weeks.items()}
    taken = {}  # week -> [hundredths, first milestone deadline that touched it]
    for need, deadline in demands:
        remaining = need
        for week in sorted(residual):
            if not _eligible_from_ledger(week, deadline):
                continue
            if remaining <= 0:
                break
            take = min(residual[week], remaining)
            if take > 0:
                residual[week] -= take
                remaining -= take
                if week in taken:
                    taken[week][0] += take
                else:
                    taken[week] = [take, deadline]
        if remaining > 0:
            return None
    return [(week, take, milestone) for week, (take, milestone) in sorted(taken.items())]


def _eligible_from_ledger(week, deadline):
    return _iso(week) <= _iso(deadline)


def option_demands(eval_result, terms):
    """[(hundredths, deadline)] for one engine option, from option-side metrics.

    Non-phased: total planning_hours by the option deadline. Phased: cumulative
    first-phase planning hours by the first deadline, then total minus first by
    the final deadline. The batch metrics already include the single shared
    shoot; nothing is summed twice.
    """
    metrics = (eval_result.get("option_metrics") or {}).get(terms["option_id"])
    est = eval_result.get("estimate") or {}
    phases = terms.get("phases") or []
    if metrics is None:
        metrics = {"planning_hours": est.get("planning_hours")}
    total = int(round(float(metrics["planning_hours"]) * 100))
    if phases:
        first = int(round(float(metrics["first_phase_planning_hours"]) * 100))
        return [(first, phases[0]["deadline"]), (total - first, phases[-1]["deadline"])]
    return [(total, terms["deadline"])]


# ---- engine data adapter ----------------------------------------------------


def adapted_data(data, ledger_weeks):
    """Copied tables where each week's engine-visible buffered capacity is at
    most its ledger residual (conservative, never above baseline).

    Conversion/rounding: for a week with baseline free f (CSV hours) and
    residual r (hundredths), we raise unscheduled_owed_hours by the smallest d
    (hundredths) with round((f - d) * (1 - pct/100), 2) * 100 <= r. The engine's
    capacity() then reports <= r for that week in isolation and <= sum(r) in
    aggregate; the exact-cent plan inside the confirmation transaction remains
    authoritative. The CSVs and the SQLite seed mirror are never touched.
    """
    pct = reserve_pct(data)
    out = copy.deepcopy(data)
    rows = {r["week_ending"]: r for r in out["calendar_weeks"]}
    for week, snap in ledger_weeks.items():
        row = rows.get(week)
        if row is None:
            continue
        free = max(0.0, float(row["working_hours"]) - float(row["calendar_busy_hours"])
                   - float(row["unscheduled_owed_hours"]))
        owed = float(row["unscheduled_owed_hours"])
        residual = snap["residual"]
        d = 0
        limit = int(round(free * 100)) + 1
        while d < limit and int(round(round((free - d / 100) * (1 - pct / 100), 2) * 100)) > residual:
            d += 1
        if d:
            row["unscheduled_owed_hours"] = round(owed + d / 100, 2)
    return out


def engine_visible_capacity(data, as_of, deadline):
    """What the engine would compute for capacity() on the baseline tables."""
    pct = reserve_pct(data)
    free = sum(
        max(0.0, float(r["working_hours"]) - float(r["calendar_busy_hours"])
            - float(r["unscheduled_owed_hours"]))
        for r in data["calendar_weeks"] if eligible(r["week_ending"], as_of, deadline))
    return round(free * (1 - pct / 100), 2)


def scoped_plan(weeks, as_of, terms, evaluation):
    """Run the booking allocator on eligible weeks of a supplied ledger snapshot."""
    permitted = {week: values for week, values in weeks.items()
                 if eligible(week, as_of, terms["deadline"])}
    return plan(permitted, option_demands(evaluation, terms))


def booking_preview(conn, version_id, option_id):
    """Read-only weekly and resource simulation for one candidate option.

    Detailed options use the exact resource planner later called by booking
    confirmation.  Its proposed allocations are layered in memory while other
    published options are checked; no allocation, reservation, or revision is
    written by this function.  Each public option remains an independent
    alternative, not a tentative booking.
    """
    import estimating
    candidate = conn.execute(
        "SELECT v.*,o.as_of,e.result_json FROM proposal_versions v "
        "JOIN offers o ON o.id=v.offer_id JOIN evaluations e ON e.offer_id=o.id "
        "WHERE v.id=? AND v.status IN ('draft','checked','published')", (version_id,)
    ).fetchone()
    if not candidate or option_id not in db.uj(candidate["public_option_ids_json"]):
        raise ValueError("option_unavailable")
    opt = conn.execute("SELECT public_terms_json FROM options WHERE id=?", (option_id,)).fetchone()
    if not opt:
        raise ValueError("option_unavailable")
    before = ledger(conn)
    candidate_terms = db.uj(opt[0])
    candidate_result = db.uj(candidate["result_json"])
    allocation = scoped_plan(before, candidate["as_of"], candidate_terms, candidate_result)
    resource = estimating.resource_check(conn, candidate_result, candidate_terms)
    summary = {"fits": allocation is not None and resource["feasible"],
               "weekly_fits": allocation is not None,
               "resource_fits": resource["feasible"],
               "resource": {"feasible": resource["feasible"],
                            "blocked_task_id": resource.get("blocked_task_id"),
                            "allocations": resource.get("allocations", [])},
               "revision": ledger_revision(conn), "weeks": [], "affected": []}
    if not summary["fits"]:
        return summary
    after = {week: dict(item) for week, item in before.items()}
    for week, amount, milestone in allocation:
        after[week]["residual"] -= amount
        summary["weeks"].append({"week": week, "before": before[week]["residual"]/100,
                                 "after": after[week]["residual"]/100,
                                 "reserved": amount/100, "milestone": milestone})
    rows = conn.execute(
        "SELECT d.client,v.id AS version_id,v.offer_id,v.public_option_ids_json,"
        "o.as_of,e.result_json FROM deals d JOIN proposal_versions v ON v.id=d.current_version_id "
        "JOIN offers o ON o.id=v.offer_id JOIN evaluations e ON e.offer_id=o.id "
        "WHERE d.id<>? AND v.status='published' AND d.status NOT IN ('closed','confirmed') "
        "ORDER BY d.client,v.id", (candidate["deal_id"],))
    for row in rows:
        evaluation = db.uj(row["result_json"])
        ids = db.uj(row["public_option_ids_json"])
        selection = conn.execute("SELECT option_ids_json FROM messages WHERE version_id=? "
                                 "AND direction='acceptance'", (row["version_id"],)).fetchone()
        if selection:
            ids = db.uj(selection[0])
        lost = []
        remaining = 0
        for oid in ids:
            terms_row = conn.execute("SELECT public_terms_json FROM options WHERE id=?", (oid,)).fetchone()
            if not terms_row:
                continue
            terms = db.uj(terms_row[0])
            before_weekly = scoped_plan(before, row["as_of"], terms, evaluation)
            before_resource = estimating.resource_check(conn, evaluation, terms)
            if before_weekly is None or not before_resource["feasible"]:
                continue
            after_weekly = scoped_plan(after, row["as_of"], terms, evaluation)
            after_resource = estimating.resource_check(
                conn, evaluation, terms, resource.get("allocations", []))
            reasons = []
            if after_weekly is None:
                reasons.append("weekly_capacity")
            if not after_resource["feasible"]:
                reasons.append("resource_conflict")
            if reasons:
                lost.append({"kind": terms["option_id"], "deadline": terms["deadline"],
                             "deliverables": terms["deliverables"], "price": terms["price_sgd"],
                             "reasons": reasons,
                             "blocked_task_id": after_resource.get("blocked_task_id")})
            else:
                remaining += 1
        if lost:
            summary["affected"].append({"client": row["client"], "version_id": row["version_id"],
                                        "offer_id": row["offer_id"], "lost": lost, "remaining": remaining})
    return summary
