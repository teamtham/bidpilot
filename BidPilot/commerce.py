"""Version-bound proposal content, additive change orders and closeout records.

All commercial writes are founder-only except acceptance by the booking token.
Change orders add work; replacing or freeing an existing reservation is unsupported.
"""
import hashlib
import html
import json
import math
from datetime import date

import capacity as cap
import core
import db
import portfolio

SCHEMA = """
CREATE TABLE IF NOT EXISTS change_orders (
 id TEXT PRIMARY KEY, deal_id TEXT NOT NULL, base_version_id TEXT NOT NULL,
 base_order_id TEXT, terms_json TEXT NOT NULL, digest TEXT NOT NULL,
 status TEXT NOT NULL, created_at REAL NOT NULL, approved_at REAL,
 accepted_at REAL, confirmed_at REAL, reservation_id TEXT);
CREATE TABLE IF NOT EXISTS priced_request_orders (
 request_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, arguments_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commerce_reference_requests (
 request_id TEXT PRIMARY KEY, option_id TEXT NOT NULL, reference_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS closeouts (
 id TEXT PRIMARY KEY, deal_id TEXT NOT NULL UNIQUE, version_id TEXT NOT NULL,
 scope_json TEXT NOT NULL, actuals_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS task_actuals (
 closeout_id TEXT NOT NULL, task_id TEXT NOT NULL, hours REAL NOT NULL,
 labor_cost_cents INTEGER NOT NULL, note TEXT NOT NULL,
 PRIMARY KEY(closeout_id,task_id));
"""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _version(conn, version_id):
    row = conn.execute('SELECT * FROM proposal_versions WHERE id=?', (version_id,)).fetchone()
    if not row:
        raise ValueError('version_not_found')
    return row


def _latest_order(conn, deal_id, version_id=None):
    if version_id is not None:
        return conn.execute("SELECT * FROM change_orders WHERE deal_id=? AND base_version_id=? AND status='confirmed' ORDER BY rowid DESC LIMIT 1", (deal_id, version_id)).fetchone()
    return conn.execute("SELECT * FROM change_orders WHERE deal_id=? AND status='confirmed' ORDER BY rowid DESC LIMIT 1", (deal_id,)).fetchone()


def _page(value):
    if not isinstance(value, dict) or set(value) - {'headline', 'introduction', 'sections', 'portfolio'}:
        raise ValueError('invalid_page')
    result = {}
    for key in ('headline', 'introduction'):
        v = value.get(key, '')
        if not isinstance(v, str) or len(v) > 4000:
            raise ValueError('invalid_page')
        result[key] = v
    for key in ('sections', 'portfolio'):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 20:
            raise ValueError('invalid_page')
        result[key] = []
        allowed = {'title', 'body'} if key == 'sections' else {'title', 'description', 'asset_id'}
        for item in items:
            if not isinstance(item, dict) or set(item) - allowed or any(not isinstance(x, str) or len(x) > 2000 for x in item.values()):
                raise ValueError('invalid_page')
            result[key].append(item)
    return result


def page_text(version):
    raw = version['page_json'] if 'page_json' in version.keys() else None
    if not raw:
        return ''
    page = _page(json.loads(raw))
    return '\n'.join([page['headline'], page['introduction']] + [v for k in ('sections', 'portfolio') for item in page[k] for field, v in item.items() if field != 'asset_id'])


def save_page(conn, version_id, page):
    """Create a fresh version so a reviewed page can never be silently edited."""
    value = _page(page)
    conn.execute('BEGIN IMMEDIATE')
    try:
        v = _version(conn, version_id)
        page_hash = _hash(value)
        prior = conn.execute('SELECT * FROM proposal_compositions WHERE source_version_id=? OR version_id=?', (version_id, version_id)).fetchone()
        if prior:
            if prior['page_hash'] != page_hash:
                raise ValueError('composition_conflict: edit the draft before replacing its page')
            conn.commit()
            return {'version_id': prior['version_id'], 'resumed': True}
        if any(not portfolio.public_asset(conn, item.get('asset_id', '')) for item in value['portfolio']):
            raise ValueError('public_portfolio_asset_required')
        latest = conn.execute('SELECT MAX(version_no) FROM proposal_versions WHERE deal_id=?', (v['deal_id'],)).fetchone()[0]
        if v['version_no'] != latest or v['status'] not in ('draft', 'checked', 'blocked'):
            raise ValueError('stale_or_uneditable_version')
        new_id = core.save_draft(conn, version_id, v['draft_body'], commit=False)
        conn.execute('UPDATE proposal_versions SET page_json=? WHERE id=?', (_canonical(value), new_id))
        conn.execute('INSERT INTO proposal_compositions VALUES (?,?,?)', (version_id, new_id, page_hash))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'version_id': new_id}


PORTFOLIO_SEARCH_SCRIPT = """<script>
function referenceTargets(){let s=document.getElementById('reference-package'),t=document.getElementById('reference-target'),n=Number(s.selectedOptions[0]?.dataset.count||0);t.replaceChildren();for(let i=1;i<=n;i++)t.add(new Option('Video '+i,String(i)))}
async function choosePortfolio(b){let s=document.getElementById('reference-package'),t=document.getElementById('reference-target'),status=document.getElementById('reference-status');if(!s.value||!t.value){status.textContent='Select a package and deliverable first.';return}b.disabled=true;try{let r=await fetch(location.pathname+'/creative-change',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:crypto.randomUUID(),option_id:s.value,target_deliverable:Number(t.value),asset_id:b.dataset.asset,action:b.dataset.action})});status.textContent=r.ok?'Reference request sent for review.':'Unable to send reference request.'}catch(e){status.textContent='Could not connect. Please retry.'}finally{b.disabled=false}}
let activeSearch='';
function renderPortfolio(assets){let box=document.getElementById('portfolio-results');box.replaceChildren();for(const a of assets){let card=document.createElement('article'),media=document.createElement(a.mime.startsWith('video/')?'video':'img');media.src=a.media_url;media.style.maxWidth='100%';if(media.tagName==='VIDEO'){media.controls=true;media.preload='metadata'}else media.alt=a.title;card.append(media);let title=document.createElement('h3');title.textContent=a.title;card.append(title);let desc=document.createElement('p');desc.textContent=a.description;card.append(desc);if(a.why){let why=document.createElement('p');why.textContent=a.why;card.append(why)}for(const [action,label] of [['style','Use style'],['swap','Swap'],['add','Add']]){let b=document.createElement('button');b.type='button';b.textContent=label;b.dataset.asset=a.id;b.dataset.action=action;b.onclick=()=>choosePortfolio(b);card.append(b)}box.append(card)}}
async function pollPortfolio(id,query,first){try{let r=await fetch(location.pathname+'/portfolio-search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(first?{request_id:id,query}:{request_id:id,poll:true})});let data=await r.json();if(id!==activeSearch)return;if(!r.ok)throw Error();renderPortfolio(data.assets);document.getElementById('portfolio-search-status').textContent=data.status==='pending'?'Showing keyword candidates while the studio assistant reviews your request.':'Studio assistant results ready.';if(data.status==='pending')setTimeout(()=>pollPortfolio(id,query,false),3000)}catch(e){if(id===activeSearch)document.getElementById('portfolio-search-status').textContent='Search unavailable. Please try again.'}}
document.getElementById('portfolio-search').onsubmit=e=>{e.preventDefault();activeSearch=crypto.randomUUID();pollPortfolio(activeSearch,document.getElementById('portfolio-query').value,true)};
</script>"""


