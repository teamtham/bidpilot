"""Durable public API transport; commercial release remains founder-only."""
import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import conversation as chat
import db
import qwenwork_api
from qwenwork_commercial import prompt as commercial_prompt, apply as commercial_apply
import qwenwork_commercial

SCHEMA = '''
CREATE TABLE IF NOT EXISTS qwenwork_work (
 key TEXT PRIMARY KEY, kind TEXT NOT NULL, inquiry_id TEXT, payload_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', session_id TEXT, turn_id TEXT,
 created_at REAL NOT NULL, due REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 contract_retries INTEGER NOT NULL DEFAULT 0,
 response_json TEXT, error TEXT, finished_at REAL);
CREATE TABLE IF NOT EXISTS qwenwork_inquiry_sessions (
 inquiry_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, expires_at REAL NOT NULL);
''' + qwenwork_commercial.SELECTIONS_SCHEMA

# Contract errors the cloud agent can repair with feedback (same set as the
# local adapter, plus the API-only observation checks). Anything else blocks.
REPAIRABLE = {'invalid_json', 'invalid_chat_answer', 'invalid_fact', 'invalid_fact_source',
              'ungrounded_fact', 'unverified_number', 'invalid_reference_cue',
              'invalid_reference_observation', 'unsafe_reference_cue',
              'invalid_comparable_selection', 'unverified_portfolio_selection',
              'invalid_commercial_answer', 'ungrounded_production_input'}



def skills_root():
    """The installed QwenWork skills, or this repo's copy when none are installed
    (a fresh checkout, CI or another machine)."""
    installed = Path.home() / '.qwenwork/skills'
    if (installed / 'bidpilot-intake').is_dir():
        return installed
    return Path(__file__).resolve().parent.parent / 'bidpilot-build-guide'

def _ensure_schema(c):
    c.executescript(SCHEMA)
    if not any(row[1] == 'contract_retries' for row in c.execute('PRAGMA table_info(qwenwork_work)')):
        with c:
            c.execute('ALTER TABLE qwenwork_work ADD COLUMN contract_retries INTEGER NOT NULL DEFAULT 0')


def enqueue(c, key, kind, inquiry, payload):
    with c:
        c.execute('INSERT OR IGNORE INTO qwenwork_work(key,kind,inquiry_id,payload_json,created_at) VALUES (?,?,?,?,?)',
                  (key, kind, inquiry, db.j(payload), time.time()))


def discover(c):
    import studio_assistant
    for brief in studio_assistant.pending(c):
        enqueue(c, 'brief:' + brief['receipt_id'], 'brief', brief['inquiry_id'], brief)
    # Recover a crash between page composition and its exact-version check.
    import core
    for version in c.execute("SELECT v.id FROM proposal_versions v JOIN proposal_compositions p ON p.version_id=v.id JOIN inquiries i ON i.deal_id=v.deal_id WHERE v.outbound_status='unchecked' AND EXISTS (SELECT 1 FROM qwenwork_work w WHERE w.inquiry_id=i.id AND w.kind='compose')").fetchall():
        import inquiry_quote
        current = c.execute('SELECT * FROM proposal_versions WHERE id=?', (version['id'],)).fetchone()
        if not inquiry_quote.scope_error(c, current):
            core.check_draft(c, version['id'])
    for item in chat.pending(c):
        inquiry = item['inquiry_id']
        ctx = chat.context(c, {'inquiry_id': inquiry})
        seq = max((t['seq'] for t in ctx['turns'] if t['sender'] == 'buyer'), default=0)
        media = (item.get('media_updates') or [{}])[0].get('analysis_id')
        enqueue(c, f'chat:{inquiry}:{seq}:{media or "text"}', 'chat', inquiry,
                {'sequence': seq, 'revision': ctx['revision'], 'media': media})


def session_name(c, inquiry_id):
    row = c.execute('SELECT d.client FROM inquiries i LEFT JOIN deals d ON d.id=i.deal_id WHERE i.id=?',
                    (inquiry_id,)).fetchone()
    label = row['client'] if row and row['client'] else inquiry_id[:8]
    return 'BidPilot · ' + label


