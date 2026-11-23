"""Versioned, deterministic estimate and resource scheduling primitives.

All detailed-estimate amounts are integer SGD cents.  Legacy engine arithmetic
and fixtures are intentionally untouched. Callers own transactions around writes.
"""
import copy
import json
import math
import statistics
import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import db

SCHEMA = '''
CREATE TABLE IF NOT EXISTS studio_evaluation_inputs (offer_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS studio_resumes (original_offer_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 offer_id TEXT NOT NULL, PRIMARY KEY(original_offer_id,fingerprint));
CREATE TABLE IF NOT EXISTS detailed_estimates (
 id TEXT PRIMARY KEY, deal_id TEXT NOT NULL, scope_revision_id TEXT NOT NULL,
 rate_version TEXT NOT NULL, input_json TEXT NOT NULL, result_json TEXT NOT NULL,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS founder_past_projects (
 project_id TEXT PRIMARY KEY, project_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS estimate_rate_versions (
 version TEXT PRIMARY KEY, rates_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS resource_availability (
 id TEXT PRIMARY KEY, resource TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_allocations (
 id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL, resource TEXT NOT NULL,
 start_at TEXT NOT NULL, end_at TEXT NOT NULL, task_id TEXT NOT NULL,
 UNIQUE(reservation_id, resource, start_at, end_at));
CREATE TABLE IF NOT EXISTS producer_overrides (
 id TEXT PRIMARY KEY, deal_id TEXT NOT NULL, scope_revision_id TEXT NOT NULL,
 item_id TEXT NOT NULL, field TEXT NOT NULL, value_json TEXT NOT NULL,
 basis_date TEXT NOT NULL, note TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
 supersedes TEXT, created_at REAL NOT NULL);
'''


def studio_fingerprint(conn, request):
    """Fingerprint the selected basis and intersecting resource windows, not IDs."""
    import hashlib
    if request.get('detailed_spec') is None:
        return None   # past-jobs pricing has no studio-basis fingerprint
    try:
        spec = request.get('detailed_spec') or promo_spec(conn, request)
    except (ValueError, KeyError, TypeError) as exc:
        return hashlib.sha256(db.j({'missing': str(exc)}).encode()).hexdigest()
    specs = [spec] + [v['spec'] for v in request.get('detailed_alternatives', [])
                      if isinstance(v, dict) and isinstance(v.get('spec'), dict)]
    windows = [(resource, t['release_at'], t['deadline']) for s in specs
               for t in s.get('tasks', []) for resource in t.get('resources', [])]
    inputs = {'specs': specs, 'rates': [], 'availability': [], 'allocations': []}
    for version in sorted({s['rate_version'] for s in specs}):
        row = conn.execute('SELECT rates_json FROM estimate_rate_versions WHERE version=?', (version,)).fetchone()
        inputs['rates'].append([version, json.loads(row[0]) if row else None])
    for table, key in [('resource_availability', 'availability'), ('resource_allocations', 'allocations')]:
        intervals = set()
        for r in conn.execute('SELECT resource,start_at,end_at FROM ' + table):
            for resource, start, end in windows:
                if r['resource'] == resource and r['start_at'] < end and r['end_at'] > start:
                    intervals.add((resource, max(start, r['start_at']), min(end, r['end_at'])))
        inputs[key] = sorted(intervals)
    return hashlib.sha256(db.j(inputs).encode()).hexdigest()