def proposal_html(conn, version, preview=False):
    raw = version['page_json'] if 'page_json' in version.keys() else None
    try:
        page = _page(json.loads(raw) if raw else {})
    except (ValueError, TypeError):
        return ''
    esc = lambda s: html.escape(s, quote=True)
    out = ['<section class="proposal-story">']
    if page['headline']:
        out.append('<h2>' + esc(page['headline']) + '</h2>')
    if page['introduction']:
        out.append('<p>' + esc(page['introduction']) + '</p>')
    for section in page['sections']:
        out.append('<section><h3>' + esc(section.get('title', '')) + '</h3><p>' + esc(section.get('body', '')) + '</p></section>')
    if page['portfolio']:
        out.append('<section><h2>Creative references</h2>')
        for item in page['portfolio']:
            asset = portfolio.public_asset(conn, item.get('asset_id', ''))
            out.append('<article><h3>' + esc(item.get('title', '')) + '</h3><p>' + esc(item.get('description', '')) + '</p>')
            if asset:
                src = esc(asset['media_url'])
                out.append('<video controls preload="metadata" src="'+src+'" style="max-width:100%"></video>' if asset['mime'].startswith('video/') else '<img src="'+src+'" alt="'+esc(asset['title'])+'" style="max-width:100%">')
            out.append('</article>')
        out.append('<p>Tell us which style you would like to use, swap or add in your change request.</p></section>')
    out.append('</section>')
    # Public library is independently revocable and does not alter approved
    # commercial terms. Selection only submits a request for review.
    try:
        assets = portfolio.search(conn, '')
    except Exception:
        assets = []
    if assets and version['status'] != 'confirmed':
        option_ids = json.loads(version['public_option_ids_json'] or '[]')
        options = []
        for oid in option_ids:
            row = conn.execute('SELECT public_terms_json FROM options WHERE id=?', (oid,)).fetchone()
            if row:
                terms = json.loads(row['public_terms_json'])
                options.append((oid, terms))
        selector = '<label for="reference-package">Package</label><select id="reference-package" onchange="referenceTargets()"><option value="">Select a package</option>' + ''.join('<option value="'+esc(oid)+'" data-count="'+str(terms['deliverables'])+'">'+esc(terms.get('kind','Option').replace('_',' '))+' · '+str(terms['deliverables'])+' videos</option>' for oid, terms in options) + '</select><label for="reference-target">Deliverable</label><select id="reference-target"></select><p id="reference-status" role="status"></p>'
        out.append('<section><h2>Explore studio work</h2><p>Ask about a style or idea. Examples are creative references; changes require review.</p>'+selector+'<form id="portfolio-search"><label for="portfolio-query">What are you looking for?</label><input id="portfolio-query" required maxlength="2000"><button>Find examples</button></form><p id="portfolio-search-status" role="status"></p><div id="portfolio-results"></div></section>')
        if not preview:
            out.append(PORTFOLIO_SEARCH_SCRIPT)
    elif assets and version['status'] == 'confirmed':
        out.append('<section><h2>Explore studio work</h2><p>Your booking is confirmed. '
                   'Creative changes are unavailable here; request them below as additional work for studio review.</p></section>')
    if version['status'] == 'confirmed':
        confirmation = conn.execute(
            "SELECT option_id FROM confirmations WHERE version_id=?",
            (version['id'],)).fetchone()
        selected_terms = None
        if confirmation:
            selected = conn.execute(
                "SELECT public_terms_json FROM options WHERE id=?",
                (confirmation['option_id'],)).fetchone()
            if selected:
                try:
                    selected_terms = json.loads(selected['public_terms_json'])
                except (TypeError, ValueError):
                    selected_terms = None
        orders = list(conn.execute(
            "SELECT * FROM change_orders WHERE base_version_id=?"
            " AND status IN ('approved','accepted','confirmed') ORDER BY rowid",
            (version['id'],)))
        by_id = {row['id']: row for row in orders if row['status'] == 'confirmed'}
        original_price = selected_terms.get('price_sgd') if isinstance(selected_terms, dict) else None
        original_deadline = selected_terms.get('deadline') if isinstance(selected_terms, dict) else None

        def chain_values(order_id):
            total, deadlines, seen = original_price, [original_deadline], set()
            chain = []
            while order_id and order_id not in seen:
                seen.add(order_id)
                row = by_id.get(order_id)
                if not row:
                    break
                chain.append(row)
                order_id = row['base_order_id']
            for row in reversed(chain):
                try:
                    terms = _terms(json.loads(row['terms_json']))
                except (ValueError, TypeError):
                    continue
                if total is not None:
                    total += terms['price_sgd']
                deadlines.extend(p['deadline'] for p in terms['phases'])
            return total, max((d for d in deadlines if d), default=None)

        latest = _latest_order(conn, version['deal_id'], version['id'])
        committed_total, committed_deadline = chain_values(latest['id'] if latest else None)
        if original_price is not None and original_deadline:
            out.append('<section><h2>Confirmed booking</h2>'
                       '<p>Original selection: S$' + format(original_price, ',.2f') +
                       ' by ' + esc(original_deadline) + '</p>'
                       '<p>Current committed total: S$' + format(committed_total, ',.2f') +
                       ' · current delivery date: ' + esc(committed_deadline) + '</p></section>')
        for order in orders:
            try:
                if order['status'] != 'confirmed':
                    _order(conn, order['id'])
                terms = _terms(json.loads(order['terms_json']))
            except (ValueError, TypeError):
                continue
            out.append('<section><h2>Additional work</h2><p>' + esc(terms['description']) + '</p><p>Additional price: S$' + format(terms['price_sgd'], ',.2f') + '</p><ul>')
            for phase in terms['phases']:
                out.append('<li>Additional work by ' + esc(phase['deadline']) + '</li>')
            out.append('</ul>')
            calc = terms.get('calculation')
            if calc and 'quantity' in calc:
                out.append('<p>Quantity: ' + str(calc['quantity']) + '</p>')
            base_total, base_deadline = chain_values(order['base_order_id'])
            revised_total = base_total + terms['price_sgd'] if base_total is not None else None
            revised_deadline = max([d for d in [base_deadline] +
                                    [p['deadline'] for p in terms['phases']] if d],
                                   default=None)
            if revised_total is not None and revised_deadline:
                label = ('Committed total after this amendment' if order['status'] == 'confirmed'
                         else 'Revised total if accepted')
                out.append('<p>' + label + ': S$' + format(revised_total, ',.2f') +
                           ' · revised delivery date: ' + esc(revised_deadline) + '</p>')
            if order['status'] == 'approved' and not preview:
                out.append('<button type="button" data-order="' + esc(order['id']) + '" onclick="acceptChangeOrder(this)">Accept additional work</button><p role="status" id="order-status-' + esc(order['id']) + '"></p>')
            else:
                out.append('<p>' + ('Awaiting founder confirmation' if order['status'] == 'accepted' else 'Confirmed' if order['status'] == 'confirmed' else 'Preview only') + '</p>')
            out.append('</section>')
        if not preview:
            out.append('<form onsubmit="requestMore(event); return false"><label for="extra-request">Request additional work</label><textarea id="extra-request" required maxlength="4000"></textarea><button>Send request</button><p id="extra-status" role="status"></p></form>')
        out.append("<script>async function requestMore(e){e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{let r=await fetch(location.pathname+'/additional-work',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({body:document.getElementById('extra-request').value,request_id:crypto.randomUUID(),reference:window.bidpilotReference||null})});document.getElementById('extra-status').textContent=r.ok?'Request sent to the studio.':'Unable to send request.';if(!r.ok)b.disabled=false}catch(e){b.disabled=false;document.getElementById('extra-status').textContent='Could not connect. Please retry.'}return false} async function acceptChangeOrder(b){b.disabled=true;try{const r=await fetch(location.pathname+'/change-order/'+encodeURIComponent(b.dataset.order)+'/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});document.getElementById('order-status-'+b.dataset.order).textContent=r.ok?'Accepted; awaiting founder confirmation.':'Unable to accept. Please contact the studio.';if(!r.ok)b.disabled=false}catch(e){b.disabled=false;document.getElementById('order-status-'+b.dataset.order).textContent='Could not connect. Please retry.'}}</script>")
    return ''.join(out)