def _api_step(c, inquiry_id, step_key, status, note, offer_id=None):
    """Founder-visible timeline entry for an API-driven stage (no deal yet: skip)."""
    row = c.execute('SELECT deal_id FROM inquiries WHERE id=?', (inquiry_id,)).fetchone()
    if not row or not row['deal_id']:
        return
    import core
    with c:
        core.record_step(c, row['deal_id'], offer_id, step_key, status, note, trigger='api')


def portfolio_manifest(c):
    """Client-safe portfolio projection: no fees, rates, hours, floors,
    capacity, client names or private notes ever enter this file."""
    import estimating
    history = {r['project_id']: r for r in estimating.get_history(c)}
    assets = []
    for row in c.execute('SELECT id,project_id,title,description,tags,mime,digest FROM portfolio_assets WHERE shared=1 ORDER BY id'):
        project = history.get(row['project_id']) or {}
        ext = {'video/mp4': 'mp4', 'image/jpeg': 'jpg', 'image/png': 'png'}.get(row['mime'], 'bin')
        assets.append({
            'asset_id': row['id'], 'project_id': row['project_id'],
            'title': row['title'], 'description': row['description'],
            'style_tags': row['tags'], 'format': project.get('format'),
            'production_mode': project.get('production_mode'),
            'shoot_days': project.get('shoot_days'), 'deliverables': project.get('deliverables'),
            'caption_versions': project.get('caption_versions'),
            'clip_drive_path': '/bidpilot/portfolio/clips/' + row['digest'][:16] + '/' + row['id'] + '.' + ext})
    return {'studio': 'Alex Tan Productions', 'assets': assets}


def _manifest_target(manifest):
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    sha = hashlib.sha256(raw).hexdigest()
    return raw, sha, '/bidpilot/portfolio/' + sha[:8] + '/portfolio.json'


def sync_portfolio(client, c):
    """Upload portfolio.json plus its clips; skip when the exact bytes are
    already in Drive. Called at worker start and before each brief prompt, so
    any portfolio change is republished before the agent reads the file."""
    manifest = portfolio_manifest(c)
    raw, sha, drive_path = _manifest_target(manifest)
    c.executescript(qwenwork_api.SCHEMA)
    if c.execute('SELECT 1 FROM qwenwork_drive_files WHERE sha256=? AND drive_path=?', (sha, drive_path)).fetchone():
        return drive_path
    rows = c.execute('SELECT id,content,mime FROM portfolio_assets WHERE shared=1 ORDER BY id').fetchall()
    with tempfile.TemporaryDirectory() as directory:
        for item, row in zip(manifest['assets'], rows):
            clip = Path(directory) / (row['id'] + '.bin')
            clip.write_bytes(row['content'])
            client.drive_upload(c, clip, item['clip_drive_path'], row['mime'])
        target = Path(directory) / 'portfolio.json'
        target.write_bytes(raw)
        client.drive_upload(c, target, drive_path, 'application/json')
    return drive_path


def synced_portfolio_path(c):
    """Drive path of the current manifest only if it is already uploaded."""
    _, sha, drive_path = _manifest_target(portfolio_manifest(c))
    try:
        row = c.execute('SELECT 1 FROM qwenwork_drive_files WHERE sha256=? AND drive_path=?', (sha, drive_path)).fetchone()
    except Exception:
        return None
    return drive_path if row else None


def _reference_duration(run, asset_path):
    if run['result_json']:
        try:
            duration = json.loads(run['result_json']).get('duration')
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 < duration <= 180:
                return float(duration)
        except (ValueError, TypeError):
            pass
    ffmpeg = chat.ffmpeg_executable()
    if ffmpeg:
        try:
            probe = subprocess.run([ffmpeg, '-hide_banner', '-i', str(asset_path)],
                                   capture_output=True, text=True, timeout=20)
            match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', probe.stderr)
            if match:
                duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                if 0 < duration <= 180:
                    return duration
        except (OSError, subprocess.SubprocessError):
            pass
    raise ValueError('reference_duration_unavailable')