def _money(value):
    return int(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _nonnegative(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid_' + name)
    return value


def get_history(conn):
    """Seed project-level evidence plus completed closeouts, when available."""
    records = [dict(r) for r in conn.execute('SELECT * FROM seed_past_projects')]
    records.extend(json.loads(r['project_json']) for r in conn.execute('SELECT project_json FROM founder_past_projects'))
    try:
        import commerce
        extra = commerce.get_history(conn)
    except (ImportError, AttributeError, sqlite3.OperationalError):
        extra = []
    known = {r['project_id'] for r in records}
    for row in extra:
        if row.get('project_id') and row['project_id'] not in known:
            records.append(dict(row))
            known.add(row['project_id'])
    return records


def add_past_project(conn, project):
    import core
    if not isinstance(project, dict):
        raise ValueError('invalid_project')
    required = ('client', 'format', 'production_mode', 'shoot_days', 'locations', 'scripting',
                'deliverables', 'editing_level', 'graphics', 'revision_rounds', 'caption_versions',
                'price_sgd', 'end')
    if any(k not in project for k in required):
        raise ValueError('missing_project_fields')
    if not isinstance(project['client'], str) or not project['client'].strip():
        raise ValueError('invalid_client')
    end = core.engine.iso_date(project['end'])
    offer = {k: project[k] for k in required if k not in ('actual_hours', 'price_sgd', 'end')}
    offer.update(interviewees=0, deadline=end.isoformat(), budget_sgd=project['price_sgd'], direct_costs_sgd=0)
    errors = core.engine.validation_errors({'as_of': end.isoformat(), 'comparable_ids': [], 'offer': offer})
    if errors:
        raise ValueError('invalid_project_scope')
    hours = project.get('actual_hours')
    if (hours is not None and _nonnegative(hours, 'actual_hours') <= 0) or _nonnegative(project['price_sgd'], 'price_sgd') <= 0:
        raise ValueError('positive_actual_hours_and_fee_required')
    item = {k: project[k] for k in required}
    item['actual_hours'] = hours
    brief = project.get('project_brief', '')
    if not isinstance(brief, str) or len(brief) > 10000:
        raise ValueError('invalid_project_brief')
    item['project_brief'] = brief
    item['project_id'] = 'founder:' + str(uuid.uuid4())
    conn.execute('INSERT INTO founder_past_projects VALUES (?,?,?)', (item['project_id'], db.j(item), db.now()))
    return {'project_id': item['project_id']}


def history_benchmark(offer, rows, as_of=None):
    """Exact scope-family matches, normalized by deliverable and actual hours.

    Fees are historical selling prices, not cost or profit. At least two clean
    completed jobs are required; no claim of statistical accuracy is implied.
    """
    matches = []
    fields = ('format', 'production_mode', 'shoot_days', 'locations', 'scripting',
              'editing_level', 'graphics', 'revision_rounds')
    for row in rows:
        try:
            if any(str(row.get(k)) != str(offer.get(k)) for k in fields):
                continue
            if as_of and (not row.get('end') or row['end'] >= as_of):
                continue
            count = int(row['deliverables'])
            hours = float(row['actual_hours']) if row.get('actual_hours') is not None else None
            price = float(row['price_sgd'])
            if count <= 0 or not math.isfinite(price) or price <= 0 or (hours is not None and (not math.isfinite(hours) or hours <= 0)):
                continue
            matches.append({'project_id': row['project_id'], 'deliverables': count,
                            'actual_hours': hours, 'price_sgd': price,
                            'price_per_deliverable_sgd': price / count,
                            'price_per_actual_hour_sgd': price / hours if hours is not None else None,
                            'scope': {k: row.get(k) for k in ('shoot_days','deliverables','caption_versions','revision_rounds','locations','graphics','scripting')}})
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    if len(matches) < 2:
        return None
    hourly = [x['price_per_actual_hour_sgd'] for x in matches if x['price_per_actual_hour_sgd'] is not None]
    return {'sample_count': len(matches), 'samples': matches,
            'median_price_per_deliverable_sgd': round(statistics.median(x['price_per_deliverable_sgd'] for x in matches), 2),
            'median_price_per_actual_hour_sgd': round(statistics.median(hourly), 2) if hourly else None}


def save_promo_template(conn, args):
    """Founder-only catalog input; approval is never inferred from buyer facts."""
    version = args['version']
    basis_date = args['basis_date']
    datetime.strptime(basis_date, '%Y-%m-%d')
    hours = args['hours']
    required = ('prep', 'shoot', 'ingest', 'review_selected', 'review_per_footage_minute',
                'original_edit', 'cutdown', 'captions', 'review_round', 'delivery')
    if not isinstance(hours, dict) or set(hours) != set(required):
        raise ValueError('template_hours_required')
    for key in required:
        if _nonnegative(hours[key], key) <= 0:
            raise ValueError('positive_template_hours_required')
    if not isinstance(args.get('note'), str) or not args['note'].strip():
        raise ValueError('producer_basis_required')
    rate = args['rate']
    save_rate_version(conn, version, [dict(rate, role='promo_producer')])
    template = {k: copy.deepcopy(args[k]) for k in ('hours', 'basis_date', 'note')}
    template['buffer_pct'] = _nonnegative(args['buffer_pct'], 'buffer_pct')
    template['supported'] = args['supported']
    if not isinstance(template['supported'], dict):
        raise ValueError('supported_template_scope_required')
    conn.execute('CREATE TABLE IF NOT EXISTS promo_templates (version TEXT PRIMARY KEY, template_json TEXT NOT NULL)')
    conn.execute('INSERT INTO promo_templates VALUES (?,?)', (version, db.j(template)))
    return {'template_version': version}


def promo_spec(conn, request):
    """Expand only the explicitly approved scope family, with inspectable inputs."""
    import core
    offer = request['offer']
    facts = request.get('production_facts', {})
    if not isinstance(facts, dict):
        raise ValueError('buyer:production_facts')
    if facts.get('production.deadline_kind') == 'first_cut' and not facts.get('production.final_delivery_date'):
        raise ValueError('buyer:production.final_delivery_date')
    try:
        rows = conn.execute('SELECT * FROM promo_templates ORDER BY rowid DESC').fetchall()
    except sqlite3.OperationalError:
        rows = []
    row = next((r for r in rows if request.get('template_version', r['version']) == r['version']), None)
    if row is None:
        raise ValueError('Alex:studio_approved_promo_template')
    template = json.loads(row['template_json'])
    for key, value in template['supported'].items():
        if offer.get(key, facts.get(key)) != value:
            raise ValueError('Alex:unsupported_template_scope.' + key)
    if offer['format'] not in ('product_promo', 'social_reel') or offer['shoot_days'] > 1 or offer['interviewees']:
        raise ValueError('Alex:unsupported_template_scope')
    if offer['graphics'] not in ('none', 'basic_titles'):
        raise ValueError('Alex:graphics')
    if offer['scripting'] == 'full' or offer['editing_level'] != 'standard' or offer['locations'] > 1:
        raise ValueError('Alex:production_complexity')
    n = offer['deliverables']
    originals = facts.get('production.original_edits')
    cuts = facts.get('production.cutdowns')
    if originals is None or cuts is None:
        raise ValueError('buyer:production.original_edits_and_cutdowns')
    if type(originals) is not int or type(cuts) is not int or originals < 1 or cuts < 0 or originals + cuts != n:
        raise ValueError('buyer:production.original_edits_and_cutdowns')
    if cuts and originals != 1:
        raise ValueError('Alex:cutdown_master_mapping')
    selected = facts.get('production.clips_preselected')
    if type(selected) is not bool:
        raise ValueError('buyer:production.clips_preselected')
    footage = facts.get('production.footage_minutes')
    if not selected and (footage is None or _nonnegative(footage, 'footage_minutes') <= 0):
        raise ValueError('buyer:production.footage_minutes')
    final = facts.get('production.final_delivery_date', offer['deadline'])
    if final != offer['deadline']:
        raise ValueError('buyer:deadline_must_match_final_delivery_date')
    first = facts.get('production.first_cut_date')
    if first and not request['as_of'] <= first <= final:
        raise ValueError('buyer:production.first_cut_date')
    rates = json.loads(conn.execute('SELECT rates_json FROM estimate_rate_versions WHERE version=?', (row['version'],)).fetchone()[0])[0]
    spec = {'rate_version': row['version'], 'buffer_pct': template['buffer_pct'],
            'floor_rate_cents': 7000, 'items': [], 'tasks': [], 'setups': [],
            'expenses': [{'cost_cents': _money(offer['direct_costs_sgd'] * 100),
                          'sell_cents': _money(offer['direct_costs_sgd'] * 100)}],
            'template_basis': template, 'production_facts': copy.deepcopy(facts)}
    h = template['hours']
    all_ids = ['video-' + str(i + 1) for i in range(n)]
    def add(ident, quantity, hours, deliverables, deps=(), shoot=False):
        if not quantity:
            return
        item = dict(rates, id=ident, quantity=quantity, unit_hours=hours,
                    basis='producer template ' + template['basis_date'] + ': ' + template['note'],
                    deliverables=deliverables, depends_on=list(deps))
        task = {'id': ident, 'item_ids': [ident], 'minutes': math.ceil(quantity * hours * (1 + spec['buffer_pct'] / 100) * 60),
                'resources': ['promo_producer'], 'depends_on': list(deps),
                'release_at': request['as_of'] + 'T09:00', 'deadline': final + 'T17:00'}
        if first and ident.startswith(('original-', 'cutdown-')):
            task['deadline'] = first + 'T17:00'
        if shoot:
            item['setup_id'] = task['setup_id'] = 'shared-shoot'
            task['kind'] = 'shoot'
        spec['items'].append(item)
        spec['tasks'].append(task)
    add('prep', 1, h['prep'], all_ids)
    if offer['shoot_days']:
        spec['setups'] = [{'id': 'shared-shoot', 'kind': 'shoot'}]
        add('shoot', 1, h['shoot'], all_ids, ['prep'], True)
    add('ingest', 1, h['ingest'], all_ids, ['shoot' if offer['shoot_days'] else 'prep'])
    spec['items'][-1]['supplied'] = not bool(offer['shoot_days'])
    add('footage-review', 1 if selected else footage,
        h['review_selected'] if selected else h['review_per_footage_minute'], all_ids, ['ingest'])
    for i, video in enumerate(all_ids):
        edit = ('original-' if i < originals else 'cutdown-') + video
        deps = ['footage-review'] if i < originals else ['original-video-1']
        add(edit, 1, h['original_edit'] if i < originals else h['cutdown'], [video], deps)
        previous = edit
        if i < offer['caption_versions']:
            previous = 'captions-' + video
            add(previous, 1, h['captions'], [video], [edit])
        review = 'review-' + video
        add(review, offer['revision_rounds'], h['review_round'], [video], [previous])
        add('delivery-' + video, 1, h['delivery'], [video], [review if offer['revision_rounds'] else previous])
    needed = math.ceil(detailed_estimate(spec)['planning_hours'] * 60)
    total = sum(t['minutes'] for t in spec['tasks'])
    spec['tasks'][-1]['minutes'] += max(0, needed - total)
    return spec


# Formats the promo rate card prices as product promos (see evaluate).
PROMO_FAMILY = {'social_reel'}


def evaluate(conn, request, data, history_override=None):
    """Legacy evaluation with opt-in historical commercial rate calibration.

    Historical calibration is the default; explicit false preserves pinned replays.
    The engine itself performs all option and floor checks with the adjusted
    rate card. Evidence is recorded alongside the resulting estimate.
    """
    import core
    if request.get('detailed_spec') is not None:
        return _detailed_with_alternatives(conn, request, data)
    if request.get('historical_pricing') is False or request.get('pricing_mode') == 'legacy_replay':
        return core.engine.evaluate(request, data)
    # Every other brief (including everything the QwenWork pipeline sends) is
    # priced from similar past jobs plus completed actuals. A promo template,
    # if one is registered, is never required and never sets this price.
    # Include completed, eligible actuals in the effort evidence as well as
    # commercial calibration. The engine remains responsible for compatibility.
    history = get_history(conn) if history_override is None else history_override
    data = copy.deepcopy(data)
    request = copy.deepcopy(request)
    if isinstance(request.get('offer'), dict) and request['offer'].get('format') in PROMO_FAMILY:
        # A short-form social reel is priced like the studio's other promos: the
        # engine only has a promo rate card, so reels and promos share one family
        # for both the effort comparables and the historical fee benchmark.
        request['offer']['format'] = 'product_promo'
        for row in data['past_projects']:
            if row.get('format') in PROMO_FAMILY:
                row['format'] = 'product_promo'
        history = [dict(row, format='product_promo') if row.get('format') in PROMO_FAMILY else row for row in history]
    existing = {row['project_id'] for row in data['past_projects']}
    additional = [row for row in history if row.get('project_id') not in existing
                  and row.get('actual_hours') is not None and row.get('end') and row['end'] < request.get('as_of', '')]
    data['past_projects'].extend(additional)
    effective_request = copy.deepcopy(request)
    if isinstance(request.get('offer'), dict) and isinstance(request.get('comparable_ids'), list):
        try:
            effort = core.engine.numeric_table(data['effort_rules'], 'rule', 'hours')
            rules = core.engine.numeric_table(data['business_rules'], 'key', 'value')
            eligible = core.engine.comparable_evidence(request['offer'],
                [row['project_id'] for row in additional], additional, effort,
                rules['included_revision_rounds'])['used']
            effective_request['comparable_ids'] += [row['project_id'] for row in eligible]
        except (KeyError, TypeError, ValueError):
            pass  # Normal engine validation returns the authoritative error.
    preliminary = core.engine.evaluate(effective_request, data)
    if preliminary['estimate'] is None:
        return preliminary
    benchmark = history_benchmark(request['offer'], history, request.get('as_of'))
    if benchmark is None:
        preliminary['historical_pricing'] = {'status': 'insufficient_history', 'minimum_samples': 2}
        return preliminary
    # Normalize project fee to a rate per video while retaining the demo setup
    # and add-on prices. Floor and discount are rechecked by the engine.
    adjusted = copy.deepcopy(data)
    prices = core.engine.numeric_table(data['rate_card'], 'item', 'price_sgd')
    effort = core.engine.numeric_table(data['effort_rules'], 'rule', 'hours')
    rules = core.engine.numeric_table(data['business_rules'], 'key', 'value')
    offer = request['offer']
    setup = prices['edit_only_setup'] if offer['shoot_days'] == 0 else prices['one_shoot_day']
    setup += max(0, offer['shoot_days'] - 1) * prices.get('extra_shoot_day', 0)
    normalized = []
    for sample in benchmark['samples']:
        scope = dict(sample['scope'])
        for field in ('shoot_days', 'deliverables', 'caption_versions', 'revision_rounds', 'locations'):
            scope[field] = int(scope[field])
        historical_addons = sum(x['price_sgd'] for x in core.engine.addons(scope, effort, prices, rules['included_revision_rounds']).values())
        normalized.append((sample['price_sgd'] - setup - historical_addons) / sample['deliverables'])
    if any(x <= 0 for x in normalized):
        preliminary['historical_pricing'] = {'status': 'incompatible_fees', **benchmark}
        return preliminary
    price = round(statistics.median(normalized), 2)
    for row in adjusted['rate_card']:
        if row['item'] == 'promo_video':
            row['price_sgd'] = price
    result = core.engine.evaluate(effective_request, adjusted)
    result['historical_pricing'] = {'status': 'applied', **benchmark,
                                    'basis': 'median_fee_less_setup_and_addons_per_deliverable', 'unit_price_sgd': price,
                                    'leave_one_out': [{'excluded_project_id': sample['project_id'],
                                        'unit_price_sgd': round(statistics.median(normalized[:i] + normalized[i+1:]), 2)}
                                        for i, sample in enumerate(benchmark['samples'])]}
    return result


def _validate_setups(spec):
    """Optional production setups group shared work. A shoot setup is one
    cost-bearing shoot session; supplied footage never joins a shoot setup."""
    raw = spec.get('setups', [])
    if not isinstance(raw, list) or len(raw) > 50:
        raise ValueError('invalid_setups')
    setups = {}
    for setup in raw:
        if not isinstance(setup, dict) or not isinstance(setup.get('id'), str) or not setup['id'] or setup['id'] in setups:
            raise ValueError('invalid_setup_id')
        if setup.get('kind') not in ('shoot', 'post'):
            raise ValueError('invalid_setup_kind')
        for key in ('location', 'date'):
            if key in setup and (not isinstance(setup[key], str) or not setup[key]):
                raise ValueError('invalid_setup_' + key)
        if 'date' in setup:
            try:
                datetime.strptime(setup['date'], '%Y-%m-%d')
            except ValueError:
                raise ValueError('invalid_setup_date')
        if 'min_call_hours' in setup:
            _nonnegative(setup['min_call_hours'], 'min_call_hours')
        setups[setup['id']] = setup
    return setups


def _validate_task_coverage(spec, estimate, tasks, required_item_ids=None):
    """Detect underallocated individual work lines, not just total hours.

    When any schedule task declares item_ids, every work line must be covered
    by tasks whose minutes sum to at least its buffered hours. Shoot setups
    must map to exactly one contiguous shoot task meeting the minimum call.
    """
    ids = {line['id'] for line in estimate['lines']}
    linked = [t for t in tasks if 'item_ids' in t]
    if linked:
        covered = {}
        for task in linked:
            item_ids = task['item_ids']
            if not isinstance(item_ids, list) or not item_ids or len(set(item_ids)) != len(item_ids) or any(not isinstance(x, str) or x not in ids for x in item_ids):
                raise ValueError('invalid_task_item_ids')
            minutes = _nonnegative(task.get('minutes'), 'minutes')
            # Allocate a block across its lines, never credit it in full to each.
            weights = {line['id']: line['hours'] for line in estimate['lines'] if line['id'] in item_ids}
            total = sum(weights.values())
            for x in item_ids:
                credit = minutes * weights[x] / total if total else 0
                covered[x] = covered.get(x, 0) + credit
        buffer = _nonnegative(spec.get('buffer_pct', 0), 'buffer_pct')
        for line in estimate['lines']:
            if required_item_ids is not None and line['id'] not in required_item_ids:
                continue
            need = math.ceil(line['hours'] * (1 + buffer / 100) * 60)
            if covered.get(line['id'], 0) < need:
                raise ValueError('task_underallocated:' + line['id'])
    setups = _validate_setups(spec)
    for task in tasks:
        if 'setup_id' in task and task['setup_id'] not in setups:
            raise ValueError('unknown_setup')
    for sid, setup in setups.items():
        if setup['kind'] != 'shoot':
            continue
        tagged = [t for t in tasks if t.get('setup_id') == sid]
        if len(tagged) != 1:
            raise ValueError('shoot_setup_needs_one_task:' + sid)
        task = tagged[0]
        if task.get('kind') != 'shoot' and not task.get('contiguous'):
            raise ValueError('shoot_setup_task_must_be_contiguous:' + sid)
        if 'min_call_hours' in setup and _nonnegative(task.get('minutes'), 'minutes') < setup['min_call_hours'] * 60:
            raise ValueError('shoot_setup_below_min_call:' + sid)


def _validate_finished_phase(spec, phase, total_count):
    ids = phase.get('deliverable_ids', ['video-' + str(i + 1) for i in range(phase['deliverables'])])
    if not isinstance(ids, list) or len(set(ids)) != phase['deliverables']:
        raise ValueError('phase_deliverable_ids_required')
    promised = set(ids)
    lines = spec['items']
    known = {d for line in lines for d in line.get('deliverables', [])}
    if not promised <= known or len(known) != total_count:
        raise ValueError('phase_deliverable_coverage_required')
    needed = {line['id'] for line in lines if promised.intersection(line.get('deliverables', []))}
    tasks = [t for t in spec['tasks'] if t['id'] in phase['task_ids']]
    if not needed <= {i for t in tasks for i in t.get('item_ids', [])}:
        raise ValueError('phase_unfinished_deliverables')
    # Keep every original line and task link when allocating phase minutes.
    # Only the required coverage is narrowed; dropping other links would
    # redistribute their work to the promised deliverables. Full-schedule
    # validation already checks setups, including shoots in later batches.
    phase_tasks = [{k: v for k, v in t.items() if k != 'setup_id'} for t in tasks]
    _validate_task_coverage({**spec, 'setups': []}, detailed_estimate(spec),
                            phase_tasks, required_item_ids=needed)


def validate_evidence_links(conn, spec, inquiry_id):
    """Every cited observation or fact must belong to the priced inquiry."""
    for item in spec.get('items', []):
        for link in item.get('evidence', []):
            if link['kind'] == 'observation':
                found = conn.execute(
                    'SELECT 1 FROM reference_observations o JOIN reference_runs r ON r.id=o.run_id '
                    'JOIN reference_assets a ON a.id=r.asset_id WHERE o.id=? AND a.inquiry_id=?',
                    (link['id'], inquiry_id)).fetchone()
            else:
                found = conn.execute('SELECT 1 FROM inquiry_facts WHERE id=? AND inquiry_id=?',
                                     (link['id'], inquiry_id)).fetchone()
            if not found:
                raise ValueError('evidence_not_in_inquiry:' + item['id'])


def detailed_estimate(spec):
    """Calculate explicit work lines without combining legacy package effort.

    Each work item supplies quantity, unit_hours, cost_rate_cents and
    sell_rate_cents. Expenses carry cost_cents and sell_cents. Shared work is
    represented by a single item. Optional low/high hours are scenario totals.
    """
    if not isinstance(spec, dict) or not isinstance(spec.get('items'), list) or not spec['items']:
        raise ValueError('items_required')
    if not isinstance(spec.get('rate_version'), str) or not spec['rate_version'].strip():
        raise ValueError('rate_version_required')
    if spec.get('legacy_package'):
        raise ValueError('legacy_package_cannot_be_combined_with_tasks')
    buffer = _nonnegative(spec.get('buffer_pct', 0), 'buffer_pct')
    if buffer > 100:
        raise ValueError('invalid_buffer_pct')
    minimum = _nonnegative(spec.get('floor_rate_cents', 7000), 'floor_rate_cents')
    setups = _validate_setups(spec)
    lines, ids, dependencies = [], set(), {}
    for item in spec['items']:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in ids:
            raise ValueError('invalid_item_id')
        ids.add(item['id'])
        if not item.get('basis') or not item.get('role'):
            raise ValueError('item_basis_and_role_required')
        q = _nonnegative(item.get('quantity'), 'quantity')
        h = _nonnegative(item.get('unit_hours'), 'unit_hours')
        cost = _nonnegative(item.get('cost_rate_cents'), 'cost_rate_cents')
        sell = _nonnegative(item.get('sell_rate_cents'), 'sell_rate_cents')
        if int(cost) != cost or int(sell) != sell:
            raise ValueError('rates_must_be_cents')
        low = _nonnegative(item.get('low_unit_hours', h), 'low_unit_hours')
        high = _nonnegative(item.get('high_unit_hours', h), 'high_unit_hours')
        if not low <= h <= high:
            raise ValueError('invalid_scenario_range')
        if (low != h or high != h) and not item.get('assumption'):
            raise ValueError('scenario_assumption_required')
        setup_id = item.get('setup_id')
        if setup_id is not None:
            if setup_id not in setups:
                raise ValueError('unknown_setup:' + str(item['id']))
            if item.get('supplied') and setups[setup_id]['kind'] == 'shoot':
                raise ValueError('supplied_footage_cannot_use_shoot_setup:' + item['id'])
        supplied = item.get('supplied', False)
        if not isinstance(supplied, bool):
            raise ValueError('invalid_supplied_flag')
        deliverables = item.get('deliverables', [])
        if not isinstance(deliverables, list) or len(deliverables) > 50 or not all(isinstance(x, str) and x for x in deliverables):
            raise ValueError('invalid_item_deliverables')
        evidence = item.get('evidence', [])
        if not isinstance(evidence, list) or len(evidence) > 20:
            raise ValueError('invalid_item_evidence')
        for link in evidence:
            if not isinstance(link, dict) or link.get('kind') not in ('observation', 'fact') or not isinstance(link.get('id'), str) or not link['id']:
                raise ValueError('invalid_item_evidence')
        deps = item.get('depends_on', [])
        if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
            raise ValueError('invalid_item_dependencies')
        dependencies[item['id']] = deps
        hours = q * h
        lines.append({'id': item['id'], 'role': item['role'], 'basis': item['basis'],
                      'quantity': q, 'unit_hours': h, 'hours': hours,
                      'low_hours': q * low, 'high_hours': q * high,
                      'assumption': item.get('assumption'),
                      'setup_id': setup_id, 'supplied': supplied,
                      'deliverables': deliverables, 'evidence': evidence,
                      'depends_on': deps,
                      'cost_cents': _money(hours * cost), 'sell_cents': _money(hours * sell)})
    for source, deps in dependencies.items():
        if any(d not in ids for d in deps):
            raise ValueError('item_dependency_not_found:' + source)
    resolved, pending = set(), dict(dependencies)
    while pending:
        ready = [k for k, deps in pending.items() if all(d in resolved for d in deps)]
        if not ready:
            raise ValueError('cyclic_item_dependencies')
        for k in ready:
            resolved.add(k); del pending[k]
    expenses = spec.get('expenses', [])
    if not isinstance(expenses, list):
        raise ValueError('invalid_expenses')
    expense_cost = expense_sell = 0
    for expense in expenses:
        for key in ('cost_cents', 'sell_cents'):
            v = _nonnegative(expense.get(key), key)
            if int(v) != v:
                raise ValueError('expenses_must_be_cents')
        expense_cost += expense['cost_cents']
        expense_sell += expense['sell_cents']
    base = sum(x['hours'] for x in lines)
    planned = round(base * (1 + buffer / 100), 2)
    # Scale each rounded base labour line by the planning allowance.
    labor_cost = sum(_money(x['cost_cents'] * (1 + buffer / 100)) for x in lines)
    labor_sell = sum(_money(x['sell_cents'] * (1 + buffer / 100)) for x in lines)
    return {'version': 'detailed-v1', 'rate_version': spec['rate_version'], 'buffer_pct': buffer, 'lines': lines,
            'setups': spec.get('setups', []),
            'expenses': expenses, 'base_hours': round(base, 4), 'planning_hours': planned,
            'low_hours': round(sum(x['low_hours'] for x in lines) * (1 + buffer / 100), 2),
            'high_hours': round(sum(x['high_hours'] for x in lines) * (1 + buffer / 100), 2),
            'scenarios_are_probabilities': False,
            'production_cost_cents': labor_cost + expense_cost,
            'floor_cents': _money(planned * minimum) + expense_cost,
            'selling_price_cents': labor_sell + expense_sell}


def validate_catalog_rates(conn, spec):
    catalog_items = spec.get('items', [])
    row = conn.execute('SELECT rates_json FROM estimate_rate_versions WHERE version=?', (spec.get('rate_version'),)).fetchone()
    if not row:
        raise ValueError('unknown_rate_version')
    rates = {item['role']: item for item in json.loads(row['rates_json'])}
    for item in catalog_items:
        rate = rates.get(item.get('role'))
        if not rate or any(item.get(k) != rate[k] for k in ('cost_rate_cents', 'sell_rate_cents')):
            raise ValueError('catalog_rate_mismatch')


def save_estimate(conn, deal_id, scope_revision_id, spec):
    deal = conn.execute('SELECT sample_data FROM deals WHERE id=?', (deal_id,)).fetchone()
    if not deal or not deal['sample_data']:
        raise ValueError('sample_deal_required')
    if not isinstance(scope_revision_id, str) or not scope_revision_id:
        raise ValueError('scope_revision_required')
    result = detailed_estimate(spec)
    validate_catalog_rates(conn, spec)
    ident = str(uuid.uuid4())
    conn.execute('INSERT INTO detailed_estimates VALUES (?,?,?,?,?,?,?)',
                 (ident, deal_id, scope_revision_id, spec['rate_version'], db.j(spec), db.j(result), db.now()))
    return {'estimate_id': ident, 'estimate': result}


def _dt(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is not None:
        raise ValueError('use_local_naive_datetime')
    return result


def save_rate_version(conn, version, rates):
    if not isinstance(version, str) or not version.strip() or not isinstance(rates, list) or not rates:
        raise ValueError('invalid_rate_version')
    for rate in rates:
        if not isinstance(rate, dict) or not rate.get('role'):
            raise ValueError('invalid_rate')
        for key in ('cost_rate_cents', 'sell_rate_cents'):
            value = _nonnegative(rate.get(key), key)
            if int(value) != value:
                raise ValueError('rates_must_be_cents')
    if conn.execute('SELECT 1 FROM estimate_rate_versions WHERE version=?', (version,)).fetchone():
        raise ValueError('rate_version_already_exists')
    conn.execute('INSERT INTO estimate_rate_versions VALUES (?,?,?)', (version, db.j(rates), db.now()))
    return {'rate_version': version}


def add_availability(conn, resource, start_at, end_at):
    if not isinstance(resource, str) or not resource.strip() or _dt(end_at) <= _dt(start_at):
        raise ValueError('invalid_availability')
    ident = str(uuid.uuid4())
    conn.execute('INSERT INTO resource_availability VALUES (?,?,?,?)', (ident, resource, start_at, end_at))
    return {'availability_id': ident}


def schedule(conn, tasks, hypothetical_allocations=None):
    """Find deterministic non-overlapping task blocks on resource calendars.

    Tasks are topologically ordered by dependencies. Shoot tasks are contiguous;
    other tasks may be split at availability and allocation boundaries. All
    resources on a task must be simultaneously available for each block.
    """
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('tasks_required')
    pending = {t['id']: t for t in tasks}
    if len(pending) != len(tasks):
        raise ValueError('duplicate_task_id')
    assigned, finish = [], {}
    busy = [(r['resource'], _dt(r['start_at']), _dt(r['end_at'])) for r in conn.execute('SELECT * FROM resource_allocations')]
    # Preview callers may layer an uncommitted candidate on the durable
    # calendar.  These rows have the same shape returned by this planner, so
    # preview and confirmation cannot drift onto different scheduling logic.
    if hypothetical_allocations is not None:
        if not isinstance(hypothetical_allocations, list):
            raise ValueError('invalid_hypothetical_allocations')
        try:
            for row in hypothetical_allocations:
                resource, start, end = row['resource'], _dt(row['start_at']), _dt(row['end_at'])
                if not isinstance(resource, str) or not resource or end <= start:
                    raise ValueError()
                busy.append((resource, start, end))
        except (KeyError, TypeError, ValueError):
            raise ValueError('invalid_hypothetical_allocations')
    availability = [(r['resource'], _dt(r['start_at']), _dt(r['end_at'])) for r in conn.execute('SELECT * FROM resource_availability')]
    while pending:
        ready = sorted(k for k, t in pending.items() if all(d in finish for d in t.get('depends_on', [])))
        if not ready:
            raise ValueError('invalid_or_cyclic_dependencies')
        ident = ready[0]
        task = pending.pop(ident)
        resources = task.get('resources')
        if not isinstance(resources, list) or not resources or len(set(resources)) != len(resources) or not all(isinstance(x, str) and x for x in resources):
            raise ValueError('resources_required')
        minutes = _nonnegative(task.get('minutes'), 'minutes')
        if int(minutes) != minutes or minutes <= 0:
            raise ValueError('invalid_minutes')
        release = max([_dt(task['release_at'])] + [finish[d] for d in task.get('depends_on', [])])
        deadline = _dt(task['deadline'])
        points = {release, deadline}
        for resource, start, end in availability + busy:
            if resource in resources:
                points.update((max(release, start), min(deadline, end)))
        points = sorted(p for p in points if release <= p <= deadline)
        blocks = []
        for a, b in zip(points, points[1:]):
            if b <= a or not all(any(r == resource and s <= a and e >= b for r, s, e in availability) and not any(r == resource and s < b and e > a for r, s, e in busy) for resource in resources):
                continue
            if blocks and blocks[-1][1] == a:
                blocks[-1] = (blocks[-1][0], b)
            else:
                blocks.append((a, b))
        remaining = timedelta(minutes=minutes)
        chosen = []
        if task.get('contiguous', False) or task.get('kind') == 'shoot':
            chosen = [(a, a + remaining) for a, b in blocks if b - a >= remaining][:1]
            if not chosen:
                return {'feasible': False, 'blocked_task_id': ident, 'allocations': []}
        else:
            for a, b in blocks:
                take = min(b - a, remaining)
                if take > timedelta(0):
                    chosen.append((a, a + take)); remaining -= take
                if remaining <= timedelta(0):
                    break
            if remaining > timedelta(0):
                return {'feasible': False, 'blocked_task_id': ident, 'allocations': []}
        finish[ident] = chosen[-1][1]
        for a, b in chosen:
            for resource in resources:
                busy.append((resource, a, b))
                assigned.append({'task_id': ident, 'resource': resource, 'start_at': a.isoformat(), 'end_at': b.isoformat()})
    return {'feasible': True, 'allocations': assigned}


def reserve_schedule(conn, reservation_id, tasks):
    """Recheck and reserve under caller's BEGIN IMMEDIATE booking transaction."""
    if not conn.in_transaction:
        raise ValueError('booking_transaction_required')
    plan = schedule(conn, tasks)
    if not plan['feasible']:
        raise ValueError('resource_schedule_conflict:' + plan['blocked_task_id'])
    for a in plan['allocations']:
        conn.execute('INSERT INTO resource_allocations VALUES (?,?,?,?,?,?)',
                     (str(uuid.uuid4()), reservation_id, a['resource'], a['start_at'], a['end_at'], a['task_id']))
    return plan


def _detailed_with_alternatives(conn, request, data):
    """Reprice founder-supplied alternate task sets; never invent task effort."""
    import core
    result = _evaluate_detailed(conn, request, data)
    if result['verdict'] == 'clarify':
        return result
    variants = request.get('detailed_alternatives', [])
    if not isinstance(variants, list) or len(variants) > 4:
        return core.engine.answer('clarify', ['INVALID_FIELD'], invalid=['detailed_alternatives'])
    keys = {'deliverables', 'caption_versions', 'deadline', 'budget_sgd', 'direct_costs_sgd'}
    options = list(result['options'])
    metrics = dict(result['option_metrics'])
    for index, variant in enumerate(variants):
        try:
            override = variant['offer']
            if not isinstance(override, dict) or set(override) - keys:
                raise ValueError('invalid_alternative_offer')
            alt = {**request, 'offer': {**request['offer'], **override},
                   'detailed_spec': variant['spec'], 'detailed_alternatives': []}
            candidate = _evaluate_detailed(conn, alt, data)
            if candidate['verdict'] == 'clarify':
                # An invalid alternate spec is a scoping error, not a
                # commercially weaker option; surface it for correction.
                return core.engine.answer('clarify', ['INVALID_FIELD'],
                                          invalid=['detailed_alternatives.' + str(index)])
            if candidate['verdict'] not in ('accept', 'accept_with_approval'):
                continue
            opt = candidate['options'][0]
            key = 'alternative_' + str(index + 1)
            opt['option_id'] = opt['kind'] = key
            metric = candidate['option_metrics']['as_offered']
            phases = variant.get('phases', [])
            if phases:
                if not isinstance(phases, list) or len(phases) != 2:
                    raise ValueError('invalid_phases')
                first, second = phases
                if (type(first['deliverables']) is not int or type(second['deliverables']) is not int or
                    first['deliverables'] <= 0 or second['deliverables'] <= 0 or
                    first['deliverables'] + second['deliverables'] != opt['deliverables'] or
                    not (request['as_of'] <= first['deadline'] < second['deadline'] == opt['deadline'])):
                    raise ValueError('invalid_phases')
                phase_ids = first.get('task_ids')
                tasks = metric['detailed_tasks']
                if not isinstance(phase_ids, list) or not phase_ids or len(set(phase_ids)) != len(phase_ids):
                    raise ValueError('phase_tasks_required')
                phase_tasks = [t for t in tasks if t['id'] in phase_ids]
                if len(phase_tasks) != len(phase_ids) or any(t['deadline'][:10] > first['deadline'] for t in phase_tasks):
                    raise ValueError('invalid_phase_tasks')
                if any(d not in phase_ids for t in phase_tasks for d in t.get('depends_on', [])):
                    raise ValueError('phase_dependency_not_included')
                _validate_finished_phase(variant['spec'], first, opt['deliverables'])
                early_hours = sum(t['minutes'] for t in phase_tasks) / 60
                rules = core.engine.numeric_table(data['business_rules'], 'key', 'value')
                early_capacity = core.engine.capacity(data['calendar_weeks'], core.engine.iso_date(request['as_of']), core.engine.iso_date(first['deadline']), rules['capacity_buffer_pct'])
                if early_hours > early_capacity or early_hours > metric['planning_hours']:
                    continue
                opt['phases'] = [{'deliverables': first['deliverables'], 'deadline': first['deadline']},
                                 {'deliverables': second['deliverables'], 'deadline': second['deadline']}]
                metric['first_phase_planning_hours'] = early_hours
                metric['first_phase_capacity_hours'] = early_capacity
            signature = lambda o: json.dumps({k:v for k,v in o.items() if k not in ('option_id','kind')}, sort_keys=True)
            if any(signature(existing) == signature(opt) for existing in options):
                continue
            options.append(opt)
            metrics[key] = metric
        except (KeyError, TypeError, ValueError, OverflowError):
            return core.engine.answer('clarify', ['INVALID_FIELD'], invalid=['detailed_alternatives.' + str(index)])
    if options:
        if result['verdict'] in ('decline', 'counter'):
            result['verdict'] = 'counter'
            result['reason_codes'] = [r for r in result['reason_codes'] if r != 'NO_FEASIBLE_OPTIONS']
        result['options'], result['option_metrics'] = options, metrics
    return result


def _evaluate_detailed(conn, request, data):
    import core
    engine = core.engine
    errors = engine.validation_errors(request)
    if errors:
        return engine.answer('clarify', [x[0] for x in errors],
                             missing=[v for k, v in errors if k == 'MISSING_FIELD'],
                             invalid=[v for k, v in errors if k == 'INVALID_FIELD'])
    spec = request['detailed_spec']
    try:
        estimate = detailed_estimate(spec)
        validate_catalog_rates(conn, spec)
        expense_cents = sum(x['cost_cents'] for x in spec.get('expenses', []))
        if expense_cents != round(request['offer']['direct_costs_sgd'] * 100):
            raise ValueError('direct_costs_must_match_expenses')
        tasks = spec.get('tasks')
        if not isinstance(tasks, list) or not tasks:
            raise ValueError('resource_tasks_required')
        if sum(_nonnegative(t.get('minutes'), 'minutes') for t in tasks) < math.ceil(estimate['planning_hours'] * 60):
            raise ValueError('resource_tasks_must_cover_planning_hours')
        _validate_task_coverage(spec, estimate, tasks)
        offer = request['offer']
        deadline = engine.iso_date(offer['deadline'])
        as_of = engine.iso_date(request['as_of'])
        for task in tasks:
            if _dt(task['release_at']).date() < as_of or _dt(task['deadline']).date() > deadline:
                raise ValueError('task_outside_offer_dates')
        checked = schedule(conn, tasks)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return engine.answer('clarify', ['INVALID_FIELD'], invalid=['detailed_spec:' + str(exc)])
    rules = engine.numeric_table(data['business_rules'], 'key', 'value')
    available = engine.capacity(data['calendar_weeks'], as_of, deadline, rules['capacity_buffer_pct'])
    list_price = estimate['selling_price_cents'] / 100
    floor = estimate['floor_cents'] / 100
    budget = offer['budget_sgd']
    estimate.update({'capacity_hours': available, 'list_sgd': list_price, 'floor_sgd': floor,
                     'effective_rate_sgd': round(budget / estimate['planning_hours'], 2) if estimate['planning_hours'] else 0})
    reasons = []
    if estimate['planning_hours'] > available:
        reasons.append('CAPACITY_SHORTFALL')
    if not checked['feasible']:
        reasons.append('RESOURCE_SCHEDULE_SHORTFALL')
    if budget < floor:
        reasons.append('BELOW_FLOOR')
    if list_price < floor:
        reasons.append('SELLING_PRICE_BELOW_FLOOR')
    if budget >= floor and budget < round(list_price * (1 - rules['discount_limit_pct'] / 100), 2):
        reasons.append('OVER_DISCOUNT_LIMIT')
    if reasons:
        return engine.answer('decline', reasons + ['NO_FEASIBLE_OPTIONS'], estimate=estimate)
    verdict = 'accept_with_approval' if budget < list_price else 'accept'
    if verdict == 'accept_with_approval':
        reasons.append('BELOW_LIST_NEEDS_APPROVAL')
    option = engine.public_option('as_offered', offer, min(budget, list_price))
    metrics = {'as_offered': {'planning_hours': estimate['planning_hours'],
                              'floor_sgd': floor, 'capacity_hours': available,
                              'detailed_tasks': copy.deepcopy(tasks),
                              'rate_version': estimate['rate_version'],
                              'detailed_estimate': copy.deepcopy(estimate)}}
    return engine.answer(verdict, reasons, estimate=estimate, options=[option], option_metrics=metrics)


def resource_check(conn, result, terms, hypothetical_allocations=None):
    metrics = (result.get('option_metrics') or {}).get(terms.get('option_id'), {})
    tasks = metrics.get('detailed_tasks')
    return schedule(conn, tasks, hypothetical_allocations) if tasks is not None else {'feasible': True, 'allocations': []}


def reserve_for_option(conn, reservation_id, result, terms):
    metrics = (result.get('option_metrics') or {}).get(terms.get('option_id'), {})
    tasks = metrics.get('detailed_tasks')
    return reserve_schedule(conn, reservation_id, tasks) if tasks is not None else {'feasible': True, 'allocations': []}


def save_producer_override(conn, args):
    """Record a dated producer replacement for one work-line quantity or rate
    of effort. Overrides never invent values; the producer supplies them."""
    for key in ('deal_id', 'scope_revision_id', 'item_id', 'request_id', 'note'):
        if not isinstance(args.get(key), str) or not args[key].strip() or len(args[key]) > 200:
            raise ValueError('invalid_' + key)
    if args['field'] not in ('quantity', 'unit_hours'):
        raise ValueError('invalid_override_field')
    value = _nonnegative(args.get('value'), 'value')
    try:
        datetime.strptime(args.get('basis_date', ''), '%Y-%m-%d')
    except (ValueError, TypeError):
        raise ValueError('invalid_basis_date')
    if not conn.execute('SELECT 1 FROM deals WHERE id=? AND sample_data=1', (args['deal_id'],)).fetchone():
        raise ValueError('sample_deal_required')
    payload = db.j({k: args[k] for k in ('deal_id', 'scope_revision_id', 'item_id', 'field', 'value', 'basis_date', 'note')})
    prior = conn.execute('SELECT * FROM producer_overrides WHERE request_id=?', (args['request_id'],)).fetchone()
    if prior:
        if db.j({k: json.loads(prior['value_json']) if k == 'value' else prior[k] for k in ('deal_id', 'scope_revision_id', 'item_id', 'field', 'value', 'basis_date', 'note')}) != payload:
            raise ValueError('request_id_conflict')
        return {'override_id': prior['id']}
    supersedes = args.get('supersedes')
    if supersedes is not None:
        if not conn.execute('SELECT 1 FROM producer_overrides WHERE id=? AND deal_id=? AND scope_revision_id=? AND item_id=? AND field=?',
                            (supersedes, args['deal_id'], args['scope_revision_id'], args['item_id'], args['field'])).fetchone():
            raise ValueError('invalid_override_source')
    ident = str(uuid.uuid4())
    conn.execute('INSERT INTO producer_overrides VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (ident, args['deal_id'], args['scope_revision_id'], args['item_id'], args['field'],
                  db.j(value), args['basis_date'], args['note'], args['request_id'], supersedes, db.now()))
    return {'override_id': ident}


def apply_producer_overrides(conn, deal_id, scope_revision_id, spec):
    """Return a copy of the spec with the latest dated override per line/field."""
    if not isinstance(spec, dict):
        raise ValueError('invalid_spec')
    rows = conn.execute('SELECT * FROM producer_overrides WHERE deal_id=? AND scope_revision_id=? ORDER BY created_at,rowid',
                        (deal_id, scope_revision_id)).fetchall()
    latest = {}
    for row in rows:
        latest[(row['item_id'], row['field'])] = row
    updated = copy.deepcopy(spec)
    items = {item.get('id'): item for item in updated.get('items', []) if isinstance(item, dict)}
    applied = []
    for (item_id, field), row in latest.items():
        if item_id not in items:
            raise ValueError('override_item_not_found:' + item_id)
        items[item_id][field] = json.loads(row['value_json'])
        items[item_id]['basis'] = 'producer override ' + row['basis_date']
        applied.append({'override_id': row['id'], 'item_id': item_id, 'field': field,
                        'value': json.loads(row['value_json']), 'basis_date': row['basis_date']})
    return updated, applied


def merge_setups(spec, source_ids, target_id):
    """Producer control: combine compatible setups into one shared setup.

    Shoot setups merge only when location and date agree. Merging shoot
    setups still requires one consolidated shoot task at validation time.
    """
    updated = copy.deepcopy(spec)
    setups = {s['id']: s for s in updated.get('setups', []) if isinstance(s, dict)}
    if not isinstance(source_ids, list) or len(source_ids) < 2 or len(set(source_ids)) != len(source_ids) or any(s not in setups for s in source_ids):
        raise ValueError('invalid_merge_sources')
    if not isinstance(target_id, str) or not target_id:
        raise ValueError('invalid_target_setup')
    if target_id in setups and target_id not in source_ids:
        raise ValueError('target_setup_exists')
    sources = [setups[s] for s in source_ids]
    if len({s['kind'] for s in sources}) != 1:
        raise ValueError('cannot_merge_different_setup_kinds')
    merged = {'id': target_id, 'kind': sources[0]['kind']}
    if merged['kind'] == 'shoot':
        for key in ('location', 'date'):
            values = {s[key] for s in sources if s.get(key)}
            if len(values) > 1:
                raise ValueError('shoot_merge_requires_same_location_and_date')
            if values:
                merged[key] = next(iter(values))
    calls = [s['min_call_hours'] for s in sources if s.get('min_call_hours') is not None]
    if calls:
        merged['min_call_hours'] = max(calls)
    updated['setups'] = [s for s in updated.get('setups', []) if s.get('id') not in source_ids] + [merged]
    for item in updated.get('items', []):
        if item.get('setup_id') in source_ids:
            item['setup_id'] = target_id
    for task in updated.get('tasks', []):
        if task.get('setup_id') in source_ids:
            task['setup_id'] = target_id
    return updated


def split_setup(spec, setup_id, groups):
    """Producer control: partition one setup's work into separate setups.

    Each group lists its item_ids; together they must partition the setup's
    items exactly. Tasks tagged with the setup must declare item_ids that fit
    wholly inside one group so they follow their work.
    """
    updated = copy.deepcopy(spec)
    setups = {s['id']: s for s in updated.get('setups', []) if isinstance(s, dict)}
    if setup_id not in setups:
        raise ValueError('unknown_setup')
    original = setups[setup_id]
    if not isinstance(groups, list) or len(groups) < 2:
        raise ValueError('invalid_split_groups')
    members = [i['id'] for i in updated.get('items', []) if i.get('setup_id') == setup_id]
    seen, targets = [], []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('setup_id'), str) or not group['setup_id'] or not isinstance(group.get('item_ids'), list) or not group['item_ids']:
            raise ValueError('invalid_split_groups')
        target = group['setup_id']
        if target in targets or (target != setup_id and target in setups):
            raise ValueError('duplicate_or_existing_split_target')
        targets.append(target)
        if any(x not in members for x in group['item_ids']):
            raise ValueError('split_item_not_in_setup')
        seen.extend(group['item_ids'])
    if len(set(seen)) != len(seen) or sorted(seen) != sorted(members):
        raise ValueError('split_must_partition_setup_items')
    assignment = {item: group['setup_id'] for group in groups for item in group['item_ids']}
    for task in updated.get('tasks', []):
        if task.get('setup_id') != setup_id:
            continue
        item_ids = task.get('item_ids')
        if not isinstance(item_ids, list) or not item_ids:
            raise ValueError('split_task_needs_item_ids')
        homes = {assignment.get(x) for x in item_ids}
        if len(homes) != 1 or None in homes:
            raise ValueError('split_task_spans_groups')
        task['setup_id'] = next(iter(homes))
    for item in updated.get('items', []):
        if item.get('setup_id') == setup_id:
            item['setup_id'] = assignment[item['id']]
    new_setups = []
    for group in groups:
        target = group['setup_id']
        entry = dict(original)
        entry['id'] = target
        new_setups.append(entry)
    updated['setups'] = [s for s in updated.get('setups', []) if s.get('id') != setup_id] + new_setups
    return updated


def scope_opportunities(spec, offer):
    """Structurally detect which scope variables could flex. Effort is never
    invented; each opportunity lists the producer inputs a variant needs."""
    items = [i for i in spec.get('items', []) if isinstance(i, dict)]
    setups = {s['id']: s for s in spec.get('setups', []) if isinstance(s, dict)}
    opportunities = []
    for item in items:
        if type(item.get('quantity')) is int and item['quantity'] >= 2 and not item.get('supplied'):
            opportunities.append({'kind': 'master_to_derivative', 'item_id': item['id'],
                                  'quantity': item['quantity'],
                                  'inputs_required': ['derivative_quantity', 'unit_hours', 'basis']})
    shoot_groups = {}
    for sid, setup in setups.items():
        if setup.get('kind') == 'shoot':
            shoot_groups.setdefault((setup.get('location'), setup.get('date')), []).append(sid)
    for (location, shoot_date), ids in shoot_groups.items():
        if len(ids) >= 2:
            opportunities.append({'kind': 'combine_shoots', 'setup_ids': ids,
                                  'location': location, 'date': shoot_date,
                                  'inputs_required': ['combined_minutes', 'basis']})
    if type(offer.get('deliverables')) is int and offer['deliverables'] >= 2:
        opportunities.append({'kind': 'phase_delivery', 'deliverables': offer['deliverables'],
                              'inputs_required': ['first_deliverables', 'first_deadline', 'first_task_ids']})
    supplied_candidates = [i['id'] for i in items
                           if i.get('setup_id') in setups and setups[i['setup_id']].get('kind') == 'shoot']
    if supplied_candidates:
        opportunities.append({'kind': 'use_supplied_footage', 'item_ids': supplied_candidates,
                              'inputs_required': ['unit_hours', 'basis', 'buyer confirmation the footage covers the work']})
    return opportunities


def _rebalance_task_coverage(spec):
    """Mechanically restore schedule coverage after a scope transformation.
    Line effort is producer-supplied; block minutes follow the coverage rule."""
    estimate = detailed_estimate(spec)
    buffer = spec.get('buffer_pct', 0)
    lines = {line['id']: line for line in estimate['lines']}
    tasks = spec.get('tasks', [])
    if not tasks:
        raise ValueError('tasks_required_after_transformation')
    linked = [t for t in tasks if 'item_ids' in t]
    if linked:
        for task in linked:
            need = math.ceil(sum(lines[x]['hours'] for x in task['item_ids']) * (1 + buffer / 100) * 60)
            task['minutes'] = max(int(task.get('minutes', 0)), need)
        return estimate
    total = sum(int(t.get('minutes', 0)) for t in tasks)
    need = math.ceil(estimate['planning_hours'] * 60)
    if total < need:
        flexible = [t for t in tasks if t.get('kind') != 'shoot' and not t.get('contiguous')]
        if not flexible:
            raise ValueError('no_flexible_task_to_cover_hours')
        target = max(flexible, key=lambda t: int(t.get('minutes', 0)))
        target['minutes'] = int(target.get('minutes', 0)) + (need - total)
    return estimate


def _rewire_task_dependencies(tasks, removed, replacement=None):
    for task in tasks:
        deps = task.get('depends_on')
        if not deps:
            continue
        updated = []
        for dep in deps:
            if dep in removed:
                if replacement and replacement != task['id'] and replacement not in updated:
                    updated.append(replacement)
            elif dep not in updated:
                updated.append(dep)
        task['depends_on'] = updated


def _dedupe(values):
    return list(dict.fromkeys(values))


def _alt_master_to_derivative(spec, offer, as_of, t):
    master_id = t['master_item_id']
    unit_hours = _nonnegative(t.get('unit_hours'), 'unit_hours')
    if unit_hours <= 0:
        raise ValueError('derivative_unit_hours_required')
    basis = t.get('basis')
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError('dated_basis_required')
    updated = copy.deepcopy(spec)
    by_id = {i['id']: i for i in updated['items']}
    if master_id not in by_id:
        raise ValueError('master_item_not_found:' + str(master_id))
    master = by_id[master_id]
    new_id = t.get('new_item_id') or master_id + '-derivatives'
    if new_id in by_id:
        raise ValueError('new_item_id_exists')
    if 'derivative_item_ids' in t:
        ids = t['derivative_item_ids']
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or any(x not in by_id or x == master_id for x in ids):
            raise ValueError('invalid_derivative_item_ids')
        removed = set(ids)
        quantity = len(ids)
        updated['items'] = [i for i in updated['items'] if i['id'] not in removed]
        for item in updated['items']:
            if item.get('depends_on'):
                item['depends_on'] = _dedupe(new_id if d in removed else d for d in item['depends_on'])
        for task in updated.get('tasks', []):
            if 'item_ids' in task:
                task['item_ids'] = _dedupe(new_id if x in removed else x for x in task['item_ids'])
        change = 'converted items ' + ', '.join(ids) + ' into derivatives of ' + master_id
    else:
        quantity = t.get('derivative_quantity')
        if type(quantity) is not int or not 1 <= quantity < master['quantity']:
            raise ValueError('invalid_derivative_quantity')
        master['quantity'] -= quantity
        change = 'converted ' + str(quantity) + ' of ' + master_id + ' into derivatives'
    updated['items'].append({'id': new_id, 'role': master['role'], 'basis': basis,
                             'quantity': quantity, 'unit_hours': unit_hours,
                             'cost_rate_cents': master['cost_rate_cents'],
                             'sell_rate_cents': master['sell_rate_cents'],
                             'depends_on': [master_id], 'deliverables': t.get('deliverables', [])})
    return updated, {}, None, [change]


def _alt_combine_shoots(spec, offer, as_of, t):
    ids = t['setup_ids']
    minutes = t.get('combined_minutes')
    if type(minutes) is not int or minutes <= 0:
        raise ValueError('combined_minutes_required')
    if not isinstance(t.get('basis'), str) or not t['basis'].strip():
        raise ValueError('dated_basis_required')
    setups = {s['id']: s for s in spec.get('setups', [])}
    if not isinstance(ids, list) or len(ids) < 2 or len(set(ids)) != len(ids) or any(s not in setups for s in ids):
        raise ValueError('invalid_setup_ids')
    if any(setups[s].get('kind') != 'shoot' for s in ids):
        raise ValueError('combine_requires_shoot_setups')
    tagged = [task for task in spec.get('tasks', []) if task.get('setup_id') in ids]
    if len(tagged) != len(ids):
        raise ValueError('each_shoot_setup_needs_one_task')
    target = t.get('target_setup_id') or ids[0] + '-combined'
    updated = merge_setups(spec, ids, target)
    merged = next(s for s in updated['setups'] if s['id'] == target)
    if merged.get('min_call_hours') and minutes < merged['min_call_hours'] * 60:
        raise ValueError('combined_call_below_min_call')
    removed = {task['id'] for task in tagged}
    new_id = t.get('task_id') or target + '-task'
    if new_id in {task['id'] for task in spec.get('tasks', [])} - removed:
        raise ValueError('task_id_exists')
    combined = {'id': new_id, 'kind': 'shoot', 'minutes': minutes,
                'resources': _dedupe(r for task in tagged for r in task.get('resources', [])),
                'depends_on': _dedupe(d for task in tagged for d in task.get('depends_on', []) if d not in removed),
                'release_at': min(task['release_at'] for task in tagged),
                'deadline': min(task['deadline'] for task in tagged), 'setup_id': target}
    updated['tasks'] = [task for task in updated['tasks'] if task['id'] not in removed] + [combined]
    _rewire_task_dependencies(updated['tasks'], removed, new_id)
    return updated, {}, None, ['combined shoots ' + ', '.join(ids) + ' into one call of ' + str(minutes) + ' minutes']


def _alt_phase_delivery(spec, offer, as_of, t):
    total = offer['deliverables']
    first_count = t.get('first_deliverables')
    if type(first_count) is not int or not 1 <= first_count < total:
        raise ValueError('invalid_first_deliverables')
    first_deadline = t.get('first_deadline')
    try:
        datetime.strptime(first_deadline, '%Y-%m-%d')
    except (ValueError, TypeError):
        raise ValueError('invalid_first_deadline')
    if not as_of <= first_deadline < offer['deadline']:
        raise ValueError('first_deadline_outside_offer_dates')
    task_ids = t.get('first_task_ids')
    tasks = {task['id']: task for task in spec.get('tasks', [])}
    if not isinstance(task_ids, list) or not task_ids or len(set(task_ids)) != len(task_ids) or any(x not in tasks for x in task_ids):
        raise ValueError('invalid_first_task_ids')
    if any(d not in task_ids for x in task_ids for d in tasks[x].get('depends_on', [])):
        raise ValueError('phase_dependency_not_included')
    if any(tasks[x]['deadline'][:10] > first_deadline for x in task_ids):
        raise ValueError('phase_task_deadline_after_first_phase')
    phases = [{'deliverables': first_count, 'deadline': first_deadline, 'task_ids': task_ids},
              {'deliverables': total - first_count, 'deadline': offer['deadline']}]
    _validate_finished_phase(spec, phases[0], total)
    return copy.deepcopy(spec), {}, phases, ['phased delivery: ' + str(first_count) + ' by ' + first_deadline]


def _alt_supplied_footage(spec, offer, as_of, t):
    ids = t['item_ids']
    unit_hours = _nonnegative(t.get('unit_hours'), 'unit_hours')
    if unit_hours <= 0:
        raise ValueError('ingest_unit_hours_required')
    if not isinstance(t.get('basis'), str) or not t['basis'].strip():
        raise ValueError('dated_basis_required')
    setups = {s['id']: s for s in spec.get('setups', [])}
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise ValueError('invalid_item_ids')
    updated = copy.deepcopy(spec)
    by_id = {i['id']: i for i in updated['items']}
    affected = set()
    for item_id in ids:
        item = by_id.get(item_id)
        if not item:
            raise ValueError('item_not_found:' + str(item_id))
        sid = item.get('setup_id')
        if sid not in setups or setups[sid].get('kind') != 'shoot':
            raise ValueError('supplied_footage_requires_shoot_item:' + str(item_id))
        affected.add(sid)
        item['supplied'] = True
        item.pop('setup_id', None)
        item['unit_hours'] = unit_hours
        item['basis'] = t['basis']
    remaining = {i.get('setup_id') for i in updated['items']}
    removed_setups = {s for s in affected if s not in remaining}
    updated['setups'] = [s for s in updated['setups'] if s['id'] not in removed_setups]
    removed_tasks = {task['id'] for task in updated['tasks'] if task.get('setup_id') in removed_setups}
    updated['tasks'] = [task for task in updated['tasks'] if task['id'] not in removed_tasks]
    _rewire_task_dependencies(updated['tasks'], removed_tasks)
    override = {}
    if 'remove_expenses' in t:
        descriptions = t['remove_expenses']
        if not isinstance(descriptions, list) or any(not isinstance(d, str) for d in descriptions):
            raise ValueError('invalid_remove_expenses')
        updated['expenses'] = [e for e in updated.get('expenses', []) if e.get('description') not in descriptions]
        override['direct_costs_sgd'] = sum(e['cost_cents'] for e in updated['expenses']) / 100
    return updated, override, None, ['marked ' + ', '.join(ids) + ' as client-supplied footage']


def generate_scope_alternatives(spec, offer, as_of, transformations):
    """Build consistent alternative scopes from producer-supplied transformations.

    Structural opportunities are detected automatically; effort values and
    production judgments must be supplied with a dated basis. Schedule blocks
    are rebalanced mechanically to cover the transformed work lines. Each
    accepted variant carries a ready-to-price 'variant' object; rejected
    candidates carry their reason. Nothing here invents effort.
    """
    if not isinstance(transformations, list) or not transformations:
        raise ValueError('transformations_required')
    base_estimate = detailed_estimate(spec)
    opportunities = scope_opportunities(spec, offer)
    handlers = {'master_to_derivative': _alt_master_to_derivative,
                'combine_shoots': _alt_combine_shoots,
                'phase_delivery': _alt_phase_delivery,
                'use_supplied_footage': _alt_supplied_footage}
    allowed_overrides = {'deliverables', 'caption_versions', 'deadline', 'budget_sgd', 'direct_costs_sgd'}
    alternatives, rejected = [], []
    for index, t in enumerate(transformations):
        kind = t.get('kind') if isinstance(t, dict) else None
        label = str(t.get('label') or kind or 'variant') + '-' + str(index + 1) if isinstance(t, dict) else 'variant-' + str(index + 1)
        try:
            if kind not in handlers:
                raise ValueError('unknown_transformation_kind')
            if len(alternatives) >= 4:
                raise ValueError('alternative_limit_reached')
            new_spec, override, phases, changes = handlers[kind](spec, offer, as_of, t)
            if set(override) - allowed_overrides:
                raise ValueError('invalid_offer_override')
            effective_offer = {**offer, **override}
            if kind == 'phase_delivery':
                estimate = detailed_estimate(new_spec)
            else:
                estimate = _rebalance_task_coverage(new_spec)
                _validate_task_coverage(new_spec, estimate, new_spec['tasks'])
            for task in new_spec['tasks']:
                if _dt(task['release_at']).date() < datetime.strptime(as_of, '%Y-%m-%d').date() or \
                   _dt(task['deadline']).date() > datetime.strptime(effective_offer['deadline'], '%Y-%m-%d').date():
                    raise ValueError('task_outside_offer_dates')
            variant = {'offer': override, 'spec': new_spec}
            if phases:
                variant['phases'] = phases
            alternatives.append({'label': label, 'kind': kind, 'changes': changes,
                                 'planning_hours': estimate['planning_hours'],
                                 'planning_hours_delta': round(estimate['planning_hours'] - base_estimate['planning_hours'], 2),
                                 'variant': variant})
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            rejected.append({'label': label, 'kind': kind, 'reason': str(exc)})
    return {'opportunities': opportunities, 'alternatives': alternatives, 'rejected': rejected,
            'base_planning_hours': base_estimate['planning_hours']}


def backtest(cases):
    """Summarize precomputed, quote-time predictions for held-out projects."""
    if not isinstance(cases, list) or not cases:
        raise ValueError('cases_required')
    groups = {}
    seen = set()
    for c in cases:
        if not c.get('project_id') or c['project_id'] in seen:
            raise ValueError('unique_project_ids_required')
        seen.add(c['project_id'])
        actual = _nonnegative(c.get('actual_hours'), 'actual_hours')
        predicted = _nonnegative(c.get('predicted_hours'), 'predicted_hours')
        group = c.get('group', 'unclassified')
        groups.setdefault(group, []).append((c, predicted - actual))
    def summarize(rows):
        errors = [error for _, error in rows]
        cost_errors = [abs(c['predicted_cost_cents'] - c['actual_cost_cents']) for c, _ in rows
                       if c.get('predicted_cost_cents') is not None and c.get('actual_cost_cents') is not None]
        return {'count': len(rows), 'median_absolute_hours_error': statistics.median(abs(x) for x in errors),
                'underestimated_share': sum(x < 0 for x in errors) / len(rows),
                'worst_underestimate_hours': max(0, -min(errors)),
                'median_absolute_cost_error_cents': statistics.median(cost_errors) if cost_errors else None}
    all_rows = [row for rows in groups.values() for row in rows]
    return {'overall': summarize(all_rows), 'groups': {k: summarize(v) for k, v in groups.items()}}


def dispatch_get(handler, path):
    if path == '/api/estimating/rates':
        row = handler.conn.execute('SELECT version, rates_json FROM estimate_rate_versions ORDER BY rowid DESC LIMIT 1').fetchone()
        handler.send_json({'ok': True, 'version': row['version'] if row else None,
                           'rates': json.loads(row['rates_json']) if row else []})
        return True
    if path == '/api/estimating/pending':
        import core
        handler.send_json({'ok': True, 'studio_resumes': core.studio_pending(handler.conn)})
        return True
    if path == '/estimating':
        handler.send_html(estimating_page())
        return True
    if path == '/api/estimating/history':
        handler.send_json({'ok': True, 'history': get_history(handler.conn)})
        return True
    return False


def dispatch_post(handler, path, data):
    if path == '/api/estimating/resume':
        import core
        try:
            evaluation, version = core.evaluate(handler.conn, data['deal_id'], data['offer_id'])
            handler.send_json({'ok': True, 'evaluation_id': evaluation, 'version_id': version})
        except ValueError as exc:
            handler.conn.rollback()
            handler.send_json({'ok': False, 'error': str(exc)}, 400)
        return True
    operations = {
        '/api/estimating/promo-template': lambda: save_promo_template(handler.conn, data),
        '/api/estimating/history': lambda: add_past_project(handler.conn, data['project']),
        '/api/estimating/rates': lambda: save_rate_version(handler.conn, data['version'], data['rates']),
        '/api/estimating/preview': lambda: detailed_estimate(data['spec']),
        '/api/estimating/estimate': lambda: save_estimate(handler.conn, data['deal_id'], data['scope_revision_id'], data['spec']),
        '/api/estimating/availability': lambda: add_availability(handler.conn, data['resource'], data['start_at'], data['end_at']),
        '/api/estimating/schedule': lambda: schedule(handler.conn, data['tasks']),
        '/api/estimating/backtest': lambda: backtest(data['cases']),
        '/api/estimating/merge-setups': lambda: {'spec': merge_setups(data['spec'], data['source_setup_ids'], data['target_setup_id'])},
        '/api/estimating/split-setup': lambda: {'spec': split_setup(data['spec'], data['setup_id'], data['groups'])},
        '/api/estimating/alternatives': lambda: generate_scope_alternatives(data['spec'], data['offer'], data['as_of'], data['transformations']),
        '/api/estimating/overrides': lambda: save_producer_override(handler.conn, data),
        '/api/estimating/apply-overrides': lambda: dict(zip(('spec', 'applied'), apply_producer_overrides(handler.conn, data['deal_id'], data['scope_revision_id'], data['spec']))),
    }
    if path not in operations:
        return False
    try:
        result = operations[path]()
        handler.conn.commit()
        if path in ('/api/estimating/promo-template', '/api/estimating/availability', '/api/estimating/rates'):
            import core
            result['studio_resumes'] = core.studio_pending(handler.conn)
        handler.send_json({'ok': True, **result})
    except (ValueError, KeyError, TypeError) as exc:
        handler.conn.rollback()
        handler.send_json({'ok': False, 'error': str(exc)}, 400)
    return True


def _estimate_scope(conn, args):
    spec = args['spec']
    applied = []
    if args.get('apply_overrides'):
        spec, applied = apply_producer_overrides(conn, args['deal_id'], args['scope_revision_id'], spec)
    result = save_estimate(conn, args['deal_id'], args['scope_revision_id'], spec)
    if applied:
        result['applied_overrides'] = applied
    return result


MCP = {
    'estimate_scope': ({'deal_id': {'type': 'string'}, 'scope_revision_id': {'type': 'string'}, 'spec': {'type': 'object'},
                        'apply_overrides': {'type': 'boolean'}}, ['deal_id', 'scope_revision_id', 'spec'],
                       _estimate_scope),
    'check_resource_schedule': ({'tasks': {'type': 'array', 'items': {'type': 'object'}}}, ['tasks'],
                                lambda conn, args: schedule(conn, args['tasks'])),
    'save_producer_override': ({'deal_id': {'type': 'string'}, 'scope_revision_id': {'type': 'string'},
                                'item_id': {'type': 'string'}, 'field': {'type': 'string'},
                                'value': {'type': 'number'}, 'basis_date': {'type': 'string'},
                                'note': {'type': 'string'}, 'request_id': {'type': 'string'},
                                'supersedes': {'type': 'string'}},
                               ['deal_id', 'scope_revision_id', 'item_id', 'field', 'value', 'basis_date', 'note', 'request_id'],
                               save_producer_override),
    'merge_scope_setups': ({'spec': {'type': 'object'}, 'source_setup_ids': {'type': 'array'},
                            'target_setup_id': {'type': 'string'}},
                           ['spec', 'source_setup_ids', 'target_setup_id'],
                           lambda conn, args: {'spec': merge_setups(args['spec'], args['source_setup_ids'], args['target_setup_id'])}),
    'split_scope_setup': ({'spec': {'type': 'object'}, 'setup_id': {'type': 'string'},
                           'groups': {'type': 'array'}},
                          ['spec', 'setup_id', 'groups'],
                          lambda conn, args: {'spec': split_setup(args['spec'], args['setup_id'], args['groups'])}),
    'propose_scope_alternatives': ({'spec': {'type': 'object'}, 'offer': {'type': 'object'},
                                    'as_of': {'type': 'string'}, 'transformations': {'type': 'array'}},
                                   ['spec', 'offer', 'as_of', 'transformations'],
                                   lambda conn, args: generate_scope_alternatives(
                                       args['spec'], args['offer'], args['as_of'], args['transformations'])),
}


def estimating_page():
    page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Detailed estimate · BidPilot</title><style>body{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;color:#202a35}label{display:block;margin:1rem 0}input,textarea{box-sizing:border-box;display:block;width:100%;padding:.6rem;font:inherit;border:1px solid #9aa8b6;border-radius:4px}textarea{height:22rem;font:14px monospace}button{background:#184c61;color:white;border:0;border-radius:4px;padding:.7rem 1rem;cursor:pointer}pre{white-space:pre-wrap;background:#eef3f5;padding:1rem}a{color:#184c61}</style></head><body><a href="/dashboard">← Dashboard</a><h1>Detailed estimate</h1><p>Enter work once per shared task. Amounts are in Singapore cents; hours are per unit. The preview is internal and requires founder approval before a proposal is released.</p><label>Deal ID<input id="deal"></label><label>Scope revision ID<input id="revision"></label><label>Estimate specification<textarea id="spec">{
  "rate_version": "studio-v1", "buffer_pct": 20, "floor_rate_cents": 7000,
  "items": [
    {"id":"planning", "role":"producer", "basis":"producer override", "quantity":1, "unit_hours":3, "cost_rate_cents":4500, "sell_rate_cents":9000},
    {"id":"shoot", "role":"producer", "basis":"producer override", "quantity":1, "unit_hours":5, "cost_rate_cents":4500, "sell_rate_cents":9000},
    {"id":"masters", "role":"editor", "basis":"producer override", "quantity":3, "unit_hours":4, "cost_rate_cents":4500, "sell_rate_cents":9000}
  ], "expenses": []
}</textarea></label><button id="preview">Preview</button> <button id="save">Save estimate</button><h2>Historical evidence</h2><button id="history">Show historical projects</button><label>Completed project facts<textarea id="project">{"client":"Example client","format":"product_promo","production_mode":"one_shoot_day","shoot_days":1,"locations":1,"scripting":"light","deliverables":3,"editing_level":"standard","graphics":"basic_titles","revision_rounds":2,"caption_versions":0,"actual_hours":29,"price_sgd":2550,"end":"2025-08-22"}</textarea></label><button id="addhistory">Add completed project</button><h2>Resource availability</h2><label>Resource<input id="resource" placeholder="producer"></label><label>Available from<input id="start" type="datetime-local"></label><label>Available until<input id="end" type="datetime-local"></label><button id="availability">Add availability</button><h2>Task schedule</h2><p>Specify tasks with IDs, minutes, resource names, release and deadline. Set kind to shoot for a contiguous slot; use depends_on for prerequisites.</p><textarea id="tasks">[{"id":"shoot","kind":"shoot","minutes":300,"resources":["producer"],"release_at":"2026-11-23T09:00","deadline":"2026-11-23T17:00"}]</textarea><button id="schedule">Check schedule</button><h2>Rate version</h2><label>Version<input id="rateversion" placeholder="studio-v1"></label><label>Rates (cents per hour)<textarea id="rates">[{"role":"producer","cost_rate_cents":4500,"sell_rate_cents":9000}]</textarea></label><button id="ratesave">Save rate version</button><p id="status" role="status"></p><pre id="result"></pre><script>
async function run(save){let spec;try{spec=JSON.parse(document.querySelector('#spec').value)}catch(e){document.querySelector('#status').textContent='Check the specification JSON: '+e.message;return}let payload=save?{deal_id:document.querySelector('#deal').value,scope_revision_id:document.querySelector('#revision').value,spec}: {spec};try{let response=await fetch('/api/estimating/'+(save?'estimate':'preview'),{method:'POST',headers:{'Content-Type':'application/json','X-Owner-Token':new URLSearchParams(location.search).get('owner')||''},body:JSON.stringify(payload)});let result=await response.json();document.querySelector('#status').textContent=result.ok?(save?'Estimate saved.':'Preview ready.'):'Unable to calculate.';document.querySelector('#result').textContent=JSON.stringify(result,null,2)}catch(e){document.querySelector('#status').textContent='Request failed: '+e.message}}async function action(path,payload){try{let response=await fetch('/api/estimating/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Owner-Token':new URLSearchParams(location.search).get('owner')||''},body:JSON.stringify(payload)});let result=await response.json();document.querySelector('#status').textContent=result.ok?'Saved or checked.':'Unable to complete request.';document.querySelector('#result').textContent=JSON.stringify(result,null,2)}catch(e){document.querySelector('#status').textContent=e.message}}document.querySelector('#addhistory').onclick=()=>{try{action('history',{project:JSON.parse(document.querySelector('#project').value)})}catch(e){document.querySelector('#status').textContent=e.message}};document.querySelector('#history').onclick=async()=>{let response=await fetch('/api/estimating/history?owner='+encodeURIComponent(new URLSearchParams(location.search).get('owner')||''));document.querySelector('#result').textContent=JSON.stringify(await response.json(),null,2)};document.querySelector('#availability').onclick=()=>action('availability',{resource:document.querySelector('#resource').value,start_at:document.querySelector('#start').value,end_at:document.querySelector('#end').value});document.querySelector('#schedule').onclick=()=>{try{action('schedule',{tasks:JSON.parse(document.querySelector('#tasks').value)})}catch(e){document.querySelector('#status').textContent=e.message}};document.querySelector('#ratesave').onclick=()=>{try{action('rates',{version:document.querySelector('#rateversion').value,rates:JSON.parse(document.querySelector('#rates').value)})}catch(e){document.querySelector('#status').textContent=e.message}};document.querySelector('#preview').onclick=()=>run(false);document.querySelector('#save').onclick=()=>run(true);
const owner=new URLSearchParams(location.search).get('owner')||'';
const resumePanel=document.createElement('section');
resumePanel.innerHTML='<h2>Resolved studio inputs</h2><button id="refresh-resumes">Refresh resumable deals</button><div id="resume-list"></div>';
document.querySelector('#status').before(resumePanel);
async function refreshResumes(){
 const response=await fetch('/api/estimating/pending?owner='+encodeURIComponent(owner));
 const result=await response.json();const list=document.querySelector('#resume-list');list.replaceChildren();
 for(const item of result.studio_resumes||[]){
  const button=document.createElement('button');button.textContent='Resume deal '+item.deal_id;
  button.onclick=async()=>{await action('resume',item);await refreshResumes()};list.append(button);
 }
 if(!list.childNodes.length)list.textContent='No changed studio inputs awaiting resumption.';
}
document.querySelector('#refresh-resumes').onclick=refreshResumes;
refreshResumes();
document.querySelector('a').href='/dashboard?owner='+encodeURIComponent(owner);</script></body></html>'''
    style = '''<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&display=swap"><style>