def _terms(terms):
    if not isinstance(terms, dict) or set(terms) not in ({'description', 'price_sgd', 'as_of', 'phases'}, {'description', 'price_sgd', 'as_of', 'phases', 'calculation'}):
        raise ValueError('invalid_terms')
    desc, price, phases = terms['description'], terms['price_sgd'], terms['phases']
    if not isinstance(desc, str) or not desc.strip() or len(desc) > 3000:
        raise ValueError('invalid_description')
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0 or round(price, 2) != price:
        raise ValueError('invalid_price')
    try:
        as_of = date.fromisoformat(terms['as_of'])
        if not isinstance(phases, list) or not 1 <= len(phases) <= 8:
            raise ValueError()
        previous = as_of
        for p in phases:
            if not isinstance(p, dict) or set(p) != {'deadline', 'hours'}:
                raise ValueError()
            day = date.fromisoformat(p['deadline'])
            h = p['hours']
            if day < previous or isinstance(h, bool) or not isinstance(h, (int, float)) or not math.isfinite(h) or h <= 0 or round(h, 2) != h:
                raise ValueError()
            previous = day
    except (TypeError, ValueError):
        raise ValueError('invalid_phases')
    return terms


def priced_terms(conn, version_id, base_order_id, kind, quantity, as_of, deadline, _frozen=None):
    if kind not in ('extra_location', 'caption_version', 'extra_revision_round') or isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
        raise ValueError('unsupported_change')
    v = _version(conn, version_id)
    latest = _latest_order(conn, v['deal_id'], version_id)
    if (latest['id'] if latest else None) != base_order_id:
        raise ValueError('stale_base')
    confirmation = conn.execute('SELECT option_id FROM confirmations WHERE version_id=?', (version_id,)).fetchone()
    if not confirmation:
        raise ValueError('booking_not_confirmed')
    opt = conn.execute('SELECT public_terms_json FROM options WHERE id=?', (confirmation['option_id'],)).fetchone()
    original = json.loads(opt['public_terms_json'])
    counts = {k: original[k] for k in ('locations', 'caption_versions', 'revision_rounds', 'deliverables', 'shoot_days')}
    for row in conn.execute("SELECT terms_json FROM change_orders WHERE deal_id=? AND base_version_id=? AND status='confirmed' ORDER BY rowid", (v['deal_id'], version_id)):
        calculation = json.loads(row['terms_json']).get('calculation')
        if not calculation:
            raise ValueError('manual_change_requires_review')
        if calculation.get('kind') not in ('extra_location','caption_version','extra_revision_round'):
            raise ValueError('manual_change_requires_review')
        field = {'extra_location':'locations', 'caption_version':'caption_versions', 'extra_revision_round':'revision_rounds'}[calculation['kind']]
        counts[field] += calculation['quantity']
    if kind == 'extra_location' and counts['shoot_days'] == 0:
        raise ValueError('location_requires_shoot')
    if kind == 'caption_version' and counts['caption_versions'] + quantity > counts['deliverables']:
        raise ValueError('captions_exceed_deliverables')
    result = json.loads(conn.execute('SELECT result_json FROM evaluations WHERE offer_id=?', (v['offer_id'],)).fetchone()['result_json'])
    estimate = result.get('estimate') or {}
    if estimate.get('version') == 'detailed-v1' or 'buffer_pct' not in estimate:
        raise ValueError('detailed_change_requires_explicit_estimate_and_schedule')
    buffer_pct = estimate['buffer_pct']
    source = _frozen if _frozen is not None else core.seed_data()
    seed = {k: source[k] for k in ('effort_rules', 'rate_card', 'business_rules')}
    effort = {r['rule']: r['hours'] for r in seed['effort_rules']}
    prices = {r['item']: float(r['price_sgd']) for r in seed['rate_card']}
    rules = {r['key']: float(r['value']) for r in seed['business_rules']}
    effort_key = {'extra_location':'extra_location', 'caption_version':'caption_per_version', 'extra_revision_round':'extra_revision_round'}[kind]
    hours = round(quantity * float(effort[effort_key]) * (1 + buffer_pct / 100), 2)
    price = round(max(quantity * prices[kind], hours * rules['min_hourly_rate_sgd']), 2)
    labels = {'extra_location':'Additional location', 'caption_version':'Additional captioned versions', 'extra_revision_round':'Additional revision rounds'}
    return _terms({'description':labels[kind], 'price_sgd':price, 'as_of':as_of, 'phases':[{'deadline':deadline, 'hours':hours}], 'calculation':{'kind':kind, 'quantity':quantity, 'seed_hash':core.seed_hash(seed), 'buffer_pct':buffer_pct, 'rate_snapshot': seed}})