def _reference_row(c, inquiry_id, analysis_id):
    return c.execute('SELECT r.id,r.status,r.result_json,a.id AS asset_id,a.path,a.mime,a.filename '
                     'FROM reference_runs r JOIN reference_assets a ON a.id=r.asset_id '
                     'WHERE r.id=? AND a.inquiry_id=?', (analysis_id, inquiry_id)).fetchone()


def prompt(c, row, client=None):
    if row['kind'] != 'chat':
        return commercial_prompt(c, row, client)
    payload = json.loads(row['payload_json'])
    import studio_assistant
    ctx = chat.context(c, {'inquiry_id': row['inquiry_id']})
    public = {key: ctx[key] for key in ('revision', 'processed_sequence', 'facts')}
    public['turns'] = [{key: t[key] for key in ('seq', 'sender', 'body')} for t in ctx['turns'][-32:]]
    public['media_analysis_id'] = None
    missing = chat.brief_missing(ctx['facts'])
    public['brief_missing'] = [{'field': m, 'subject': chat.BRIEF_SUBJECTS.get(m.removeprefix('Confirm ').capitalize())}
                               for m in missing]
    root = skills_root()
    bank = (root / 'bidpilot-intake/QUESTION_BANK.md').read_bytes()
    if not bank.strip() or bank != (root / 'bidpilot-scoping/QUESTION_BANK.md').read_bytes():
        raise ValueError('question_bank_mismatch')
    suffix = ('\nNo tools or frames are available. Return only final JSON. Do not return operations. '
              'Each fact has ONLY subject,value,status,supersedes(optional). Never emit source_sequence or evidence keys; '
              'the server adds provenance. Only save requirements explicitly supported by the latest buyer message. '
              'When the latest buyer message answers a brief_missing field, save it under the subject listed there.\n')
    if payload.get('media'):
        # The cloud agent watches the buyer's reference itself from Drive; the
        # server validates every returned timestamp and cue before saving.
        if client is None:
            raise ValueError('qwenwork_client_required')
        run = _reference_row(c, row['inquiry_id'], payload['media'])
        if not run:
            raise ValueError('stale_reference_update')
        duration = _reference_duration(run, run['path'])
        ext = {'video/mp4': 'mp4', 'video/quicktime': 'mov', 'video/x-m4v': 'm4v'}.get(run['mime'], 'mp4')
        drive_path = '/bidpilot/inquiries/' + row['inquiry_id'] + '/' + run['asset_id'] + '.' + ext
        client.drive_upload(c, run['path'], drive_path, run['mime'])
        public['media_analysis_id'] = run['id']
        public['reference'] = {'drive_path': drive_path, 'duration_seconds': duration,
                               'filename': run['filename']}
        suffix = ('\nA buyer reference video is in QwenWork Drive at ' + drive_path + ' (duration ' + str(duration) +
                  ' seconds). Download it with the qwenwork CLI (qwenwork drive download) and watch it yourself '
                  'with the media-read skill. Only Drive download and media analysis tools are available; do not '
                  'send, publish, approve or book anything. Return only final JSON with reply, fact_changes and '
                  'reference_observations: an array of at most 50 objects {start,end,body,uncertainty}. start/end '
                  'are seconds within 0..' + str(duration) + '; body is a buyer-safe, number-free visual observation; '
                  'uncertainty states its limits. The server links these observations to the analysis as production '
                  'cues; do not return production_cues or observation_id yourself. Each fact has ONLY '
                  'subject,value,status,supersedes(optional). Never emit source_sequence or evidence keys. Only save '
                  'requirements explicitly supported by the latest buyer message. For a reference update after an '
                  'answered turn leave fact_changes empty.\n')
    return bank.decode() + '\n' + studio_assistant.PROMPT + suffix + db.j(public)


