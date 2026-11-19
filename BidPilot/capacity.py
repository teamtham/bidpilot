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
    weeks = baseline_weeks(data or _seed())
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