def _price_order_request(conn, version_id, base_order_id, request_id, arguments, calculate_terms):
    conn.execute('BEGIN IMMEDIATE')
    try:
        args = _canonical(arguments)
        if request_id is not None:
            if not isinstance(request_id, str) or not request_id:
                raise ValueError('invalid_request_id')
            key = request_id if request_id.startswith('additional:') else 'additional:' + request_id
            message = conn.execute("SELECT * FROM messages WHERE request_id=? AND direction='inbound'", (key,)).fetchone()
            if not message or message['version_id'] != version_id:
                raise ValueError('request_not_found')
            previous = conn.execute('SELECT * FROM priced_request_orders WHERE request_id=?', (key,)).fetchone()
            if previous:
                if previous['arguments_json'] != args:
                    raise ValueError('request_id_conflict')
                order = conn.execute('SELECT digest FROM change_orders WHERE id=?', (previous['order_id'],)).fetchone()
                conn.commit()
                return {'order_id': previous['order_id'], 'digest': order['digest']}
        terms = calculate_terms()
        result = _create_order(conn, version_id, base_order_id, terms)
        if request_id is not None:
            conn.execute('INSERT INTO priced_request_orders VALUES (?,?,?)', (key, result['order_id'], args))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def create_priced_order(conn, version_id, base_order_id, kind, quantity, as_of, deadline, request_id=None):
    arguments = dict(version_id=version_id, base_order_id=base_order_id, kind=kind,
                     quantity=quantity, as_of=as_of, deadline=deadline)
    return _price_order_request(conn, version_id, base_order_id, request_id, arguments,
                                lambda: priced_terms(conn, version_id, base_order_id, kind, quantity, as_of, deadline))


def detailed_terms(conn, version_id, spec, tasks, as_of, deadline, description):
    """Price founder-specified incremental work with registered, frozen rates."""
    import estimating
    v = _version(conn, version_id)
    if not conn.execute('SELECT 1 FROM confirmations WHERE version_id=?', (version_id,)).fetchone():
        raise ValueError('booking_not_confirmed')
    estimating.validate_catalog_rates(conn, spec)
    estimate = estimating.detailed_estimate(spec)
    if estimate['selling_price_cents'] < estimate['floor_cents']:
        raise ValueError('selling_price_below_floor')
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('resource_tasks_required')
    start, end = date.fromisoformat(as_of), date.fromisoformat(deadline)
    if end < start:
        raise ValueError('invalid_phases')
    for task in tasks:
        if estimating._dt(task['release_at']).date() < start or estimating._dt(task['deadline']).date() > end:
            raise ValueError('task_outside_order_dates')
    if sum(estimating._nonnegative(t.get('minutes'), 'minutes') for t in tasks) < math.ceil(estimate['planning_hours'] * 60):
        raise ValueError('resource_tasks_must_cover_planning_hours')
    estimating._validate_task_coverage(spec, estimate, tasks)
    if not estimating.schedule(conn, tasks)['feasible']:
        raise ValueError('resource_schedule_conflict')
    return _terms({'description':description, 'price_sgd':estimate['selling_price_cents']/100,
        'as_of':as_of, 'phases':[{'deadline':deadline, 'hours':estimate['planning_hours']}],
        'calculation':{'kind':'detailed', 'spec':spec, 'tasks':tasks}})


def create_detailed_order(conn, version_id, base_order_id, spec, tasks, as_of, deadline, description, request_id=None):
    arguments = dict(version_id=version_id, base_order_id=base_order_id, spec=spec, tasks=tasks,
                     as_of=as_of, deadline=deadline, description=description)
    return _price_order_request(conn, version_id, base_order_id, request_id, arguments,
                                lambda: detailed_terms(conn, version_id, spec, tasks, as_of, deadline, description))


def create_order(conn, version_id, base_order_id, terms):
    conn.execute('BEGIN IMMEDIATE')
    try:
        result = _create_order(conn, version_id, base_order_id, terms)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _create_order(conn, version_id, base_order_id, terms):
    terms = _terms(terms)
    v = _version(conn, version_id)
    if v['status'] != 'confirmed' or core.release_integrity_errors(conn, v):
        raise ValueError('booking_not_current')
    latest = _latest_order(conn, v['deal_id'], version_id)
    if (latest['id'] if latest else None) != base_order_id:
        raise ValueError('stale_base')
    if conn.execute('SELECT 1 FROM closeouts WHERE deal_id=?', (v['deal_id'],)).fetchone():
        raise ValueError('already_closed_out')
    order_id = db.new_id()
    payload = {'id': order_id, 'deal_id': v['deal_id'], 'base_version_id': version_id, 'base_order_id': base_order_id, 'terms': terms}
    conn.execute('INSERT INTO change_orders (id,deal_id,base_version_id,base_order_id,terms_json,digest,status,created_at) VALUES (?,?,?,?,?,?,?,?)',
                 (order_id, v['deal_id'], version_id, base_order_id, _canonical(terms), _hash(payload), 'draft', db.now()))
    return {'order_id': order_id, 'digest': _hash(payload)}


def _order(conn, order_id):
    o = conn.execute('SELECT * FROM change_orders WHERE id=?', (order_id,)).fetchone()
    if not o:
        raise ValueError('order_not_found')
    payload = {'id': o['id'], 'deal_id': o['deal_id'], 'base_version_id': o['base_version_id'], 'base_order_id': o['base_order_id'], 'terms': _terms(json.loads(o['terms_json']))}
    if _hash(payload) != o['digest']:
        raise ValueError('digest_mismatch')
    v = _version(conn, o['base_version_id'])
    if conn.execute('SELECT 1 FROM closeouts WHERE deal_id=?', (o['deal_id'],)).fetchone():
        raise ValueError('already_closed_out')
    latest = _latest_order(conn, o['deal_id'], o['base_version_id'])
    if v['status'] != 'confirmed' or core.release_integrity_errors(conn, v) or (latest['id'] if latest else None) != o['base_order_id']:
        raise ValueError('stale_base')
    terms = payload['terms']
    if 'calculation' in terms:
        calc = terms['calculation']
        try:
            expected = (detailed_terms(conn, o['base_version_id'], calc['spec'], calc['tasks'], terms['as_of'], terms['phases'][0]['deadline'], terms['description'])
                        if calc.get('kind') == 'detailed' else
                        priced_terms(conn, o['base_version_id'], o['base_order_id'], calc['kind'], calc['quantity'], terms['as_of'], terms['phases'][0]['deadline'], calc.get('rate_snapshot')))
        except (KeyError, IndexError, TypeError):
            raise ValueError('calculation_invalid')
        if expected != terms:
            raise ValueError('calculation_changed')
    return o, terms


