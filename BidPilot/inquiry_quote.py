"""Bind one commercial evaluation to the exact inquiry brief that it priced."""
import db


SCHEMA = """
CREATE TABLE IF NOT EXISTS priced_briefs (
 offer_id TEXT PRIMARY KEY, inquiry_id TEXT NOT NULL, revision INTEGER NOT NULL,
 input_hash TEXT NOT NULL, UNIQUE(inquiry_id,revision));
CREATE TABLE IF NOT EXISTS offer_briefs (
 offer_id TEXT PRIMARY KEY, inquiry_id TEXT NOT NULL, revision INTEGER NOT NULL);
"""


def scope_error(conn, version):
    if version["status"] == "confirmed":
        return None
    inquiry = conn.execute(
        "SELECT i.*,coalesce(p.revision,b.revision) AS priced_revision FROM inquiries i "
        "LEFT JOIN priced_briefs p ON p.inquiry_id=i.id AND p.offer_id=? "
        "LEFT JOIN offer_briefs b ON b.inquiry_id=i.id AND b.offer_id=? WHERE i.deal_id=?",
        (version["offer_id"], version["offer_id"], version["deal_id"])).fetchone()
    if not inquiry:
        return None
    if inquiry["revision"] != inquiry["priced_revision"]:
        return "scope_revision_changed"
    pending = conn.execute("SELECT 1 FROM inquiry_turns WHERE inquiry_id=? AND sender='buyer' "
                           "AND seq>? LIMIT 1", (inquiry["id"], inquiry["processed_seq"])).fetchone()
    return "buyer_reply_pending" if pending else None


def price(conn, args):
    import core
    import hashlib

    inquiry_id = args["inquiry_id"]
    revision = args["expected_revision"]
    request = dict(args["offer_request"])
    request.setdefault("historical_pricing", True)
    digest = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        inquiry = conn.execute("SELECT * FROM inquiries WHERE id=? AND revoked=0", (inquiry_id,)).fetchone()
        if not inquiry or inquiry["revision"] != revision:
            raise ValueError("stale_revision")
        if conn.execute("SELECT 1 FROM inquiry_turns WHERE inquiry_id=? AND sender='buyer' AND seq>?",
                        (inquiry_id, inquiry["processed_seq"])).fetchone():
            raise ValueError("buyer_reply_pending")
        if conn.execute('SELECT 1 FROM gemini_chats WHERE inquiry_id=?',(inquiry_id,)).fetchone():
            # The structured quote form is itself a submission. Opening its chat
            # receipt must not require the buyer to submit the identical brief again.
            original_form = revision == 0 and inquiry['processed_seq'] == 1 and conn.execute(
                'SELECT 1 FROM inquiry_receipts WHERE inquiry_id=?', (inquiry_id,)).fetchone()
            if not original_form and not conn.execute('SELECT 1 FROM studio_submissions WHERE inquiry_id=? AND revision=? AND sequence=?',(inquiry_id,revision,inquiry['processed_seq'])).fetchone():
                raise ValueError('brief_not_submitted')
        import json
        facts = {}
        for fact in conn.execute('SELECT * FROM inquiry_facts WHERE inquiry_id=? ORDER BY revision,created_at,rowid', (inquiry_id,)):
            if fact['status'] == 'confirmed':
                facts[fact['subject']] = json.loads(fact['value_json'])
            else:
                facts.pop(fact['subject'], None)
        confirmed = {k: v for k, v in facts.items() if k.startswith('production.')}
        if confirmed:
            supplied = request.get('production_facts', {})
            if any(k in supplied and supplied[k] != v for k, v in confirmed.items()):
                raise ValueError('confirmed_fact_conflict')
            request['production_facts'] = {**supplied, **confirmed}
        digest = hashlib.sha256(db.j(request).encode()).hexdigest()
        errors = core.validate_offer_request(request)
        if errors:
            raise ValueError("invalid_scope:" + ",".join(key for _, key in errors))
        if request.get("detailed_spec") is not None:
            import estimating
            estimating.validate_evidence_links(conn, request["detailed_spec"], inquiry_id)
            for variant in request.get("detailed_alternatives", []):
                if isinstance(variant, dict) and isinstance(variant.get("spec"), dict):
                    estimating.validate_evidence_links(conn, variant["spec"], inquiry_id)
        prior = conn.execute("SELECT * FROM priced_briefs WHERE inquiry_id=? AND revision=?",
                             (inquiry_id, revision)).fetchone()
        if prior and digest != prior["input_hash"]:
            raise ValueError("priced_revision_conflict")
        deal_id = inquiry["deal_id"]
        if deal_id:
            deal = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
            if not deal or not deal["sample_data"] or deal["status"] in ("confirmed", "closed"):
                raise ValueError("use_booking_amendment")
        if prior:
            offer_id = core.studio_resume_offer(conn, prior["offer_id"])
            if offer_id != prior['offer_id']:
                inherit_snapshot(conn, prior['offer_id'], offer_id)
                conn.execute('UPDATE priced_briefs SET offer_id=? WHERE inquiry_id=? AND revision=?',
                             (offer_id, inquiry_id, revision))
        elif not deal_id:
            import deal_logic
            if deal_logic.looks_like_description(request["offer"].get("client")):
                # The deal needs the buyer's business or name, not a project label.
                raise ValueError("client_name_required: ask the buyer for their business or name")
            first = conn.execute("SELECT body FROM inquiry_turns WHERE inquiry_id=? AND sender='buyer' "
                                 "ORDER BY seq LIMIT 1", (inquiry_id,)).fetchone()
            receipt = conn.execute('SELECT source_id FROM inquiry_receipts WHERE inquiry_id=?', (inquiry_id,)).fetchone()
            source_id = receipt['source_id'] if receipt else 'inquiry-'+inquiry_id
            deal_id, offer_id, _ = core.claim_or_resume(conn, "site", source_id,
                    request["offer"]["client"], first[0] if first else "", request, require_sample=True, commit=False)
            conn.execute("UPDATE inquiries SET deal_id=? WHERE id=?", (deal_id,inquiry_id))
        else:
            offer_id = core._insert_intake_offer(conn, deal_id, "conversation", inquiry_id+":"+str(revision),
                                                request, db.now())
        if not prior:
            conn.execute("INSERT INTO priced_briefs VALUES (?,?,?,?)", (offer_id,inquiry_id,revision,digest))
        import delivery
        delivery.bind_recipient(conn, inquiry_id, deal_id)
        evaluation_id, version_id = core.evaluate(conn, deal_id, offer_id, commit=False)
        conn.commit()
        return {"deal_id":deal_id, "offer_id":offer_id, "evaluation_id":evaluation_id,
                "version_id":version_id, "scope_revision":revision}
    except Exception:
        conn.rollback()
        raise


MCP = {"price_inquiry_scope": ({"inquiry_id":{"type":"string"}, "expected_revision":{"type":"integer"},
                               "offer_request":{"type":"object"}},
                              ["inquiry_id","expected_revision","offer_request"], price)}


def inherit_snapshot(conn, previous_offer, new_offer):
    """A new quote of an existing scope retains its original conversation revision."""
    conn.execute("INSERT OR IGNORE INTO offer_briefs SELECT ?,inquiry_id,revision FROM "
                 "(SELECT inquiry_id,revision FROM priced_briefs WHERE offer_id=? UNION ALL "
                 " SELECT inquiry_id,revision FROM offer_briefs WHERE offer_id=?) LIMIT 1",
                 (new_offer, previous_offer, previous_offer))