def _save_reference_observations(c, row, payload, observations):
    """Validate cloud observations, record them against the analysis run and
    return (production_cues, inserted_ids) for the conversation commit."""
    run = _reference_row(c, row['inquiry_id'], payload['media'])
    if not run:
        raise ValueError('stale_reference_update')
    if c.execute('SELECT 1 FROM inquiry_commits WHERE inquiry_id=? AND request_id=?',
                 (row['inquiry_id'], row['key'])).fetchone():
        # Already committed: rebuild the identical cue args so the replayed
        # commit returns its receipt instead of inserting duplicate evidence.
        cues = [{'observation_id': r['supersedes'], 'body': r['body'], 'uncertainty': r['uncertainty']}
                for r in c.execute("SELECT supersedes,body,uncertainty FROM reference_observations "
                                   "WHERE run_id=? AND kind='production_cue' AND method='QwenWork agent cue' ORDER BY rowid",
                                   (run['id'],))]
        return cues, []
    duration = _reference_duration(run, run['path'])
    if not isinstance(observations, list) or len(observations) > 50:
        raise ValueError('invalid_reference_observation')
    import core
    deal = c.execute('SELECT d.client FROM inquiries i LEFT JOIN deals d ON d.id=i.deal_id WHERE i.id=?',
                     (row['inquiry_id'],)).fetchone()
    names = core.other_client_names(c, deal['client'] if deal and deal['client'] else '')
    prepared = []
    for obs in observations:
        if not isinstance(obs, dict) or set(obs) != {'start', 'end', 'body', 'uncertainty'}:
            raise ValueError('invalid_reference_observation')
        start, end = obs['start'], obs['end']
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not 0 <= start <= end <= duration + 0.1):
            raise ValueError('invalid_reference_observation')
        body, uncertainty = obs['body'], obs['uncertainty']
        if (not isinstance(body, str) or not body.strip() or len(body) > 2000
                or not isinstance(uncertainty, str) or not uncertainty.strip() or len(uncertainty) > 1000):
            raise ValueError('invalid_reference_observation')
        if not core.engine.outbound_check(body + ' ' + uncertainty, [], {}, names)['passed']:
            raise ValueError('unsafe_reference_cue')
        prepared.append((float(start), min(float(end), duration), body, uncertainty))
    inserted = []
    with c:
        for start, end, body, uncertainty in prepared:
            oid = chat.uid()
            c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                      (oid, run['id'], start, end, 'agent', body, 'QwenWork cloud agent',
                       uncertainty, None, None, time.time()))
            inserted.append(oid)
    cues = [{'observation_id': oid, 'body': body, 'uncertainty': uncertainty}
            for oid, (_, _, body, uncertainty) in zip(inserted, prepared)]
    return cues, inserted


def apply(c, row, answer):
    if row['kind'] != 'chat':
        return commercial_apply(c, row, answer)
    payload = json.loads(row['payload_json'])
    allowed = {'reply', 'fact_changes', 'proposal_email'}
    if payload.get('media'):
        allowed.add('reference_observations')
    if not isinstance(answer, dict) or set(answer) - allowed:
        raise ValueError('invalid_chat_answer')
    import studio_assistant
    args = {'inquiry_id': row['inquiry_id'], 'through_sequence': payload['sequence'],
            'expected_revision': payload['revision'], 'request_id': row['key'],
            'reply': answer['reply'], 'fact_changes': []}
    latest = c.execute("SELECT body FROM inquiry_turns WHERE inquiry_id=? AND seq=? AND sender='buyer'",
                       (row['inquiry_id'], payload['sequence'])).fetchone()
    changes = answer.get('fact_changes', [])
    if not isinstance(changes, list) or len(changes) > 50:
        raise ValueError('invalid_fact')
    for f in changes:
        if not isinstance(f, dict) or set(f) - {'subject', 'value', 'status', 'supersedes'}:
            raise ValueError('invalid_fact')
        if f.get('status') == 'confirmed' and isinstance(f.get('value'), str) and not studio_assistant.confirmed_text_matches(f, latest['body'] if latest else ''):
            raise ValueError('ungrounded_fact')
        args['fact_changes'].append(dict(f, source_sequence=payload['sequence']))
    if answer.get('proposal_email'):
        args['proposal_email'] = answer['proposal_email']
    inserted = []
    if payload.get('media'):
        cues, inserted = _save_reference_observations(c, row, payload, answer.get('reference_observations', []))
        args['media_analysis_id'] = payload['media']
        args['production_cues'] = cues
    try:
        chat.commit(c, args)
    except Exception:
        if inserted:
            with c:
                for oid in inserted:
                    c.execute("DELETE FROM reference_observations WHERE id=? AND kind='agent'", (oid,))
        raise
    if payload.get('media'):
        _api_step(c, row['inquiry_id'], 'reference', 'ok',
                  str(len(inserted)) + ' production cues saved from cloud reference analysis')
    else:
        _api_step(c, row['inquiry_id'], 'chat', 'ok', 'Buyer chat answered via QwenWork API')


