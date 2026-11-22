"""Founder price list: hours and price for each kind of work.

Standard jobs are priced by the engine. Custom jobs the engine can't price
(e.g. custom animation) are built from this list: the founder picks
quantities, and the totals go through the existing manual-estimate path, so
the same calendar fit, outbound check and approval gate apply.
"""
import hashlib
import json
import math

import db

DEFAULTS = (
    ("promo", "Promo video, shot and edited", "30 s, shoot day shared", 10.5, 800),
    ("edit", "Edit-only video", "30 s, from your footage", 4, 350),
    ("shoot", "Extra shoot day", "per day", 6, 600),
    ("anim", "Custom animation", "per 10 s", 8, 700),
    ("cap", "Captioned version", "per video", 0.5, 40),
    ("rev", "Extra revision round", "per round", 2, 150),
)


def items(conn):
    rows = conn.execute("SELECT * FROM price_list_items ORDER BY sort").fetchall()
    if not rows:
        now = db.now()
        for i, (key, name, unit, hours, price) in enumerate(DEFAULTS):
            conn.execute("INSERT OR IGNORE INTO price_list_items VALUES (?,?,?,?,?,?,?)",
                         (key, name, unit, hours, price, i, now))
        conn.commit()
        rows = conn.execute("SELECT * FROM price_list_items ORDER BY sort").fetchall()
    return [{"id": r["id"], "name": r["name"], "unit": r["unit"], "hours": r["hours"],
             "price_sgd": r["price_sgd"]} for r in rows]


def _number(value, field, upper):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < 0 or value > upper:
        raise ValueError(f"Enter a {field} between 0 and {upper:g}.")
    return float(value)


def save(conn, data):
    """Update hours and prices of existing lines. Sent proposals keep their price."""
    rows = data.get("items")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Nothing to save.")
    known = {r["id"] for r in items(conn)}
    now = db.now()
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in known:
            raise ValueError("Unknown price list line.")
        conn.execute("UPDATE price_list_items SET hours=?, price_sgd=?, updated_at=? WHERE id=?",
                     (_number(row.get("hours"), "number of hours", 200),
                      _number(row.get("price_sgd"), "price", 100000), now, row["id"]))
    conn.commit()
    return items(conn)


def totals(conn, quantities):
    lines = {r["id"]: r for r in items(conn)}
    hours = price = 0.0
    if not isinstance(quantities, list):
        raise ValueError("Choose quantities from your price list.")
    for q in quantities:
        if not isinstance(q, dict) or q.get("id") not in lines:
            raise ValueError("Unknown price list line.")
        n = _number(q.get("qty"), "quantity", 100)
        hours += n * lines[q["id"]]["hours"]
        price += n * lines[q["id"]]["price_sgd"]
    if hours <= 0 or price <= 0:
        raise ValueError("Add at least one item from your price list.")
    return round(hours, 2), round(price, 2)


def estimate(conn, data):
    """Run the manual-estimate path with totals from the price list."""
    import core
    hours, price = totals(conn, data.get("items"))
    # The detailed estimator needs a dated catalog rate; derive one from this
    # price list (same rate for the one work line) so nothing is invented.
    sell = math.floor(price * 100 / hours)
    rates = [{"role": "producer", "cost_rate_cents": 0, "sell_rate_cents": sell}]
    version = "price-list-" + hashlib.sha256(json.dumps(rates).encode()).hexdigest()[:10]
    if not conn.execute("SELECT 1 FROM estimate_rate_versions WHERE version=?", (version,)).fetchone():
        conn.execute("INSERT INTO estimate_rate_versions VALUES (?,?,?)", (version, db.j(rates), db.now()))
    _ensure_weekday_availability(conn, data.get("deal_id"))
    result = core.enter_manual_estimate(conn, {"deal_id": data.get("deal_id"),
                                               "hours_needed": hours, "list_price": price,
                                               "rate_version": version})
    return {"hours": hours, "price_sgd": price, **result}


def _ensure_weekday_availability(conn, deal_id):
    """Give the producer weekday availability from the founder's own weekly
    working hours (split over Mon-Fri, from 09:00) between the offer's
    planning date and deadline, where none was entered. Weekly capacity,
    blocks and bookings are still checked by the estimate itself."""
    from datetime import date, timedelta
    offer = conn.execute("SELECT evidence_json FROM offers WHERE deal_id=? ORDER BY round DESC LIMIT 1",
                         (deal_id,)).fetchone()
    if offer is None:
        return
    request = json.loads(offer["evidence_json"])
    try:
        start = date.fromisoformat(request["as_of"])
        end = date.fromisoformat(request["offer"]["deadline"])
    except (KeyError, TypeError, ValueError):
        return
    setting = conn.execute("SELECT working_hours FROM calendar_settings WHERE id=1").fetchone()
    weekly = float(setting[0]) if setting else 40.0
    minutes = int(round(weekly * 60 / 5))
    day = start
    while day <= end:
        if day.weekday() < 5:
            begin = f"{day.isoformat()}T09:00:00"
            stop_at = (datetime_at(day) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            if not conn.execute("SELECT 1 FROM resource_availability WHERE resource='producer'"
                                " AND start_at < ? AND end_at > ?", (stop_at, begin)).fetchone():
                conn.execute("INSERT INTO resource_availability VALUES (?,?,?,?)",
                             (db.new_id(), "producer", begin, stop_at))
        day += timedelta(days=1)


def datetime_at(day):
    from datetime import datetime
    return datetime(day.year, day.month, day.day, 9, 0, 0)