body{font:14px/1.5 'Instrument Sans',system-ui,sans-serif;max-width:1180px;margin:0 auto;padding:24px;color:#17201d;background:#f2f4f3}h1{font-size:26px;margin:8px 0 0}a{color:#0e6b5b}button{background:#0e6b5b;border-radius:6px;font-size:13px;font-weight:600}input,textarea{border:1px solid #dde2df;border-radius:6px;background:white}details,.rates-panel{background:white;border:1px solid #dde2df;border-radius:10px;padding:16px;margin-top:20px}summary{cursor:pointer;font-weight:600}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:8px;border-bottom:1px solid #dde2df}th{color:#5d6a65}td input{width:130px;margin:0}.muted{color:#5d6a65}.rates-panel input{padding:7px}.rates-panel button{margin-top:12px}@media(max-width:650px){.rates-panel{overflow-x:auto}td input{width:95px}}
</style>'''
    intro = '''<h1>Production estimates</h1><p class="muted">Your studio rates. New estimates use a saved rate version; earlier proposals keep their reviewed terms.</p><section class="rates-panel"><h2>Studio rates</h2><p class="muted">Hourly production rates in Singapore dollars. Work quantities and hours are estimated separately for each job.</p><div id="studio-rates">Loading rates…</div><p id="studio-rates-status" role="status"></p></section><details><summary>Detailed estimating and studio settings</summary><h2>Detailed estimate</h2>'''
    extra_script = '''<script>
const rateToken=new URLSearchParams(location.search).get('owner')||'';
async function loadStudioRates(){const box=document.getElementById('studio-rates');try{const r=await fetch('/api/estimating/rates',{headers:{'X-Owner-Token':rateToken}});const data=await r.json();box.replaceChildren();if(!data.rates.length){box.textContent='No studio rate version has been saved yet. Add one in detailed studio settings.';return}const table=document.createElement('table');table.innerHTML='<thead><tr><th>Role</th><th>Cost per hour (S$)</th><th>Price per hour (S$)</th></tr></thead>';const body=document.createElement('tbody');data.rates.forEach(rate=>{const tr=document.createElement('tr');const role=document.createElement('th');role.textContent=rate.role;tr.append(role);for(const field of ['cost_rate_cents','sell_rate_cents']){const td=document.createElement('td'),input=document.createElement('input');input.type='number';input.min='0';input.step='0.01';input.value=(rate[field]/100).toFixed(2);input.dataset.field=field;input.setAttribute('aria-label',field==='cost_rate_cents'?'Cost per hour for '+rate.role:'Price per hour for '+rate.role);td.append(input);tr.append(td)}tr.dataset.role=rate.role;body.append(tr)});table.append(body);box.append(table);const button=document.createElement('button');button.textContent='Save rate version';button.onclick=async()=>{const inputs=Array.from(body.querySelectorAll('input'));if(inputs.some(input=>input.value.trim()===''||!input.checkValidity())){document.getElementById('studio-rates-status').textContent='Enter a non-negative amount for each rate.';return}const rates=Array.from(body.rows).map(row=>({role:row.dataset.role,cost_rate_cents:Math.round(Number(row.querySelector('[data-field="cost_rate_cents"]').value)*100),sell_rate_cents:Math.round(Number(row.querySelector('[data-field="sell_rate_cents"]').value)*100)}));const res=await fetch('/api/estimating/rates',{method:'POST',headers:{'Content-Type':'application/json','X-Owner-Token':rateToken},body:JSON.stringify({version:'studio-'+crypto.randomUUID(),rates})});const result=await res.json();document.getElementById('studio-rates-status').textContent=result.ok?'New rate version saved. It applies to new estimates.':'Could not save the rates: '+(result.error||'check the values');};box.append(button)}catch(e){box.textContent='Could not load studio rates.'}}
loadStudioRates();</script>'''
    page = page.replace('</head>', style + '</head>', 1).replace('<h1>Detailed estimate</h1>', intro, 1)
    page = page.replace('<script>\nasync function run(save)', '</details><script>\nasync function run(save)', 1)
    return page.replace('</body></html>', extra_script + '</body></html>')