def approve_order(conn, order_id, reviewed_digest):
    conn.execute('BEGIN IMMEDIATE')
    try:
        o, terms = _order(conn, order_id)
        if o['status'] != 'draft' or reviewed_digest != o['digest']:
            raise ValueError('review_mismatch')
        # Run the standard outbound gate on the supplementary prose. Numeric
        # price and dates are rendered from the structured terms themselves.
        deal = conn.execute('SELECT client FROM deals WHERE id=?', (o['deal_id'],)).fetchone()
        version = _version(conn, o['base_version_id'])
        evaluation = conn.execute('SELECT result_json FROM evaluations WHERE offer_id=?', (version['offer_id'],)).fetchone()
        result = json.loads(evaluation['result_json'])
        estimate = result.get('estimate') or {}
        private = {k: estimate[k] for k in ('floor_sgd', 'capacity_hours') if k in estimate}
        private['min_hourly_rate_sgd'] = 70
        for key, metric in (result.get('option_metrics') or {}).items():
            if metric and 'floor_sgd' in metric:
                private['opt_' + key + '_floor'] = metric['floor_sgd']
        check = core.run_outbound_check({'draft': terms['description'], 'approved_options': [], 'private_values': private, 'other_client_names': core.other_client_names(conn, deal['client'])})
        if not check['passed']:
            raise ValueError('outbound_blocked')
        conn.execute("UPDATE change_orders SET status='approved', approved_at=? WHERE id=?", (db.now(), order_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'ok': True}


def creative_change(conn, token, data):
    if not isinstance(data, dict) or set(data) != {'request_id','option_id','target_deliverable','asset_id','action'}:
        raise ValueError('invalid_request')
    request_id = data['request_id']
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100:
        raise ValueError('invalid_request_id')
    conn.execute('BEGIN IMMEDIATE')
    try:
        v = core.published_by_token(conn, token)
        if not v or core.release_integrity_errors(conn, v):
            raise ValueError('not_found')
        option_id = data['option_id']
        if option_id not in json.loads(v['public_option_ids_json'] or '[]'):
            raise ValueError('invalid_option')
        if v['status'] == 'confirmed':
            selected = conn.execute('SELECT option_id FROM confirmations WHERE version_id=?', (v['id'],)).fetchone()
            if not selected or selected['option_id'] != option_id:
                raise ValueError('invalid_option')
        option = conn.execute('SELECT public_terms_json FROM options WHERE id=?', (option_id,)).fetchone()
        if not option:
            raise ValueError('invalid_option')
        target = data['target_deliverable']
        if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= json.loads(option['public_terms_json'])['deliverables']:
            raise ValueError('invalid_reference_target')
        asset = portfolio.public_asset(conn, data['asset_id']) if isinstance(data['asset_id'], str) else None
        if not asset or data['action'] not in ('style','swap','add'):
            raise ValueError('invalid_reference')
        key = 'creative:' + request_id
        payload = _canonical(data)
        existing = conn.execute('SELECT * FROM commerce_reference_requests WHERE request_id=?', (key,)).fetchone()
        if existing:
            if existing['option_id'] != option_id or existing['reference_json'] != payload:
                raise ValueError('request_id_conflict')
        else:
            body = 'Creative reference request: ' + data['action'] + ' ' + asset['title'] + ' for video ' + str(target)
            conn.execute('INSERT INTO messages (id,deal_id,version_id,direction,body,option_ids_json,request_id,created_at) VALUES (?,?,?,?,?,?,?,?)', (db.new_id(),v['deal_id'],v['id'],'inbound',body,db.j([option_id]),key,db.now()))
            conn.execute('INSERT INTO commerce_reference_requests VALUES (?,?,?)', (key,option_id,payload))
        conn.commit()
        return {'ok': True}
    except Exception:
        conn.rollback()
        raise


def request_change(conn, token, body, request_id, reference=None):
    if not isinstance(body, str) or not body.strip() or len(body) > 4000 or not isinstance(request_id, str) or not request_id or len(request_id) > 100:
        raise ValueError('invalid_request')
    conn.execute('BEGIN IMMEDIATE')
    try:
        v = core.published_by_token(conn, token)
        if not v or v['status'] != 'confirmed' or core.release_integrity_errors(conn, v):
            raise ValueError('not_found')
        key = 'additional:' + request_id
        selected = conn.execute('SELECT option_id FROM confirmations WHERE version_id=?', (v['id'],)).fetchone()
        if reference is not None:
            if not isinstance(reference, dict) or set(reference) != {'action','reference','target_deliverable'} or reference['action'] not in ('style','swap','add') or not isinstance(reference['reference'], str) or not 0 < len(reference['reference']) <= 200 or not selected:
                raise ValueError('invalid_reference')
            terms = conn.execute('SELECT public_terms_json FROM options WHERE id=?', (selected['option_id'],)).fetchone()
            target = reference['target_deliverable']
            if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= json.loads(terms['public_terms_json'])['deliverables']:
                raise ValueError('invalid_reference_target')
        old = conn.execute('SELECT * FROM messages WHERE request_id=?', (key,)).fetchone()
        if old:
            saved = conn.execute('SELECT * FROM commerce_reference_requests WHERE request_id=?', (key,)).fetchone()
            if old['version_id'] != v['id'] or old['body'] != body or (saved['reference_json'] if saved else None) != (_canonical(reference) if reference is not None else None):
                raise ValueError('request_id_conflict')
        else:
            conn.execute('INSERT INTO messages (id,deal_id,version_id,direction,body,option_ids_json,request_id,created_at) VALUES (?,?,?,?,?,?,?,?)', (db.new_id(),v['deal_id'],v['id'],'inbound',body,db.j([selected['option_id']]) if selected else '[]',key,db.now()))
            if reference is not None:
                conn.execute('INSERT INTO commerce_reference_requests VALUES (?,?,?)', (key, selected['option_id'], _canonical(reference)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'ok':True}


def accept_order(conn, token, order_id):
    conn.execute('BEGIN IMMEDIATE')
    try:
        v = core.published_by_token(conn, token)
        existing = conn.execute('SELECT * FROM change_orders WHERE id=?', (order_id,)).fetchone()
        if v and existing and v['id'] == existing['base_version_id'] and existing['status'] in ('accepted', 'confirmed'):
            conn.commit()
            return {'ok': True, 'pending': existing['status'] == 'accepted'}
        o, _ = _order(conn, order_id)
        if not v or v['id'] != o['base_version_id'] or o['status'] != 'approved':
            raise ValueError('not_found')
        conn.execute("UPDATE change_orders SET status='accepted',accepted_at=? WHERE id=?", (db.now(), order_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'ok': True, 'pending': True}


def confirm_order(conn, order_id):
    conn.execute('BEGIN IMMEDIATE')
    try:
        existing = conn.execute('SELECT * FROM change_orders WHERE id=?', (order_id,)).fetchone()
        if existing and existing['status'] == 'confirmed':
            reservation = conn.execute('SELECT capacity_revision FROM reservations WHERE id=?', (existing['reservation_id'],)).fetchone()
            if not reservation:
                raise ValueError('missing_reservation')
            conn.commit()
            return {'ok': True, 'reservation_id': existing['reservation_id'], 'capacity_revision': reservation['capacity_revision']}
        o, terms = _order(conn, order_id)
        if o['status'] != 'accepted':
            raise ValueError('acceptance_required')
        demands = [(int(round(p['hours'] * 100)), p['deadline']) for p in terms['phases']]
        effective_as_of = max(terms['as_of'], date.today().isoformat())
        ledger = cap.ledger(conn, as_of=effective_as_of, deadline=terms['phases'][-1]['deadline'])
        allocation = cap.plan(ledger, demands)
        if allocation is None:
            raise ValueError('capacity_changed')
        now, rev, rid = db.now(), cap.ledger_revision(conn) + 1, db.new_id()
        if terms.get('calculation', {}).get('kind') == 'detailed':
            import estimating
            estimating.reserve_schedule(conn, rid, terms['calculation']['tasks'])
        for week, amount, _ in allocation:
            ledger[week]['allocated'] += amount
            ledger[week]['residual'] -= amount
        conn.execute('INSERT INTO capacity_revisions (revision,seed_hash,snapshot_json,created_at) VALUES (?,?,?,?)', (rev, core.seed_hash(core.seed_data()), db.j({'as_of': effective_as_of, 'revision': rev, 'weeks': [{'week': w, **s} for w, s in sorted(ledger.items())]}), now))
        # Reservation points to the confirmed booking, but uses a distinct
        # synthetic version key to satisfy the ledger's uniqueness constraint.
        conn.execute('INSERT INTO reservations (id,deal_id,version_id,option_id,confirmation_id,capacity_revision,status,total_planning_hundredths,created_at) VALUES (?,?,?,?,?,?,?,?,?)', (rid, o['deal_id'], 'change:' + order_id, 'change:' + order_id, 'change:' + order_id, rev, 'active', sum(n for n, _ in demands), now))
        for week, amount, milestone in allocation:
            conn.execute('INSERT INTO allocations (id,reservation_id,week_ending,planning_hundredths,milestone,created_at) VALUES (?,?,?,?,?,?)', (db.new_id(), rid, week, amount, milestone, now))
        conn.execute("UPDATE change_orders SET status='confirmed',confirmed_at=?,reservation_id=? WHERE id=?", (now, rid, order_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {'ok': True, 'reservation_id': rid, 'capacity_revision': rev}


def closeout(conn, version_id, actuals):
    """Record producer-verified actuals atomically with the selected scope."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        result = _closeout(conn, version_id, actuals)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _closeout(conn, version_id, actuals):
    v = _version(conn, version_id)
    if v['status'] != 'confirmed':
        raise ValueError('booking_not_confirmed')
    required = {'actual_hours', 'actual_cost_sgd', 'end', 'notes', 'reliable'}
    if not isinstance(actuals, dict) or not required <= set(actuals) or set(actuals) - required - {'task_actuals','actual_labor_cost_sgd'}:
        raise ValueError('invalid_actuals')
    for key in ('actual_hours', 'actual_cost_sgd'):
        val = actuals[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0:
            raise ValueError('invalid_actuals')
    if not isinstance(actuals['notes'], str) or len(actuals['notes']) > 5000 or not isinstance(actuals['reliable'], bool):
        raise ValueError('invalid_actuals')
    try:
        date.fromisoformat(actuals['end'])
    except (TypeError, ValueError):
        raise ValueError('invalid_actuals')
    if conn.execute("SELECT 1 FROM change_orders WHERE deal_id=? AND status IN ('approved','accepted')", (v['deal_id'],)).fetchone():
        raise ValueError('pending_change_order')
    offer = conn.execute('SELECT offer_json FROM offers WHERE id=?', (v['offer_id'],)).fetchone()
    selected = conn.execute('SELECT option_id FROM confirmations WHERE version_id=?', (version_id,)).fetchone()
    if not offer or not selected:
        raise ValueError('missing_booking')
    option = conn.execute('SELECT public_terms_json FROM options WHERE id=?', (selected['option_id'],)).fetchone()
    scope = {'offer': json.loads(offer['offer_json']), 'selected_terms': json.loads(option['public_terms_json']),
             'change_orders': [json.loads(r['terms_json']) for r in conn.execute("SELECT terms_json FROM change_orders WHERE deal_id=? AND base_version_id=? AND status='confirmed' ORDER BY rowid", (v['deal_id'], version_id))]}
    task_actuals = actuals.get('task_actuals')
    if task_actuals is not None:
        if scope['change_orders']:
            raise ValueError('task_actuals_with_changes_unsupported')
        evaluation = conn.execute('SELECT result_json FROM evaluations WHERE offer_id=?', (v['offer_id'],)).fetchone()
        metrics = json.loads(evaluation['result_json']).get('option_metrics', {}) if evaluation else {}
        tasks = metrics.get(scope['selected_terms'].get('option_id'), {}).get('detailed_tasks')
        if not isinstance(tasks, list) or not tasks or not isinstance(task_actuals, list):
            raise ValueError('detailed_booking_required')
        expected = {t['id'] for t in tasks}
        seen = set()
        for task in task_actuals:
            if not isinstance(task, dict) or set(task) != {'task_id','hours','labor_cost_cents','note'} or task['task_id'] not in expected or task['task_id'] in seen:
                raise ValueError('invalid_task_actuals')
            hours, cents = task['hours'], task['labor_cost_cents']
            if isinstance(hours, bool) or not isinstance(hours, (int,float)) or not math.isfinite(hours) or hours < 0 or type(cents) is not int or cents < 0 or not isinstance(task['note'], str) or len(task['note']) > 2000:
                raise ValueError('invalid_task_actuals')
            seen.add(task['task_id'])
        labor = actuals.get('actual_labor_cost_sgd')
        if seen != expected or isinstance(labor,bool) or not isinstance(labor,(int,float)) or not math.isfinite(labor) or labor < 0 or abs(sum(t['hours'] for t in task_actuals)-actuals['actual_hours']) > 0.000001 or abs(sum(t['labor_cost_cents'] for t in task_actuals)-labor*100) > 0.000001:
            raise ValueError('task_actual_totals_mismatch')
    elif 'actual_labor_cost_sgd' in actuals:
        raise ValueError('task_actuals_required')
    if conn.execute('SELECT 1 FROM closeouts WHERE deal_id=?', (v['deal_id'],)).fetchone():
        raise ValueError('already_closed_out')
    closeout_id = db.new_id()
    conn.execute('INSERT INTO closeouts (id,deal_id,version_id,scope_json,actuals_json,created_at) VALUES (?,?,?,?,?,?)', (closeout_id, v['deal_id'], version_id, _canonical(scope), _canonical(actuals), db.now()))
    for task in task_actuals or []:
        conn.execute('INSERT INTO task_actuals VALUES (?,?,?,?,?)', (closeout_id,task['task_id'],task['hours'],task['labor_cost_cents'],task['note']))
    return {'ok': True}


def get_history(conn):
    result = []
    for row in conn.execute('SELECT * FROM closeouts ORDER BY created_at'):
        actuals, snapshot = json.loads(row['actuals_json']), json.loads(row['scope_json'])
        # Changed work cannot be attributed to the original quote-time scope.
        if not actuals['reliable']:
            continue
        offer, terms = snapshot['offer'], snapshot['selected_terms']
        if isinstance(offer.get('offer'), dict):
            offer = offer['offer']
        keys = ('format', 'production_mode', 'shoot_days', 'locations', 'scripting', 'editing_level', 'graphics', 'revision_rounds', 'caption_versions')
        item = {k: offer.get(k) for k in keys}
        item.update({k: terms.get(k, item.get(k)) for k in ('deliverables', 'revision_rounds', 'caption_versions', 'locations', 'shoot_days')})
        if any(item.get(k) is None for k in keys + ('deliverables',)):
            continue
        total = terms['price_sgd']
        valid = True
        for order in snapshot['change_orders']:
            calc = order.get('calculation')
            fields = {'extra_location': 'locations', 'caption_version': 'caption_versions', 'extra_revision_round': 'revision_rounds'}
            if not calc or calc.get('kind') not in fields:
                valid = False
                break
            item[fields[calc['kind']]] += calc['quantity']
            total += order['price_sgd']
        if not valid:
            continue
        item.update(project_id='closeout:' + row['id'], actual_hours=actuals['actual_hours'], price_sgd=total, end=actuals['end'])
        result.append(item)
    return result


def list_orders(conn, version_id):
    v = _version(conn, version_id)
    return [{'id': r['id'], 'status': r['status'], 'terms': json.loads(r['terms_json']), 'digest': r['digest'], 'base_order_id': r['base_order_id']} for r in conn.execute('SELECT * FROM change_orders WHERE deal_id=? ORDER BY rowid', (v['deal_id'],))]


def _save_page_tool(conn, args):
    v = _version(conn, args['version_id'])
    deal = conn.execute('SELECT sample_data FROM deals WHERE id=?', (v['deal_id'],)).fetchone()
    if not deal or not deal['sample_data']:
        raise ValueError('sample_only')
    return save_page(conn, args['version_id'], args['page'])


def _orders_tool(conn, args):
    v = _version(conn, args['version_id'])
    deal = conn.execute('SELECT sample_data FROM deals WHERE id=?', (v['deal_id'],)).fetchone()
    if not deal or not deal['sample_data']:
        raise ValueError('sample_only')
    return {'orders': list_orders(conn, args['version_id'])}

def _priced_tool(conn, args):
    v = _version(conn, args['version_id'])
    deal = conn.execute('SELECT sample_data FROM deals WHERE id=?', (v['deal_id'],)).fetchone()
    if not deal or not deal['sample_data']:
        raise ValueError('sample_only')
    return create_priced_order(conn, args['version_id'], args.get('base_order_id'), args['kind'], args['quantity'], args['as_of'], args['deadline'], args.get('request_id'))

def _detailed_priced_tool(conn, args):
    v = _version(conn, args['version_id'])
    deal = conn.execute('SELECT sample_data FROM deals WHERE id=?', (v['deal_id'],)).fetchone()
    if not deal or not deal['sample_data']:
        raise ValueError('sample_only')
    return create_detailed_order(conn, args['version_id'], args.get('base_order_id'), args['spec'], args['tasks'], args['as_of'], args['deadline'], args['description'], args.get('request_id'))

MCP = {
    'compose_proposal_page': ({'version_id': {'type': 'string'}, 'page': {'type': 'object'}}, ['version_id', 'page'], _save_page_tool),
    'price_additional_work': ({'version_id': {'type':'string'}, 'base_order_id': {'type':['string','null']}, 'kind': {'type':'string'}, 'quantity': {'type':'integer'}, 'as_of': {'type':'string'}, 'deadline': {'type':'string'}, 'request_id': {'type':'string'}}, ['version_id','kind','quantity','as_of','deadline'], _priced_tool),
    'price_detailed_additional_work': ({'version_id': {'type':'string'}, 'base_order_id': {'type':['string','null']}, 'spec': {'type':'object'}, 'tasks': {'type':'array'}, 'description': {'type':'string'}, 'request_id': {'type':'string'}, 'as_of': {'type':'string'}, 'deadline': {'type':'string'}}, ['version_id','spec','tasks','description','as_of','deadline'], _detailed_priced_tool),
    'get_change_orders': ({'version_id': {'type': 'string'}}, ['version_id'], _orders_tool),
}


def dispatch_get(handler, path):
    if path == '/api/commerce/versions':
        if not handler.owner_ok():
            handler.forbidden('owner token required')
        else:
            handler.send_json({'versions':[dict(r) for r in handler.conn.execute("SELECT v.id, v.status, v.version_no, d.client FROM proposal_versions v JOIN deals d ON d.id=v.deal_id WHERE v.status IN ('draft','checked','blocked','confirmed') ORDER BY v.created_at DESC")]})
        return True
    if path != '/commerce':
        return False
    if not handler.owner_ok():
        handler.forbidden('owner token required')
        return True
    handler.send_html(_owner_page())
    return True


def dispatch_post(handler, path):
    # Called only after owner and same-origin checks. Buyer acceptance is
    # integrated separately in the token route via accept_order.
    operations = {'/api/commerce/page': lambda a: save_page(handler.conn, a['version_id'], a['page']),
                  '/api/commerce/order': lambda a: create_order(handler.conn, a['version_id'], a.get('base_order_id'), a['terms']),
                  '/api/commerce/priced-order': lambda a: create_priced_order(handler.conn, a['version_id'], a.get('base_order_id'), a['kind'], a['quantity'], a['as_of'], a['deadline']),
                  '/api/commerce/approve': lambda a: approve_order(handler.conn, a['order_id'], a['digest']),
                  '/api/commerce/confirm': lambda a: confirm_order(handler.conn, a['order_id']),
                  '/api/commerce/closeout': lambda a: closeout(handler.conn, a['version_id'], a['actuals']),
                  '/api/commerce/orders': lambda a: {'orders': list_orders(handler.conn, a['version_id'])}}
    if path not in operations:
        return False
    try:
        result = operations[path](handler.body())
        handler.send_json({'ok': True, **result})
    except (ValueError, KeyError, TypeError) as exc:
        handler.conn.rollback()
        handler.send_json({'ok': False, 'error': str(exc)}, 400)
    return True


def _owner_page():
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Proposal studio</title><style>body{font:16px system-ui;max-width:760px;margin:2em auto;padding:0 1em}label,input,select,textarea,button{display:block;margin:.5em 0}input,select,textarea{width:100%;padding:.5em;box-sizing:border-box}textarea{min-height:5em}button{padding:.6em}section{border-top:1px solid #bbb;margin:2em 0}pre{white-space:pre-wrap}</style><main><h1>Proposal studio</h1><label for="version">Proposal</label><select id="version"></select><section><h2>Personalize proposal</h2><label>Headline<input id="headline"></label><label>Introduction<textarea id="intro"></textarea></label><label>Section title<input id="section-title"></label><label>Section text<textarea id="section-body"></textarea></label><button id="save-page">Save as new version</button><p>Review and check the new version in the dashboard before releasing it.</p></section><section><h2>Price additional work</h2><p>Supported additions use the studio rate card and the original planning buffer.</p><label>Service<select id="kind"><option value="extra_location">Additional location</option><option value="caption_version">Caption an existing video</option><option value="extra_revision_round">Additional revision round</option></select></label><label>Quantity<input type="number" id="qty" min="1" value="1"></label><label>Planning from<input id="asof" type="date" value="2026-11-23"></label><label>Deliver by<input id="deadline" type="date" value="2026-12-04"></label><button id="price">Calculate draft</button><button id="refresh">Show change orders</button><div id="orders"></div><p>Approval releases the exact order for the buyer to accept. Confirm after acceptance to reserve the additional capacity.</p></section><section><h2>Closeout actuals</h2><label>Actual hours<input id="hours" type="number" min="0" step="0.01"></label><label>Actual direct cost S$<input id="cost" type="number" min="0" step="0.01"></label><label>Labour cost S$ (for task actuals)<input id="laborcost" type="number" min="0" step="0.01"></label><label>Task actuals JSON (optional; all booked detailed task IDs)<textarea id="taskactuals" placeholder='[&#123;"task_id":"shoot","hours":5,"labor_cost_cents":22500,"note":""&#125;]'></textarea></label><label>Completed on<input id="end" type="date"></label><label>Notes<textarea id="notes"></textarea></label><label><input id="reliable" type="checkbox"> These actuals are reliable for future estimates</label><button id="closeout">Record final actuals</button></section><pre id="result" role="status"></pre></main><script>
const token=new URLSearchParams(location.search).get('owner'), el=id=>document.getElementById(id), v=()=>el('version').value;
let latest=null,orders=[];
async function call(action,data){let r=await fetch('/api/commerce/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Owner-Token':token},body:JSON.stringify(data)});let x=await r.json();el('result').textContent=r.ok?'Saved.':(x.error||'Unable to save.');return r.ok?x:null}
async function refresh(){let r=await call('orders',{version_id:v()});if(!r)return;orders=r.orders;latest=[...orders].reverse().find(x=>x.status==='confirmed')?.id||null;el('orders').replaceChildren();orders.forEach(x=>{let box=document.createElement('article'), title=document.createElement('h3'), detail=document.createElement('p');title.textContent=x.terms.description+' · '+x.status;detail.textContent='S$'+x.terms.price_sgd.toFixed(2)+' · '+x.terms.phases.map(p=>p.deadline).join(', ')+(x.terms.calculation?' · quantity '+x.terms.calculation.quantity:'');box.append(title,detail);if(x.status==='draft'){let b=document.createElement('button');b.textContent='Approve these exact terms';b.onclick=async()=>{await call('approve',{order_id:x.id,digest:x.digest});refresh()};box.append(b)}if(x.status==='accepted'){let b=document.createElement('button');b.textContent='Confirm and reserve capacity';b.onclick=async()=>{await call('confirm',{order_id:x.id});refresh()};box.append(b)}el('orders').append(box)})}
fetch('/api/commerce/versions?owner='+encodeURIComponent(token)).then(r=>r.json()).then(d=>{d.versions.forEach(x=>{let o=document.createElement('option');o.value=x.id;o.textContent=x.client+' — version '+x.version_no+' ('+x.status+')';el('version').append(o)});if(v())refresh()});el('version').onchange=refresh;el('refresh').onclick=refresh;
el('save-page').onclick=()=>call('page',{version_id:v(),page:{headline:el('headline').value,introduction:el('intro').value,sections:el('section-title').value||el('section-body').value?[{title:el('section-title').value,body:el('section-body').value}]:[],portfolio:[]}});
el('price').onclick=async()=>{let x=await call('priced-order',{version_id:v(),base_order_id:latest,kind:el('kind').value,quantity:Number(el('qty').value),as_of:el('asof').value,deadline:el('deadline').value});if(x)refresh()};
el('closeout').onclick=()=>{if(!el('hours').value||!el('cost').value||!el('end').value){el('result').textContent='Enter hours, cost and completion date. Zero may be entered explicitly.';return}let actuals={actual_hours:Number(el('hours').value),actual_cost_sgd:Number(el('cost').value),end:el('end').value,notes:el('notes').value,reliable:el('reliable').checked};if(el('taskactuals').value.trim()){try{actuals.task_actuals=JSON.parse(el('taskactuals').value);if(!el('laborcost').value)throw Error('Enter labour cost.');actuals.actual_labor_cost_sgd=Number(el('laborcost').value)}catch(e){el('result').textContent='Check task actuals: '+e.message;return}}call('closeout',{version_id:v(),actuals})};
</script></html>"""
