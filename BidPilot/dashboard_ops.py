"""Owner-only, transactional dashboard import and sample reset operations."""
import hashlib
import json

import core
import db
import capacity as cap


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def import_run(conn, bundle):
    if not isinstance(bundle, dict):
        raise ValueError('invalid_bundle')
    source, source_id = bundle.get('source'), bundle.get('source_id')
    request, draft, keys, steps = (bundle.get('offer_request'), bundle.get('draft'),
                                   bundle.get('option_ids'), bundle.get('steps'))
    if not all(isinstance(x, str) and x.strip() for x in (source, source_id, draft)):
        raise ValueError('missing_source_source_id_or_draft')
    if source not in ('site', 'hashmeta', 'manual'):
        # Same allowlist as MCP and HTTP import: internal offer namespaces
        # (change:<request_id>) must never alias another deal's offer rows.
        raise ValueError('invalid_source')
    if core.validate_offer_request(request):
        raise ValueError('invalid_offer_request')
    if (not isinstance(keys, list) or any(not isinstance(k, str) or not k for k in keys)
            or len(set(keys)) != len(keys)):
        raise ValueError('invalid_option_ids')
    if not isinstance(steps, list) or any(not isinstance(s, dict) for s in steps):
        raise ValueError('invalid_steps')
    for step in steps:
        if (not isinstance(step.get('step_key'), str) or not isinstance(step.get('status'), str)
                or step['step_key'] not in core.STEP_KEYS or step['status'] not in core.STEP_STATUSES):
            raise ValueError('invalid_step_key_or_status')
        if step.get('private_note') is not None and not isinstance(step['private_note'], str):
            raise ValueError('invalid_private_note')
    if 'deal_id' in bundle and bundle['deal_id'] is not None and not isinstance(bundle['deal_id'], str):
        raise ValueError('invalid_deal_id')
    if 'raw_inquiry' in bundle and not isinstance(bundle['raw_inquiry'], str):
        raise ValueError('invalid_raw_inquiry')
    payload_hash = _hash({'tool': 'import_run', 'bundle': bundle})
    request_id = bundle.get('request_id')
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        raise ValueError('invalid_request_id')
    request_id = request_id.strip() if request_id is not None else 'import-run:' + payload_hash
    try:
        conn.execute('BEGIN IMMEDIATE')
        op = conn.execute('SELECT * FROM mcp_operations WHERE request_id=?', (request_id,)).fetchone()
        if op:
            if op['tool'] != 'import_run' or op['payload_hash'] != payload_hash:
                raise ValueError('request_id_conflict')
            result = db.uj(op['result_json'])
            conn.rollback()
            return result
        existing = conn.execute('SELECT * FROM deals WHERE source=? AND source_id=?',
                                (source, source_id)).fetchone()
        if existing and not existing['sample_data']:
            raise ValueError('non_sample_record_rejected')
        if bundle.get('deal_id') and (not existing or existing['id'] != bundle['deal_id']):
            raise ValueError('deal_id_mismatch')
        if existing:
            old = conn.execute('SELECT evidence_json FROM offers WHERE source=? AND source_id=?',
                               (source, source_id)).fetchone()
            if old and db.uj(old['evidence_json']) != request:
                raise ValueError('offer_request_mismatch')
            if existing['status'] in ('confirmed', 'closed'):
                raise ValueError('record_not_editable')
        client = request['offer'].get('client', '')
        deal_id, offer_id, _ = core.claim_or_resume(conn, source, source_id, client,
                                                    bundle.get('raw_inquiry', ''), request,
                                                    commit=False)
        if conn.execute('SELECT 1 FROM evaluations WHERE offer_id=?', (offer_id,)).fetchone():
            offer_id = core.reevaluate(conn, offer_id, commit=False)
        eval_id, initial_version = core.evaluate(conn, deal_id, offer_id, commit=False)
        rows = conn.execute('SELECT id, option_key FROM options WHERE evaluation_id=? ORDER BY rowid',
                            (eval_id,)).fetchall()
        by_key = {row['option_key']: row['id'] for row in rows}
        if any(k not in by_key for k in keys):
            raise ValueError('option_id_not_in_evaluation')
        version_id = core.save_draft(conn, initial_version, draft, commit=False)
        conn.execute('UPDATE proposal_versions SET public_option_ids_json=?, import_option_keys_json=? WHERE id=?',
                     (db.j([by_key[k] for k in keys]), db.j(keys), version_id))
        outcome = core.check_draft(conn, version_id, commit=False)
        actual = db.uj(conn.execute('SELECT result_json FROM evaluations WHERE id=?',
                                    (eval_id,)).fetchone()['result_json'])
        eval_mismatch = 'evaluation' in bundle and bundle['evaluation'] != actual
        out_mismatch = 'outbound_result' in bundle and bundle['outbound_result'] != outcome
        comparison = {k: bundle[k] for k in ('evaluation', 'outbound_result') if k in bundle}
        conn.execute('INSERT INTO agent_outputs (id,deal_id,offer_id,kind,payload_json,request_id,created_at) '
                     'VALUES (?,?,?,?,?,?,?)', (db.new_id(), deal_id, offer_id, 'imported_run',
                     db.j(comparison), 'import-output:' + request_id, db.now()))
        for step in steps:
            core.record_step(conn, deal_id, offer_id, step['step_key'], step['status'],
                             step.get('private_note'))
        result = {'deal_id': deal_id, 'offer_id': offer_id, 'evaluation_id': eval_id,
                  'version_id': version_id, 'outbound_result': outcome,
                  'evaluation_mismatch': eval_mismatch, 'outbound_result_mismatch': out_mismatch,
                  'mismatch': eval_mismatch or out_mismatch}
        conn.execute('INSERT INTO mcp_operations (request_id,tool,payload_hash,result_json,created_at,deal_id) '
                     'VALUES (?,?,?,?,?,?)', (request_id, 'import_run', payload_hash, db.j(result), db.now(), deal_id))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def reset_sample(conn, data):
    if not isinstance(data, dict) or data.get('confirm') != 'RESET SAMPLE DATA':
        raise ValueError('confirmation_required')
    request_id = data.get('request_id')
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        raise ValueError('invalid_request_id')
    if request_id is not None:
        request_id = request_id.strip()
    payload_hash = _hash({'tool': 'reset_sample', 'data': data})
    try:
        conn.execute('BEGIN IMMEDIATE')
        if request_id:
            op = conn.execute('SELECT * FROM mcp_operations WHERE request_id=?', (request_id,)).fetchone()
            if op:
                if op['tool'] != 'reset_sample' or op['payload_hash'] != payload_hash:
                    raise ValueError('request_id_conflict')
                result = db.uj(op['result_json'])
                conn.rollback()
                return result
        sample = 'SELECT id FROM deals WHERE sample_data=1'
        versions = f'SELECT id FROM proposal_versions WHERE deal_id IN ({sample})'
        offers = f'SELECT id FROM offers WHERE deal_id IN ({sample})'
        evaluations = f'SELECT id FROM evaluations WHERE offer_id IN ({offers})'
        reservations = f'SELECT id FROM reservations WHERE deal_id IN ({sample})'
        import conversation
        conversation.reset_sample(conn)
        conditions = [
            ('inquiry_proposal_links', f'version_id IN ({versions})'),
            ('capacity_impacts', f'version_id IN ({versions}) OR reservation_id IN ({reservations})'),
            ('resource_allocations', f'reservation_id IN ({reservations})'),
            ('priced_briefs', f'offer_id IN ({offers})'),
            ('offer_briefs', f'offer_id IN ({offers})'),
            ('commerce_reference_requests', f'request_id IN (SELECT request_id FROM messages WHERE deal_id IN ({sample}))'),
            ('priced_request_orders', f'order_id IN (SELECT id FROM change_orders WHERE deal_id IN ({sample}))'),
            ('detailed_estimates', f'deal_id IN ({sample})'),
            ('closeouts', f'deal_id IN ({sample})'),
            ('change_orders', f'deal_id IN ({sample})'),
            ('allocations', f'reservation_id IN ({reservations})'),
            ('reservations', f'deal_id IN ({sample})'),
            ('confirmations', f'deal_id IN ({sample})'),
            ('approvals', f'version_id IN ({versions})'),
            ('messages', f'deal_id IN ({sample})'),
            ('agent_outputs', f'deal_id IN ({sample})'),
            ('run_steps', f'deal_id IN ({sample})'),
            ('options', f'evaluation_id IN ({evaluations})'),
            ('evaluations', f'offer_id IN ({offers})'),
            ('proposal_versions', f'deal_id IN ({sample})'),
            ('offers', f'deal_id IN ({sample})'),
            ('inbox', 'sample_data=1'),
        ]
        counts = {}
        active_removed = conn.execute(f"SELECT COUNT(*) FROM reservations WHERE deal_id IN ({sample}) AND status='active'").fetchone()[0]
        # Quote receipts have no deal owner; associate them only through a
        # source ID that still identifies an explicitly sample inbox row.
        sample_inbox = {r[0] for r in conn.execute('SELECT source_id FROM inbox WHERE sample_data=1')}
        quote_ids = []
        for row in conn.execute("SELECT request_id,result_json FROM mcp_operations WHERE tool='quote'"):
            try:
                receipt = db.uj(row['result_json'])
            except (ValueError, TypeError):
                continue
            if (isinstance(receipt, dict) and isinstance(receipt.get('source_id'), str)
                    and receipt['source_id'] in sample_inbox):
                quote_ids.append(row['request_id'])
        for quote_id in quote_ids:
            conn.execute('DELETE FROM mcp_operations WHERE request_id=?', (quote_id,))
        legacy_ids = []
        for row in conn.execute("SELECT request_id,result_json FROM mcp_operations WHERE tool='save_draft'"):
            try:
                version = db.uj(row['result_json']).get('draft_version', {})
                version_id, deal_id = version.get('version_id'), version.get('deal_id')
            except (ValueError, TypeError, AttributeError):
                continue
            if conn.execute(f'SELECT 1 FROM proposal_versions WHERE id=? AND deal_id=? AND deal_id IN ({sample})',
                            (version_id, deal_id)).fetchone():
                legacy_ids.append(row['request_id'])
        for legacy_id in legacy_ids:
            conn.execute('DELETE FROM mcp_operations WHERE request_id=?', (legacy_id,))
        conditions.insert(0, ('mcp_operations', f'deal_id IN ({sample})'))
        for table, where in conditions:
            counts[table] = conn.execute(f'DELETE FROM {table} WHERE {where}').rowcount
        counts['mcp_operations'] = counts.get('mcp_operations', 0) + len(quote_ids) + len(legacy_ids)
        counts['deals'] = conn.execute('DELETE FROM deals WHERE sample_data=1').rowcount
        core.load_seed(conn, commit=False)
        counts['seed_tables'] = {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                                 for table in ('seed_business_rules', 'seed_effort_rules', 'seed_rate_card',
                                               'seed_past_projects', 'seed_calendar_weeks')}
        if active_removed:
            revision = cap.ledger_revision(conn) + 1
            weeks = cap.ledger(conn)
            snapshot = {'revision': revision, 'reason': 'sample_reset',
                        'weeks': [{'week': w, **values} for w, values in sorted(weeks.items())]}
            conn.execute('INSERT INTO capacity_revisions (revision,seed_hash,snapshot_json,created_at) VALUES (?,?,?,?)',
                         (revision, core.seed_hash(core.seed_data()), db.j(snapshot), db.now()))
        result = {'counts': counts}
        if request_id:
            conn.execute('INSERT INTO mcp_operations (request_id,tool,payload_hash,result_json,created_at) VALUES (?,?,?,?,?)',
                         (request_id, 'reset_sample', payload_hash, db.j(result), db.now()))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
