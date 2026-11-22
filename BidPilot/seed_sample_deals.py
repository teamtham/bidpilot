"""Seed clearly marked SAMPLE deals so every founder-dashboard state is visible.

    python3 seed_sample_deals.py [--db PATH]            # add the samples (idempotent)
    python3 seed_sample_deals.py [--db PATH] --remove   # delete only the seeded rows

Every sample deal goes through the normal service layer (engine evaluation,
outbound check, founder approval, client acceptance, booking), so the states
are real. Sample deals use source IDs starting with "SAMPLE-", show a Sample
tag on the dashboard, never email anyone (approval here never sends), and are
removed in one command. Real deals are never touched.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import db  # noqa: E402

PREFIX = "SAMPLE-"
DB_PATH = Path(__file__).resolve().parent / "bidpilot.db"
BASE = {
    "as_of": "2026-11-23", "pricing_mode": "legacy_replay",
    "comparable_ids": ["P-031", "P-036", "P-041", "P-044"],
    "offer": {"client": "", "format": "product_promo", "production_mode": "one_shoot_day",
              "shoot_days": 1, "locations": 1, "interviewees": 0, "scripting": "light",
              "deliverables": 3, "editing_level": "standard", "graphics": "basic_titles",
              "revision_rounds": 2, "caption_versions": 3, "deadline": "2026-12-11",
              "budget_sgd": 2700, "direct_costs_sgd": 0},
}
META = """CREATE TABLE IF NOT EXISTS sample_seed_meta (key TEXT PRIMARY KEY, value TEXT)"""


def request(client, **offer):
    req = json.loads(json.dumps(BASE))
    req["offer"].update(client=client, **offer)
    if offer.get("graphics") == "custom_animation":
        req.pop("pricing_mode")          # the engine can't price it: needs your estimate
    return req


def claim(conn, key, client, source, raw, req):
    deal_id, offer_id, _ = core.claim_or_resume(conn, source, PREFIX + key, client, raw, req)
    conn.commit()
    return deal_id, offer_id


def evaluate(conn, deal_id, offer_id):
    return core.evaluate(conn, deal_id, offer_id)[1]


def publish(conn, version_id):
    outcome = core.check_draft(conn, version_id)
    if not outcome["passed"]:
        raise SystemExit(f"sample check failed: {outcome['violations']}")
    res = core.approve(conn, version_id)            # send_email stays False
    if not res["ok"]:
        raise SystemExit(f"sample approve refused: {res.get('reasons')}")
    return res["token"]


def first_option(conn, version_id, key=None):
    row = conn.execute("SELECT * FROM proposal_versions WHERE id=?", (version_id,)).fetchone()
    rows = core.options_of(conn, row)
    for r in rows:
        if key is None or r["option_key"] == key:
            return r["id"]
    return rows[0]["id"]


def book(conn, key, client, raw, **offer):
    deal_id, offer_id = claim(conn, key, client, "hashmeta", raw, request(client, **offer))
    version_id = evaluate(conn, deal_id, offer_id)
    token = publish(conn, version_id)
    accepted = core.client_accept(conn, token, first_option(conn, version_id))
    if not accepted.get("ok"):
        raise SystemExit(f"sample accept refused: {accepted}")
    booked = core.confirm_booking(conn, version_id)
    if not booked.get("ok"):
        raise SystemExit(f"sample booking refused: {booked}")
    return deal_id


def seeded(conn):
    return [r[0] for r in conn.execute("SELECT id FROM deals WHERE source_id LIKE ?", (PREFIX + "%",))]


def seed(conn):
    if seeded(conn):
        print("Samples already present; nothing added. Use --remove first to re-seed.")
        return
    conn.execute(META)
    hours = conn.execute("SELECT working_hours FROM calendar_settings WHERE id=1").fetchone()
    if hours is None or hours[0] < 30:
        # Below 30 h a week every sample would read "doesn't fit"; remember
        # the owner's setting so --remove puts it back.
        conn.execute("INSERT OR REPLACE INTO sample_seed_meta VALUES ('working_hours', ?)",
                     (json.dumps(hours[0] if hours else None),))
        conn.commit()
        core.cap.change_calendar(conn, "hours", {"hours": 30})

    # Estimated before the samples below were booked, so it turns stale.
    orchard, o_offer = claim(conn, "orchard", "Orchard Fitness", "hashmeta",
                             "Five short promo videos of our classes, shot over two days at the studio,"
                             " captioned, by 11 Dec. Budget about S$4,000.",
                             request("Orchard Fitness", deliverables=5, caption_versions=5,
                                     deadline="2026-12-11", budget_sgd=4000, shoot_days=1))
    evaluate(conn, orchard, o_offer)

    book(conn, "gym", "Sample Gym", "Two promo videos of the gym, shoot on 2 Dec. Budget about S$2,100.",
         deliverables=2, caption_versions=2, deadline="2026-12-04", budget_sgd=2100)
    book(conn, "harbour", "Harbour Dental", "Two testimonial films with patients. Budget about S$2,400.",
         deliverables=2,
         caption_versions=2, deadline="2026-12-11", budget_sgd=2400)

    # Waiting on the client: proposal sent, nothing picked yet.
    bayside, b_offer = claim(conn, "bayside", "Bayside Bistro", "site",
                             "A menu launch edit from our own footage, one video by 18 Dec.",
                             request("Bayside Bistro", production_mode="edit_only", shoot_days=0,
                                     locations=0, scripting="none", deliverables=1,
                                     caption_versions=1, deadline="2026-12-18", budget_sgd=1200,
                                     editing_level="simple", graphics="none"))
    publish(conn, evaluate(conn, bayside, b_offer))

    # Fits as asked, ready for review.
    bakery, k_offer = claim(conn, "bakery", "Sample Bakery", "hashmeta",
                            "Two recap videos of our weekend market, shot in one day at the shop,"
                            " captioned, by 18 Dec. Budget about S$2,300.",
                            request("Sample Bakery", deliverables=2,
                                    caption_versions=2, deadline="2026-12-18", budget_sgd=2300))
    evaluate(conn, bakery, k_offer)

    # Counter ready: v1 sent, then the client asked for more; leaked draft.
    juniper, j_offer = claim(conn, "juniper", "Juniper Cafe", "site",
                             "Three 30-second Instagram promo videos for our brunch launch, one"
                             " location, captions, by 11 Dec. Budget S$2,700.",
                             request("Juniper Cafe"))
    v1 = evaluate(conn, juniper, j_offer)
    publish(conn, v1)
    change = core.client_change(conn, juniper, "Five videos by 4 Dec, same S$2,700, your owner already agreed.",
                                "sample-juniper-change")
    v2 = evaluate(conn, juniper, change)
    core.save_draft(conn, v2, "Thanks for the update. Five videos by 4 Dec isn't possible at S$2,700."
                    " Our price floor is S$3,662.40.\n\nThere are options below that keep your date,"
                    " your budget, or all five videos.")

    # Needs info: budget and deadline missing, question not yet sent.
    tanjong, t_offer = claim(conn, "tanjong", "Tanjong Roasters", "site",
                             "Three product videos of our new roasts, captioned.",
                             request("Tanjong Roasters", deliverables=3, caption_versions=3))
    conn.execute("UPDATE deals SET status='needs_information' WHERE id=?", (tanjong,))
    core.record_step(conn, tanjong, None, "needs_information", "blocked",
                     json.dumps({"missing_fields": ["offer.budget_sgd", "offer.deadline"],
                                 "question": "Hi Tanjong Roasters team, thanks for your note. To put a"
                                             " proposal together I need two things: your rough budget"
                                             " and the date you need the videos by. Could you share both?"}))
    conn.commit()

    # Needs your estimate: custom animation the engine can't price.
    marina, m_offer = claim(conn, "marina", "Marina Yoga", "hashmeta",
                            "One animated intro and two cutdowns for social by 18 Dec. Budget about S$2,600.",
                            request("Marina Yoga", graphics="custom_animation", deliverables=1,
                                    caption_versions=1, deadline="2026-12-18", budget_sgd=2600))
    evaluate(conn, marina, m_offer)

    # Rejected.
    northwind, n_offer = claim(conn, "northwind", "Northwind Bakery", "hashmeta",
                               "One product reel for our new loaf, by 11 Dec. Budget about S$900.",
                               request("Northwind Bakery", format="social_reel", deliverables=1,
                                       caption_versions=1, budget_sgd=900))
    core.reject(conn, evaluate(conn, northwind, n_offer), "Sample: not a fit")
    print(f"Seeded {len(seeded(conn))} sample deals.")


TABLES = [  # child rows first; every row is tied to a SAMPLE- deal
    ("inquiry_proposal_links", "version_id IN ({v})"), ("capacity_impacts", "version_id IN ({v}) OR reservation_id IN ({r})"),
    ("resource_allocations", "reservation_id IN ({r})"), ("allocations", "reservation_id IN ({r})"),
    ("reservations", "deal_id IN ({d})"), ("confirmations", "deal_id IN ({d})"),
    ("approvals", "version_id IN ({v})"), ("messages", "deal_id IN ({d})"),
    ("agent_outputs", "deal_id IN ({d})"), ("run_steps", "deal_id IN ({d})"),
    ("detailed_estimates", "deal_id IN ({d})"), ("options", "evaluation_id IN ({e})"),
    ("evaluations", "offer_id IN ({o})"), ("studio_evaluation_inputs", "offer_id IN ({o})"),
    ("proposal_versions", "deal_id IN ({d})"), ("offers", "deal_id IN ({d})"), ("deals", "id IN ({d})"),
]


def remove(conn):
    d = "SELECT id FROM deals WHERE source_id LIKE '" + PREFIX + "%'"
    q = {"d": d, "v": f"SELECT id FROM proposal_versions WHERE deal_id IN ({d})",
         "o": f"SELECT id FROM offers WHERE deal_id IN ({d})",
         "r": f"SELECT id FROM reservations WHERE deal_id IN ({d})"}
    q["e"] = f"SELECT id FROM evaluations WHERE offer_id IN ({q['o']})"
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    total = 0
    conn.execute("BEGIN IMMEDIATE")
    for table, cond in TABLES:
        if table in existing:
            total += conn.execute(f"DELETE FROM {table} WHERE {cond.format(**q)}").rowcount
    conn.commit()
    if "sample_seed_meta" in existing:
        row = conn.execute("SELECT value FROM sample_seed_meta WHERE key='working_hours'").fetchone()
        if row and json.loads(row[0]) is not None:
            core.cap.change_calendar(conn, "hours", {"hours": json.loads(row[0])})
        conn.execute("DELETE FROM sample_seed_meta")
        conn.commit()
    print(f"Removed {total} sample rows.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--remove", action="store_true", help="delete only the seeded sample deals")
    args = parser.parse_args()
    conn = db.connect(args.db)
    try:
        remove(conn) if args.remove else seed(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