def _repair_feedback(c, row, rejected_text, error):
    """Feedback turn for a rejected answer, mirroring the local adapter."""
    payload = json.loads(row['payload_json'])
    if row['kind'] != 'chat':
        shape = ('{"offer_request":{"as_of":"YYYY-MM-DD","offer":{...engine fields...},"production_facts":{...}},'
                 '"comparable_ids":[...],"similar_work_asset_id":"..."} with NO other top-level keys in the answer '
                 '(as_of belongs INSIDE offer_request; keep it there)'
                 if row['kind'] == 'brief' else
                 '{"page":{...}} with NO other top-level keys')
        feedback = {'rejected_answer': rejected_text, 'contract_error': error,
                    'instruction': 'Your last JSON failed server validation. Return only ' + shape +
                    '. Re-read the original task instructions in this session. Use only IDs that '
                    'exist in the provided portfolio file or local data; never invent IDs. Do not '
                    'include private numbers, fees, rates or hours anywhere.'}
        if row['kind'] == 'brief':
            ctx = chat.context(c, {'inquiry_id': row['inquiry_id']})
            gone = {f.get('supersedes') for f in ctx['facts'] if f.get('supersedes')}
            feedback['confirmed_production_facts'] = {
                f['subject']: f['value'] for f in ctx['facts']
                if f['id'] not in gone and f['status'] == 'confirmed' and f['subject'].startswith('production.')}
            feedback['buyer_messages'] = [t['body'] for t in ctx['turns'] if t['sender'] == 'buyer']
            feedback['instruction'] += (' Set offer_request.production_facts to exactly confirmed_production_facts: '
                                        'a flat map subject -> plain value, no wrapper objects, no extra subjects. '
                                        'Set offer_request.client to the business or person name the buyer stated '
                                        'in buyer_messages; if none was stated, return the blocked form asking for it.')
            if error.startswith('invalid_scope:'):
                feedback['instruction'] += (' Every offer field is required and must be a real value, never null or missing: '
                                            'interviewees is an integer (0 when the buyer mentions none) and '
                                            'direct_costs_sgd is 0. Fields rejected: ' + error.split(':', 1)[1] + '.')
        return db.j(feedback)
    ctx = chat.context(c, {'inquiry_id': row['inquiry_id']})
    latest = next((t['body'] for t in reversed(ctx['turns']) if t['sender'] == 'buyer'), '')
    feedback = {'rejected_answer': rejected_text, 'contract_error': error,
                'latest_buyer_message': latest,
                'instruction': 'Correct the JSON using the original buyer evidence. Confirmed string '
                'values must be exact contiguous quotations from latest_buyer_message, not summaries, '
                'canonical labels or earlier turns; use typed booleans/numbers for typed answers. Each '
                'fact has ONLY subject,value,status,supersedes(optional); never emit source_sequence or '
                'evidence keys. Keep the reply number-free and commercial-commitment-free. Do not invent '
                'or silently discard buyer requirements. Do not re-emit already_saved_facts unless the '
                'latest buyer message explicitly changes them. Return only the corrected final JSON.'}
    superseded = {f['supersedes'] for f in ctx['facts'] if f.get('supersedes')}
    saved = {f['subject']: {'id': f['id'], 'value': f['value'], 'status': f['status']}
             for f in ctx['facts'] if f['id'] not in superseded}
    feedback['already_saved_facts'] = saved
    feedback['omit_subjects'] = sorted(saved)
    feedback['instruction'] += (' These subjects are ALREADY SAVED and must be omitted from fact_changes '
                                'entirely unless latest_buyer_message explicitly changes them: '
                                + ', '.join(sorted(saved)) + '. Emit only facts grounded in '
                                'latest_buyer_message itself.')
    if error == 'invalid_fact_source':
        ids = {}
        for f in ctx['facts']:
            ids.setdefault(f['subject'], []).append(f['id'])
        feedback['existing_fact_ids'] = ids
    if payload.get('media'):
        run = _reference_row(c, row['inquiry_id'], payload['media'])
        if run:
            feedback['reference_rules'] = {
                'analysis_id': run['id'], 'duration_seconds': _reference_duration(run, run['path']),
                'observation_shape': '{start,end,body,uncertainty}', 'max_observations': 50,
                'note': 'start/end are seconds within the clip duration; body and uncertainty are '
                        'buyer-safe and number-free; do not return production_cues or observation_id.'}
    return db.j(feedback)


def tick(client, c):
    _ensure_schema(c)
    discover(c)
    with c:
        # A crash during POST has an unknown remote outcome. Never blindly replay.
        c.execute("UPDATE qwenwork_work SET status='blocked',error='dispatch_outcome_unknown',finished_at=? WHERE status='dispatching' AND due<=?", (time.time(), time.time()))
        c.execute("UPDATE qwenwork_work SET status='blocked',error='commercial_runtime_disabled',finished_at=? WHERE kind NOT IN ('chat','brief','compose') AND status IN ('queued','running')", (time.time(),))
    row = c.execute("SELECT w.* FROM qwenwork_work w WHERE kind IN ('chat','brief','compose') AND status IN ('queued','running') AND due<=? AND NOT EXISTS (SELECT 1 FROM qwenwork_work earlier WHERE earlier.inquiry_id=w.inquiry_id AND earlier.created_at<w.created_at AND earlier.status IN ('running','dispatching')) ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
    if not row:
        return
    try:
        payload = json.loads(row['payload_json'])
        ctx = chat.context(c, {'inquiry_id': row['inquiry_id']})
        if row['kind'] == 'chat' and payload.get('media') and payload['revision'] != ctx['revision']:
            # An earlier text turn may have committed since discovery; the
            # media analysis attaches to the current brief, not that snapshot.
            payload['revision'] = ctx['revision']
            with c:
                c.execute('UPDATE qwenwork_work SET payload_json=? WHERE key=?', (db.j(payload), row['key']))
        revision = payload['revision'] if row['kind'] == 'chat' else payload['expected_revision']
        latest = max((t['seq'] for t in ctx['turns'] if t['sender'] == 'buyer'), default=0)
        if ctx['revision'] != revision or (row['kind'] == 'chat' and (latest != payload['sequence'] or ctx['processed_sequence'] >= payload['sequence']) and not payload.get('media')):
            with c:
                c.execute("UPDATE qwenwork_work SET status='stale',finished_at=? WHERE key=?", (time.time(), row['key']))
            return
        if not row['session_id']:
            text = prompt(c, row, client)
            with c:
                claimed = c.execute("UPDATE qwenwork_work SET status='dispatching',due=?,attempts=attempts+1 WHERE key=? AND status='queued'", (time.time()+30, row['key'])).rowcount
            if not claimed:
                return
            saved = c.execute('SELECT * FROM qwenwork_inquiry_sessions WHERE inquiry_id=? AND expires_at>?', (row['inquiry_id'], time.time()+30)).fetchone() if row['kind'] == 'chat' else None
            if saved:
                sid = saved['session_id']
                try:
                    response = client.api('/v1/sessions/' + quote(sid, safe='') + '/prompt', 'POST', {'text': text})
                except ValueError as exc:
                    if str(exc) in ('http_404', 'http_410'):
                        with c:
                            c.execute('DELETE FROM qwenwork_inquiry_sessions WHERE inquiry_id=?', (row['inquiry_id'],))
                            c.execute("UPDATE qwenwork_work SET status='queued',due=0 WHERE key=?", (row['key'],))
                        return
                    raise
                tid = response['turn_id']
            else:
                response = client.api('/v1/sessions', 'POST', {'initial_prompt': {'text': text},
                    'name': session_name(c, row['inquiry_id']), 'mode': 'flash', 'locale': 'en', 'timezone': 'Asia/Singapore', 'ttl_seconds': 3600})
                sid, tid = response['session_id'], response.get('first_turn_id')
                if row['kind'] == 'chat':
                    with c:
                        c.execute('INSERT OR REPLACE INTO qwenwork_inquiry_sessions VALUES (?,?,?)', (row['inquiry_id'], sid, time.time()+3600))
            with c:
                c.execute("UPDATE qwenwork_work SET session_id=?,turn_id=?,status='running',due=? WHERE key=?", (sid, tid, time.time()+1, row['key']))
            return
        # created_at is queue age, not API session age. Bound active polling count.
        if row['attempts'] >= 300:
            raise ValueError('work_timeout')
        with c:
            c.execute('UPDATE qwenwork_work SET attempts=attempts+1,due=? WHERE key=?', (time.time()+2, row['key']))
        path = '/v1/sessions/' + quote(row['session_id'], safe='')
        if not row['turn_id']:
            turns = client.api(path + '/turns').get('items', [])
            if turns:
                with c:
                    c.execute('UPDATE qwenwork_work SET turn_id=? WHERE key=?', (turns[-1]['turn_id'], row['key']))
            return
        try:
            turn = client.api(path + '/turns/' + quote(row['turn_id'], safe=''))
        except ValueError as exc:
            if str(exc) == 'http_404' and client.api(path).get('status') in ('starting', 'running', 'idle'):
                return
            raise
        if turn['status'] != 'completed':
            if turn['status'] in ('failed', 'interrupted', 'action_required'):
                raise ValueError('remote_turn_failed')
            return
        text = turn['final_message'].strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        try:
            try:
                answer = json.loads(text)
            except ValueError:
                raise ValueError('invalid_json') from None
            outcome = apply(c, row, answer)
        except ValueError as exc:
            # Repair contract errors with the agent (bounded), never by
            # dropping or rewriting its facts server-side.
            if (str(exc) in REPAIRABLE or str(exc).startswith('invalid_scope:')) and row['contract_retries'] < 2:
                response = client.api('/v1/sessions/' + quote(row['session_id'], safe='') + '/prompt',
                                      'POST', {'text': _repair_feedback(c, row, text, str(exc))})
                with c:
                    c.execute("UPDATE qwenwork_work SET turn_id=?,contract_retries=contract_retries+1,due=?,status='running' WHERE key=?",
                              (response['turn_id'], time.time() + 1, row['key']))
                return
            raise
        if outcome and outcome.get('compose'):
            enqueue(c, 'compose:' + outcome['compose'], 'compose', row['inquiry_id'], {'version_id': outcome['compose'], 'expected_revision': outcome['expected_revision']})
        with c:
            c.execute("UPDATE qwenwork_work SET status=?,response_json=?,finished_at=? WHERE key=?", ('blocked' if outcome and outcome.get('blocked') else 'done', db.j(answer), time.time(), row['key']))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        c.rollback()
        reason = str(exc) if (str(exc).startswith('invalid_scope:') or str(exc) in ('sign_in_required', 'http_401', 'http_403', 'http_429', 'work_timeout', 'remote_turn_failed', 'reference_runtime_disabled', 'question_bank_mismatch', 'stale_revision', 'reference_duration_unavailable', 'invalid_reference_observation', 'unsafe_reference_cue', 'unverified_portfolio_selection', 'invalid_comparable_selection', 'ungrounded_fact', 'invalid_json', 'ungrounded_production_input', 'priced_revision_conflict')) else 'api_or_contract_failure'
        with c:
            c.execute("UPDATE qwenwork_work SET status='blocked',error=?,finished_at=? WHERE key=?", (reason, time.time(), row['key']))