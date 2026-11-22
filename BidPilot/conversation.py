"""Private inquiry chat and bounded, local reference evidence for QwenWork.

The host dispatches routes before owner authentication, after opening its per-request
connection. MCP callers are trusted service users; public routes use capability tokens.
"""
import base64
from datetime import date
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import unicodedata

SCHEMA = '''
CREATE TABLE IF NOT EXISTS inquiries (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, revision INTEGER NOT NULL DEFAULT 0, processed_seq INTEGER NOT NULL DEFAULT 0, deal_id TEXT, created_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS inquiry_turns (inquiry_id TEXT NOT NULL, seq INTEGER NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL, refs_json TEXT NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(inquiry_id,seq), UNIQUE(inquiry_id,request_id));
CREATE TABLE IF NOT EXISTS inquiry_facts (id TEXT PRIMARY KEY, inquiry_id TEXT NOT NULL, revision INTEGER NOT NULL, subject TEXT NOT NULL, value_json TEXT NOT NULL, status TEXT NOT NULL, source_seq INTEGER NOT NULL, supersedes TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS inquiry_jobs (id TEXT PRIMARY KEY, inquiry_id TEXT NOT NULL, seq INTEGER NOT NULL, status TEXT NOT NULL, lease_until REAL, UNIQUE(inquiry_id,seq));
CREATE TABLE IF NOT EXISTS reference_assets (id TEXT PRIMARY KEY, inquiry_id TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reference_runs (id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT, error TEXT, created_at REAL NOT NULL, lease_until REAL);
CREATE TABLE IF NOT EXISTS reference_observations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, method TEXT NOT NULL, uncertainty TEXT NOT NULL, frame TEXT, supersedes TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS media_notifications (run_id TEXT PRIMARY KEY, inquiry_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued');
CREATE TABLE IF NOT EXISTS inquiry_commits (inquiry_id TEXT NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(inquiry_id,request_id));
CREATE TABLE IF NOT EXISTS inquiry_receipts (source_id TEXT PRIMARY KEY, inquiry_id TEXT UNIQUE NOT NULL);
'''
MAX_UPLOAD = 30 * 1024 * 1024

def uid(): return str(uuid.uuid4())
def digest(s): return hashlib.sha256(s.encode()).hexdigest()
def packed(x): return json.dumps(x, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
def _begin(c): c.execute('BEGIN IMMEDIATE')
def _get(c, inquiry):
    row = c.execute('SELECT * FROM inquiries WHERE id=? AND revoked=0', (inquiry,)).fetchone()
    if not row: raise ValueError('not_found')
    return row

def create_receipt_inquiry(c, source_id, token, fields):
    """Create a form-linked inquiry in the caller's receipt transaction."""
    inquiry = uid()
    body = packed(fields)
    c.execute('INSERT INTO inquiries(id,token_hash,created_at) VALUES (?,?,?)', (inquiry,digest(token),time.time()))
    c.execute('INSERT INTO inquiry_receipts VALUES (?,?)', (source_id,inquiry))
    import delivery
    delivery.save_recipient(c, {'id': inquiry, 'deal_id': None}, fields['reply_email'], True, 1)
    ph = digest(packed({'text':body,'references':[]}))
    c.execute('INSERT INTO inquiry_turns VALUES (?,?,?,?,?,?,?,?)', (inquiry,1,'buyer',body,'[]','receipt',ph,time.time()))
    c.execute('INSERT INTO inquiry_jobs(id,inquiry_id,seq,status) VALUES (?,?,1,?)', (uid(),inquiry,'queued'))
    return inquiry

def create_inquiry(c, data=None, *, provider='qwenwork'):
    token = secrets.token_urlsafe(32); inquiry = uid(); data=data or {}
    text=data.get('text',''); req=data.get('request_id') or uid()
    if not isinstance(text,str) or len(text)>10000 or not isinstance(req,str) or len(req)>128: raise ValueError('invalid_message')
    try:
        _begin(c)
        c.execute('INSERT INTO inquiries(id,token_hash,created_at) VALUES (?,?,?)', (inquiry,digest(token),time.time()))
        if provider == 'gemini':
            c.execute('INSERT INTO gemini_chats(inquiry_id) VALUES (?)', (inquiry,))
        if text.strip():
            ph=digest(packed({'text':text,'references':[]}))
            c.execute('INSERT INTO inquiry_turns VALUES (?,?,?,?,?,?,?,?)',(inquiry,1,'buyer',text,'[]',req,ph,time.time()))
            c.execute('INSERT INTO inquiry_jobs(id,inquiry_id,seq,status) VALUES (?,?,1,?)',(uid(),inquiry,'queued'))
        c.commit()
    except Exception: c.rollback(); raise
    return {'inquiry_id': inquiry, 'token': token}

def _by_token(c, token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token): return None
    return c.execute('SELECT * FROM inquiries WHERE token_hash=? AND revoked=0', (digest(token),)).fetchone()

def _validate_refs(c, inquiry, refs):
    if not isinstance(refs,list) or len(refs)>20: raise ValueError('invalid_references')
    for ref in refs:
        if not isinstance(ref,dict) or not isinstance(ref.get('asset_id'),str): raise ValueError('invalid_references')
        if not c.execute('SELECT 1 FROM reference_assets WHERE id=? AND inquiry_id=?',(ref['asset_id'],inquiry)).fetchone(): raise ValueError('invalid_references')
        for k in ('start','end'):
            if k in ref and (isinstance(ref[k],bool) or not isinstance(ref[k],(float,int)) or not 0 <= ref[k] <= 180): raise ValueError('invalid_references')
        if ref.get('start',0)>ref.get('end',180): raise ValueError('invalid_references')
        if 'observation_id' in ref and not c.execute('SELECT 1 FROM reference_observations o JOIN reference_runs r ON r.id=o.run_id JOIN reference_assets a ON a.id=r.asset_id WHERE o.id=? AND a.id=? AND a.inquiry_id=?',(ref['observation_id'],ref['asset_id'],inquiry)).fetchone(): raise ValueError('invalid_references')

def add_message(c, inquiry, data):
    body=data.get('text',''); req=data.get('request_id'); refs=data.get('references',[])
    if not isinstance(body,str) or len(body)>10000 or not isinstance(req,str) or not 1<=len(req)<=128 or (not body.strip() and not refs): raise ValueError('invalid_message')
    payload=digest(packed({'text':body,'references':refs}))
    try:
        _begin(c); _get(c,inquiry); _validate_refs(c,inquiry,refs)
        old=c.execute('SELECT seq,payload_hash FROM inquiry_turns WHERE inquiry_id=? AND request_id=?',(inquiry,req)).fetchone()
        if old:
            if old['payload_hash']!=payload: raise ValueError('request_id_conflict')
            c.commit(); return {'sequence':old['seq']}
        seq=c.execute('SELECT COALESCE(MAX(seq),0)+1 FROM inquiry_turns WHERE inquiry_id=?',(inquiry,)).fetchone()[0]
        c.execute('INSERT INTO inquiry_turns VALUES (?,?,?,?,?,?,?,?)',(inquiry,seq,'buyer',body,packed(refs),req,payload,time.time()))
        c.execute('INSERT INTO inquiry_jobs(id,inquiry_id,seq,status) VALUES (?,?,?,?)',(uid(),inquiry,seq,'queued'))
        c.commit(); return {'sequence':seq}
    except Exception: c.rollback(); raise

# Required brief fields, in the order the brief card shows them. Keep the
# patterns in step with FIELDS in _UI_COMMON_JS. Budget is optional by design.
BRIEF_FIELDS=[('Format',r'format|video_type|project_type|style|platform|channels'),
              ('Production',r'shoot_or|on_location|client_supplied_footage|production_type|production\.mode|asset_supply'),
              ('Shoot days',r'shoot_?days|days_on_set'),('Locations',r'location'),
              ('Videos',r'(^|\.)videos$|original_edits|deliverables|video_count|reel_count'),('Captioned versions',r'caption'),
              ('Scripting',r'script'),('Revision rounds',r'revision'),('Deadline',r'deadline|final_delivery|due_date'),
              ('Editing',r'editing|edit_level|edit_style'),('Your budget',r'budget')]
# Subjects the assistant should use when it fills a missing card field.
BRIEF_SUBJECTS={'Format':'production.video_format','Production':'production.asset_supply','Shoot days':'production.shoot_days',
                'Locations':'production.location_requirements','Videos':'production.original_edits',
                'Captioned versions':'production.caption_requirement','Scripting':'production.script_supply',
                'Revision rounds':'production.revision_rounds','Deadline':'production.final_delivery_date',
                'Editing':'production.edit_level'}
OPTIONAL_FIELDS={'Your budget'}

def brief_missing(facts):
    """Required fields still empty, or only proposed ('to confirm'), as card labels."""
    gone={f.get('supersedes') for f in facts if f.get('supersedes')}
    latest={}
    for f in facts:
        if f['id'] not in gone: latest[f['subject']]=f
    slots=[None]*len(BRIEF_FIELDS)
    for f in latest.values():
        i=next((i for i,(_,p) in enumerate(BRIEF_FIELDS) if re.search(p,f['subject'])),None)
        if i is not None and slots[i] is None: slots[i]=f
    missing=[]
    for (name,_),f in zip(BRIEF_FIELDS,slots):
        if name in OPTIONAL_FIELDS: continue
        if f is None: missing.append(name)
        elif f['status']=='proposed': missing.append('Confirm '+name.lower())
    return missing

def context(c,args):
    inquiry=args['inquiry_id']; row=_get(c,inquiry)
    through=args.get('through_sequence', 2**63-1)
    if isinstance(through,bool) or not isinstance(through,int) or through<0: raise ValueError('invalid_sequence')
    turns=[dict(r) for r in c.execute('SELECT seq,sender,body,refs_json,created_at FROM inquiry_turns WHERE inquiry_id=? AND seq<=? ORDER BY seq',(inquiry,through))]
    for t in turns: t['references']=json.loads(t.pop('refs_json'))
    facts=[dict(r) for r in c.execute('SELECT id,revision,subject,value_json,status,source_seq,supersedes FROM inquiry_facts WHERE inquiry_id=? ORDER BY revision,rowid',(inquiry,))]
    for f in facts: f['value']=json.loads(f.pop('value_json'))
    assets=[dict(r) for r in c.execute('SELECT a.id,a.filename,r.id AS analysis_id,r.status,r.error FROM reference_assets a LEFT JOIN reference_runs r ON r.asset_id=a.id WHERE a.inquiry_id=? ORDER BY a.created_at,r.created_at',(inquiry,))]
    history=[]
    if row['deal_id']:
        for r in c.execute('SELECT o.id AS offer_id,o.offer_json,o.evidence_json,e.id AS evaluation_id,e.result_json FROM offers o LEFT JOIN evaluations e ON e.offer_id=o.id WHERE o.deal_id=? ORDER BY o.round,o.created_at',(row['deal_id'],)):
            item=dict(r)
            for key in ('offer_json','evidence_json','result_json'):
                item[key.removesuffix('_json')]=json.loads(item.pop(key) or 'null')
            history.append(item)
    email = c.execute('SELECT email,confirmed,source_seq,revision FROM proposal_recipients WHERE inquiry_id=?', (inquiry,)).fetchone()
    return {'inquiry_id':inquiry,'deal_id':row['deal_id'],'revision':row['revision'],'processed_sequence':row['processed_seq'],'turns':turns,'facts':facts,'references':assets,'pricing_history':history,'proposal_email':dict(email) if email else None}

def _check_buyer_text(c, inquiry, text, allow_scoping_question=False):
    """Reject internal labels, other client names and numbers absent from buyer messages.

    This is a conservative guard, not a semantic guarantee of model behavior.
    """
    import core
    normalized=unicodedata.normalize('NFKC',text).casefold()
    if re.search(r'\b(?:price floor|floor price|profit margin|hourly rate|capacity hours|internal estimate|private costs?|internal costs?)\b',normalized): raise ValueError('private_information')
    row=_get(c,inquiry)
    client=''
    if row['deal_id']:
        deal=c.execute('SELECT client FROM deals WHERE id=?',(row['deal_id'],)).fetchone()
        if deal: client=deal['client']
    for name in core.other_client_names(c,client):
        clean=unicodedata.normalize('NFKC',name).casefold().strip()
        if clean and re.search(r'(?<!\w)'+re.escape(clean)+r'(?!\w)',normalized): raise ValueError('private_information')
    buyer=' '.join(r[0] for r in c.execute("SELECT body FROM inquiry_turns WHERE inquiry_id=? AND sender='buyer'",(inquiry,)))
    buyer=unicodedata.normalize('NFKC',buyer).casefold()
    allowed=set(re.findall(r'\d[\d,]*(?:\.\d+)?',buyer))
    for i,word in enumerate(('zero','one','two','three','four','five','six','seven','eight','nine','ten')):
        if re.search(r'(?<!\w)'+word+r'(?!\w)',buyer): allowed.add(str(i))
    # Count words buyers use instead of digits ("a single story" -> 1 edit).
    for word,n in (('single','1'),('once','1'),('twice','2'),('double','2'),('both','2')):
        if re.search(r'(?<!\w)'+word+r'(?!\w)',buyer): allowed.add(n)
    unknown=[n for n in re.findall(r'\d[\d,]*(?:\.\d+)?',normalized) if n not in allowed]
    if unknown:
        neutral=(allow_scoping_question and '?' in text and not re.search(r'(?:s\$|sgd|\$)\s*\d',normalized)
                 and all(re.search(r'(?<!\w)'+re.escape(n)+r'\s*(?:-|\s)?(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|videos?|reels?|versions?|formats?|ratios?|fps|p|k)\b',normalized) for n in unknown))
        if not neutral: raise ValueError('unverified_number')

def grounded_date(subject, value, buyer_text):
    """Recognize a fully specified buyer date after normalization to the bank's ISO format."""
    if subject not in ('production.first_cut_date', 'production.final_delivery_date', 'deadline'):
        return False
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return False
    month = f"(?:{day:%B}|{day:%b})"
    number = f"0?{day.day}(?:st|nd|rd|th)?"
    return bool(re.search(rf'\b(?:{value}|(?:{number} {month}|{month} {number}),? {day.year})\b',
                          buyer_text, re.I))


def commit(c,args, decline_evaluation=None, gemini_lease=None):
    inquiry=args['inquiry_id']; seq=args['through_sequence']; rev=args['expected_revision']; reply=args['reply']; req=args['request_id']; changes=args.get('fact_changes',[])
    analysis=args.get('media_analysis_id'); cues=args.get('production_cues',[])
    if not isinstance(reply,str) or not reply.strip() or len(reply)>10000 or not isinstance(req,str) or not 1<=len(req)<=128 or not isinstance(changes,list) or len(changes)>50 or isinstance(seq,bool) or not isinstance(seq,int) or isinstance(rev,bool) or not isinstance(rev,int) or (analysis is not None and not isinstance(analysis,str)) or not isinstance(cues,list) or len(cues)>50: raise ValueError('invalid_turn')
    if cues and not analysis: raise ValueError('invalid_reference_cue')
    # The conversation can collect requests but cannot make studio commitments.
    if re.search(r"\b(?:we (?:agree|accept|guarantee|confirm(?:ed)? (?:your |the )?booking|can do (?:it|this|that) for)|you(?: are|'re) booked|booking (?:is )?confirmed)\b", reply, re.I): raise ValueError('commitment_requires_founder')
    ph=digest(packed(args))
    try:
        _begin(c); row=_get(c,inquiry)
        prior=c.execute('SELECT * FROM inquiry_commits WHERE inquiry_id=? AND request_id=?',(inquiry,req)).fetchone()
        if prior:
            if prior['payload_hash'] != ph: raise ValueError('request_id_conflict')
            c.commit(); return json.loads(prior['result_json'])
        owner=c.execute('SELECT * FROM gemini_chats WHERE inquiry_id=?',(inquiry,)).fetchone()
        if gemini_lease is not None:
            if not owner or owner['lease']!=gemini_lease or owner['lease_until']<=time.time():
                raise ValueError('stale_lease')
        elif owner:
            raise ValueError('wrong_chat_provider')
        _check_buyer_text(c,inquiry,reply,allow_scoping_question=True)
        latest=c.execute("SELECT COALESCE(MAX(seq),0) FROM inquiry_turns WHERE inquiry_id=? AND sender='buyer'",(inquiry,)).fetchone()[0]
        media=None
        if analysis:
            media=c.execute("SELECT n.run_id,r.status FROM media_notifications n JOIN reference_runs r ON r.id=n.run_id JOIN reference_assets a ON a.id=r.asset_id WHERE n.run_id=? AND n.inquiry_id=? AND n.status=? AND a.inquiry_id=?",(analysis,inquiry,'gemini' if gemini_lease else 'queued',inquiry)).fetchone()
            if not media: raise ValueError('stale_reference_update')
        if decline_evaluation:
            priced = c.execute('SELECT e.verdict,p.revision FROM priced_briefs p JOIN evaluations e ON e.offer_id=p.offer_id WHERE p.inquiry_id=? AND e.id=?', (inquiry,decline_evaluation)).fetchone()
            current=c.execute('SELECT e.id FROM priced_briefs p JOIN evaluations e ON e.offer_id=p.offer_id WHERE p.inquiry_id=? ORDER BY p.rowid DESC LIMIT 1',(inquiry,)).fetchone()
            if not priced or not current or current['id'] != decline_evaluation or priced['verdict'] != 'decline' or priced['revision'] != rev or changes or media:
                raise ValueError('decline_question_requires_current_decline')
        if latest!=seq or row['revision']!=rev or (seq<row['processed_seq'] if media or decline_evaluation else seq<=row['processed_seq']): raise ValueError('stale_revision')
        proposal_email = args.get('proposal_email')
        if proposal_email is not None:
            import delivery
            if not isinstance(proposal_email, dict) or set(proposal_email) != {'email', 'confirmed'}:
                raise ValueError('invalid_proposal_email')
            email = delivery.validate_email(proposal_email['email'])
            current_body = c.execute("SELECT body FROM inquiry_turns WHERE inquiry_id=? AND seq=? AND sender='buyer'", (inquiry, seq)).fetchone()
            old_email = c.execute('SELECT email FROM proposal_recipients WHERE inquiry_id=?', (inquiry,)).fetchone()
            if not current_body or (email.casefold() not in current_body['body'].casefold() and
                    not (old_email and email == old_email['email'] and re.fullmatch(r"(?:yes|correct|yes,? that's right|that's right)[.! ]*", current_body['body'].strip(), re.I))):
                raise ValueError('email_confirmation_not_grounded')
            delivery.save_recipient(c, row, email, proposal_email['confirmed'], seq)
        source_body = c.execute("SELECT body FROM inquiry_turns WHERE inquiry_id=? AND seq=? AND sender='buyer'",
                                (inquiry, seq)).fetchone() if changes else None
        for f in changes:
            if not isinstance(f,dict) or not isinstance(f.get('subject'),str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,99}',f['subject']) or f.get('status') not in ('proposed','confirmed','excluded') or 'value' not in f or f.get('source_sequence') != seq: raise ValueError('invalid_fact')
            packed(f['value'])
            if re.search(r'(?:private|internal|floor|margin|capacity|hourly|cost|rate|profit)',f['subject'],re.I): raise ValueError('private_information')
            if not (source_body and grounded_date(f['subject'], f['value'], source_body[0])):
                _check_buyer_text(c,inquiry,packed(f['value']))
            if f.get('supersedes') and not c.execute('SELECT 1 FROM inquiry_facts WHERE id=? AND inquiry_id=? AND subject=?',(f['supersedes'],inquiry,f['subject'])).fetchone(): raise ValueError('invalid_fact_source')
        grounded=[]
        for cue in cues:
            if not isinstance(cue,dict) or set(cue)!={'observation_id','body','uncertainty'} or not all(isinstance(cue.get(k),str) and cue[k].strip() for k in cue) or len(cue['body'])>2000 or len(cue['uncertainty'])>1000: raise ValueError('invalid_reference_cue')
            source=c.execute("SELECT o.* FROM reference_observations o JOIN reference_runs r ON r.id=o.run_id JOIN reference_assets a ON a.id=r.asset_id WHERE o.id=? AND o.run_id=? AND a.inquiry_id=?",(cue['observation_id'],analysis,inquiry)).fetchone()
            if not source: raise ValueError('invalid_reference_cue')
            if gemini_lease:
                _check_buyer_text(c, inquiry, cue['body'] + ' ' + cue['uncertainty'])
            grounded.append((cue,source))
        if changes or (gemini_lease and not decline_evaluation): rev+=1
        for f in changes:
            c.execute('INSERT INTO inquiry_facts VALUES (?,?,?,?,?,?,?,?,?)',(uid(),inquiry,rev,f['subject'],packed(f['value']),f['status'],seq,f.get('supersedes'),time.time()))
        for cue,source in grounded:
            method = 'Gemini agent cue' if gemini_lease else 'QwenWork agent cue'
            c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),analysis,source['start'],source['end'],'production_cue',cue['body'],method,cue['uncertainty'],source['frame'],source['id'],time.time()))
        next_seq=c.execute('SELECT COALESCE(MAX(seq),0)+1 FROM inquiry_turns WHERE inquiry_id=?',(inquiry,)).fetchone()[0]
        c.execute('INSERT INTO inquiry_turns VALUES (?,?,?,?,?,?,?,?)',(inquiry,next_seq,'assistant',reply,'[]','agent:'+req,ph,time.time()))
        c.execute('UPDATE inquiries SET revision=?,processed_seq=? WHERE id=?',(rev,seq,inquiry))
        c.execute("UPDATE inquiry_jobs SET status='done',lease_until=NULL,lease_owner=NULL WHERE inquiry_id=? AND seq<=?",(inquiry,seq))
        if media: c.execute("UPDATE media_notifications SET status='done' WHERE run_id=? AND inquiry_id=?",(analysis,inquiry))
        result={'sequence':next_seq,'revision':rev}; c.execute('INSERT INTO inquiry_commits VALUES (?,?,?,?)',(inquiry,req,ph,packed(result)))
        c.commit(); return result
    except Exception: c.rollback(); raise

def pending(c):
    items={r['inquiry_id']:dict(r) for r in c.execute("SELECT inquiry_id,MAX(seq) AS through_sequence FROM inquiry_jobs WHERE status='queued' OR (status='leased' AND lease_until<?) GROUP BY inquiry_id",(time.time(),))}
    for r in c.execute("SELECT n.inquiry_id,n.run_id,r.status FROM media_notifications n JOIN reference_runs r ON r.id=n.run_id WHERE n.status='queued'"):
        item=items.setdefault(r['inquiry_id'],{'inquiry_id':r['inquiry_id']})
        item.setdefault('media_updates',[]).append({'analysis_id':r['run_id'],'status':r['status']})
    return [item for item in items.values() if not c.execute('SELECT 1 FROM gemini_chats WHERE inquiry_id=?',(item['inquiry_id'],)).fetchone()]

def acknowledge_media(c,args):
    with c:
        cur=c.execute("UPDATE media_notifications SET status='done' WHERE run_id=? AND inquiry_id=?",(args['analysis_id'],args['inquiry_id']))
        if not cur.rowcount: raise ValueError('not_found')
    return {'acknowledged':True}

def claim_work(c,args):
    """Lease one inquiry's pending turns to a single worker.

    Prevents two agents from drafting replies to the same buyer turn. A
    commit marks the jobs done; an explicit release or lease expiry returns
    them to the queue. Re-claiming by the same worker refreshes the lease.
    """
    inquiry=args['inquiry_id']; worker=args.get('worker_id'); seconds=args.get('lease_seconds',300)
    if not isinstance(worker,str) or not 1<=len(worker)<=128: raise ValueError('invalid_worker_id')
    if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not 1<=seconds<=3600: raise ValueError('invalid_lease_seconds')
    try:
        _begin(c); _get(c,inquiry); now=time.time()
        if c.execute('SELECT 1 FROM gemini_chats WHERE inquiry_id=?',(inquiry,)).fetchone():
            raise ValueError('wrong_chat_provider')
        if c.execute("SELECT 1 FROM inquiry_jobs WHERE inquiry_id=? AND status='leased' AND lease_until>? AND lease_owner IS NOT NULL AND lease_owner!=?",(inquiry,now,worker)).fetchone():
            raise ValueError('already_leased')
        rows=c.execute("SELECT seq FROM inquiry_jobs WHERE inquiry_id=? AND (status='queued' OR (status='leased' AND (lease_until<=? OR lease_owner=?)))",(inquiry,now,worker)).fetchall()
        if not rows: c.commit(); return {'inquiry_id':inquiry,'through_sequence':None,'lease_until':None}
        until=now+seconds
        c.execute("UPDATE inquiry_jobs SET status='leased',lease_until=?,lease_owner=? WHERE inquiry_id=? AND (status='queued' OR (status='leased' AND (lease_until<=? OR lease_owner=?)))",(until,worker,inquiry,now,worker))
        c.commit()
        return {'inquiry_id':inquiry,'through_sequence':max(r['seq'] for r in rows),'lease_until':until}
    except Exception: c.rollback(); raise

def release_work(c,args):
    inquiry=args['inquiry_id']; worker=args.get('worker_id')
    if not isinstance(worker,str) or not 1<=len(worker)<=128: raise ValueError('invalid_worker_id')
    _get(c,inquiry)
    with c:
        cur=c.execute("UPDATE inquiry_jobs SET status='queued',lease_until=NULL,lease_owner=NULL WHERE inquiry_id=? AND status='leased' AND lease_owner=?",(inquiry,worker))
    return {'released':cur.rowcount}

def _storage(c):
    name=c.execute('PRAGMA database_list').fetchone()[2]
    if not name: raise ValueError('persistent_database_required')
    p=Path(name).resolve().parent / (Path(name).name+'.media')
    p.mkdir(mode=0o700,parents=True,exist_ok=True)
    return p

def upload(c,inquiry,data):
    _get(c,inquiry); name=data.get('filename'); encoded=data.get('data'); mime=data.get('mime')
    if not isinstance(name,str) or not 1<=len(name)<=200 or mime not in ('video/mp4','video/quicktime','video/x-m4v') or not isinstance(encoded,str) or len(encoded)>MAX_UPLOAD*4//3+8: raise ValueError('invalid_upload')
    try: raw=base64.b64decode(encoded,validate=True)
    except Exception: raise ValueError('invalid_upload')
    if not raw or len(raw)>MAX_UPLOAD: raise ValueError('invalid_upload')
    aid=uid(); root=_storage(c); target=root/(aid+'.video')
    try:
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f: f.write(raw)
        with c: c.execute('INSERT INTO reference_assets VALUES (?,?,?,?,?,?,?)',(aid,inquiry,name,mime,hashlib.sha256(raw).hexdigest(),str(target),time.time()))
    except Exception:
        target.unlink(missing_ok=True); raise
    run=start_analysis(c,{'asset_id':aid})
    return {'asset_id':aid,'analysis_id':run['id']}

def remove_reference(c,inquiry,args):
    """Let a client delete a reference they uploaded by mistake.

    Scoped to the caller's inquiry: removes the asset, its analysis runs,
    observations and queued media notifications, then the private media files.
    Historical turns keep their text; a removed asset can no longer be cited.
    Safe while a run is processing: the worker discards results for a missing run.
    """
    _get(c,inquiry); aid=args.get('asset_id')
    if not isinstance(aid,str) or not aid: raise ValueError('invalid_reference')
    try:
        _begin(c)
        asset=c.execute('SELECT id,path FROM reference_assets WHERE id=? AND inquiry_id=?',(aid,inquiry)).fetchone()
        if not asset: raise ValueError('not_found')
        runs=[r[0] for r in c.execute('SELECT id FROM reference_runs WHERE asset_id=?',(aid,))]
        for run in runs:
            c.execute('DELETE FROM reference_observations WHERE run_id=?',(run,))
            c.execute('DELETE FROM media_notifications WHERE run_id=?',(run,))
        c.execute('DELETE FROM reference_runs WHERE asset_id=?',(aid,))
        c.execute('DELETE FROM reference_assets WHERE id=?',(aid,))
        c.commit()
    except Exception: c.rollback(); raise
    root=_storage(c)
    try: Path(asset['path']).unlink(missing_ok=True)
    except OSError: pass
    for run in runs: shutil.rmtree(root/run,ignore_errors=True)
    return {'removed':aid}

def start_analysis(c,args):
    aid=args['asset_id']
    if not c.execute('SELECT 1 FROM reference_assets WHERE id=?',(aid,)).fetchone(): raise ValueError('not_found')
    existing=c.execute("SELECT id,status FROM reference_runs WHERE asset_id=? AND status IN ('queued','running','done') ORDER BY created_at DESC LIMIT 1",(aid,)).fetchone()
    if existing: return dict(existing)
    rid=uid()
    with c: c.execute('INSERT INTO reference_runs(id,asset_id,status,created_at) VALUES (?,?,?,?)',(rid,aid,'queued',time.time()))
    return {'id':rid,'status':'queued'}

def evidence(c,args):
    row=c.execute('SELECT id,asset_id,status,result_json,error FROM reference_runs WHERE id=?',(args['analysis_id'],)).fetchone()
    if not row: raise ValueError('not_found')
    result=dict(row); result['result']=json.loads(result.pop('result_json') or 'null')
    result['observations']=[dict(r) for r in c.execute('SELECT id,start,end,kind,body,method,uncertainty,frame,supersedes FROM reference_observations WHERE run_id=? ORDER BY start,created_at',(args['analysis_id'],))]
    return result

def reference_frame(c,args):
    row=c.execute('SELECT o.frame,r.id AS run_id FROM reference_observations o JOIN reference_runs r ON r.id=o.run_id WHERE o.id=?',(args['observation_id'],)).fetchone()
    if not row or not row['frame'] or not re.fullmatch(r'frame-[0-9]+\.jpg',row['frame']): raise ValueError('not_found')
    path=_storage(c)/row['run_id']/row['frame']
    if not path.is_file(): raise ValueError('not_found')
    return {'mime':'image/jpeg','data_base64':base64.b64encode(path.read_bytes()).decode('ascii')}

def correct(c,args):
    original=c.execute('SELECT * FROM reference_observations WHERE id=?',(args['observation_id'],)).fetchone()
    body=args.get('body'); reason=args.get('reason')
    if not original: raise ValueError('not_found')
    if not isinstance(body,str) or not body.strip() or len(body)>2000 or not isinstance(reason,str) or not reason.strip() or len(reason)>1000: raise ValueError('invalid_correction')
    oid=uid()
    with c: c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',(oid,original['run_id'],original['start'],original['end'],original['kind'],body,'QwenWork agent correction',reason,original['frame'],original['id'],time.time()))
    return {'observation_id':oid}

def ffmpeg_executable():
    executable = os.environ.get('BIDPILOT_FFMPEG') or shutil.which('ffmpeg')
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


def process_media_once(db_path):
    """Claim one queued/stale job and decode with AVFoundation. Safe to call from a background worker."""
    import db
    c=db.connect(db_path)
    try:
        _begin(c)
        r=c.execute("SELECT r.id,r.asset_id,a.path,a.inquiry_id FROM reference_runs r JOIN reference_assets a ON a.id=r.asset_id WHERE r.status='queued' OR (r.status='running' AND r.lease_until<?) ORDER BY r.created_at LIMIT 1",(time.time(),)).fetchone()
        if not r: c.commit(); return False
        lease=time.time()+180
        c.execute("UPDATE reference_runs SET status='running',lease_until=? WHERE id=?",(lease,r['id'])); c.commit()
        def renew():
            # Long pipelines (decode + OCR + transcription) can outlive one
            # lease. Renew between stages; a lost lease means another worker
            # owns the run and this pass must discard its result.
            nonlocal lease
            fresh=time.time()+180
            _begin(c)
            cur=c.execute("UPDATE reference_runs SET lease_until=? WHERE id=? AND status='running' AND lease_until=?",(fresh,r['id'],lease))
            if not cur.rowcount: c.rollback(); return False
            c.commit(); lease=fresh; return True
        directory=_storage(c)/r['id']; directory.mkdir(mode=0o700,exist_ok=True)
        try:
            env = os.environ.copy()
            ffmpeg = ffmpeg_executable()
            has_audio = None
            if ffmpeg:
                probe=subprocess.run([ffmpeg,'-hide_banner','-i',r['path']],capture_output=True,text=True,timeout=20)
                match=re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)',probe.stderr)
                if not match or 'Video:' not in probe.stderr: raise ValueError('invalid_media')
                has_audio = 'Audio:' in probe.stderr
                duration=int(match.group(1))*3600+int(match.group(2))*60+float(match.group(3))
                if not 0<duration<=180: raise ValueError('invalid_duration')
                proc=subprocess.run([ffmpeg,'-hide_banner','-i',r['path'],'-vf',"fps=2,select='gt(scene,0.26)+not(mod(n,10))',showinfo",'-frames:v','40','-fps_mode','vfr',str(directory/'frame-%d.jpg')],capture_output=True,text=True,timeout=90,check=True)
                times=[float(x) for x in re.findall(r'pts_time:([0-9.]+)',proc.stderr)]
                frames=[{'time':t,'file':f'frame-{i+1}.jpg','ocr':'','cut':False} for i,t in enumerate(times) if (directory/f'frame-{i+1}.jpg').is_file()]
                if not frames: raise ValueError('decode_failed')
                result={'duration':duration,'provider':'ffmpeg','version':'scene-v1','cut_detection':'Frame selection includes approximate scene changes and regular samples','ocr':{'status':'unavailable'}}
                tesseract=os.environ.get('BIDPILOT_TESSERACT') or shutil.which('tesseract')
                if tesseract:
                    try:
                        for frame in frames:
                            recognized=subprocess.run([tesseract,str(directory/frame['file']),'stdout'],capture_output=True,text=True,timeout=15,check=True)
                            frame['ocr']=recognized.stdout.strip()
                        result['ocr']={'status':'complete','provider':'tesseract'}
                    except (OSError,subprocess.SubprocessError): result['ocr']={'status':'failed','provider':'tesseract'}
                elif sys.platform == 'darwin':
                    try:
                        recognized=subprocess.run(['swift',str(Path(__file__).with_name('media_probe.swift')),'--ocr-frames',str(directory)],capture_output=True,text=True,timeout=60,check=True,env=env)
                        by_file={x['file']:x.get('text','') for x in json.loads(recognized.stdout).get('frames',[]) if isinstance(x,dict) and isinstance(x.get('file'),str)}
                        for frame in frames: frame['ocr']=by_file.get(frame['file'],'')
                        result['ocr']={'status':'complete','provider':'Vision'}
                    except (OSError,subprocess.SubprocessError,ValueError,KeyError,json.JSONDecodeError):
                        result['ocr']={'status':'failed','provider':'Vision'}
                else:
                    result['ocr']={'status':'unavailable','reason':'No local OCR adapter available'}
            else:
                proc=subprocess.run(['swift',str(Path(__file__).with_name('media_probe.swift')),r['path'],str(directory)],capture_output=True,timeout=120,check=True,env=env)
                result=json.loads(proc.stdout); frames=result.pop('frames')
            result['transcription']={'status':'unavailable','reason':'No local transcription adapter configured'}
            prefix = Path(sys.prefix)
            bundled_cli = prefix / 'bin' / 'whisper-cli'
            bundled_model = prefix / 'share' / 'bidpilot' / 'ggml-tiny.en.bin'
            whisper = os.environ.get('BIDPILOT_WHISPER_CLI') or (str(bundled_cli) if bundled_cli.is_file() else shutil.which('whisper-cli'))
            model = os.environ.get('BIDPILOT_WHISPER_MODEL') or (str(bundled_model) if bundled_model.is_file() else None)
            if not renew(): return True
            if whisper and model:
                if has_audio is False:
                    result['transcription']={'status':'no_audio','provider':'none','reason':'Reference contains no audio stream'}
                else:
                    out=directory/'transcript'
                    wav=directory/'audio.wav'
                    converter=[ffmpeg,'-y','-i',r['path'],'-vn','-ac','1','-ar','16000',str(wav)] if ffmpeg else ['/usr/bin/afconvert','-f','WAVE','-d','LEI16@16000',r['path'],str(wav)]
                    try:
                        subprocess.run(converter,capture_output=True,timeout=30,check=True)
                    except subprocess.CalledProcessError as exc:
                        stderr=(exc.stderr or b'').decode('utf-8','replace')
                        if 'does not contain any stream' in stderr or 'no audio' in stderr.lower():
                            result['transcription']={'status':'no_audio','provider':'none','reason':'Reference contains no audio stream'}
                        else:
                            result['transcription']={'status':'failed','provider':'whisper.cpp','step':'audio_extraction','exit_code':exc.returncode}
                    except (OSError,subprocess.TimeoutExpired) as exc:
                        result['transcription']={'status':'failed','provider':'whisper.cpp','step':'audio_extraction','error':type(exc).__name__}
                    else:
                        try:
                            subprocess.run([whisper,'-m',model,'-f',str(wav),'-oj','-of',str(out)],capture_output=True,timeout=90,check=True)
                            transcript=json.loads(out.with_suffix('.json').read_text())
                            result['transcription']={'status':'complete','provider':'whisper.cpp','segments':transcript.get('transcription',[])}
                        except subprocess.CalledProcessError as exc:
                            result['transcription']={'status':'failed','provider':'whisper.cpp','step':'transcription','exit_code':exc.returncode}
                        except (OSError,subprocess.TimeoutExpired,ValueError,json.JSONDecodeError) as exc:
                            result['transcription']={'status':'failed','provider':'whisper.cpp','step':'transcription','error':type(exc).__name__}
            if not renew(): return True
            _begin(c)
            if not c.execute("SELECT 1 FROM reference_runs WHERE id=? AND status='running' AND lease_until=?",(r['id'],lease)).fetchone():
                c.rollback(); return True
            c.execute('DELETE FROM reference_observations WHERE run_id=?',(r['id'],))
            for index,frame in enumerate(frames):
                start=float(frame['time']); end=float(frames[index+1]['time']) if index+1<len(frames) else float(result['duration'])
                c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),r['id'],start,end,'frame','Possible visual cut' if frame.get('cut') else 'Representative frame',('AVFoundation frame difference' if frame.get('cut') else 'AVFoundation sampled frame') if result['provider'].startswith('AVFoundation') else 'FFmpeg sampled frame','Approximate visual cut; may miss or mistake transitions' if frame.get('cut') else 'Sample only; not a shot boundary',frame['file'],None,time.time()))
                if frame.get('ocr'):
                    provider=result.get('ocr',{}).get('provider')
                    method='Tesseract OCR' if provider=='tesseract' else ('Vision OCR' if provider else 'OCR')
                    c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),r['id'],start,end,'ocr',frame['ocr'][:4000],method,'Machine recognition may be inaccurate',frame['file'],None,time.time()))
            for segment in result['transcription'].get('segments',[]):
                offsets=segment.get('offsets',{})
                try:
                    start=float(offsets['from'])/1000; end=float(offsets['to'])/1000
                    body=segment['text'].strip()
                    if body and 0<=start<=end<=float(result['duration'])+0.1:
                        c.execute('INSERT INTO reference_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid(),r['id'],start,end,'speech',body[:4000],'whisper.cpp','Machine transcription may be inaccurate',None,None,time.time()))
                except (KeyError,TypeError,ValueError): pass
            c.execute("UPDATE reference_runs SET status='done',result_json=?,lease_until=NULL WHERE id=?",(packed(result),r['id']))
            c.execute("INSERT OR IGNORE INTO media_notifications(run_id,inquiry_id) VALUES (?,?)",(r['id'],r['inquiry_id'])); c.commit()
        except Exception as exc:
            c.rollback()
            with c:
                cur=c.execute("UPDATE reference_runs SET status='failed',error=?,lease_until=NULL WHERE id=? AND status='running' AND lease_until=?",('Media could not be decoded or processing timed out ('+type(exc).__name__+')',r['id'],lease))
                if cur.rowcount: c.execute("INSERT OR IGNORE INTO media_notifications(run_id,inquiry_id) VALUES (?,?)",(r['id'],r['inquiry_id']))
        return True
    finally: c.close()

def _binary(h,path,mime):
    size=os.path.getsize(path); start=0; end=size-1; status=200
    header=h.headers.get('Range')
    if header:
        match=re.fullmatch(r'bytes=(\d+)-(\d*)',header)
        if not match: h.send_error(416); return
        start=int(match.group(1)); end=min(int(match.group(2)) if match.group(2) else end,end)
        if start>end: h.send_error(416); return
        status=206
    h.send_response(status); h.send_header('Content-Type',mime); h.send_header('Cache-Control','no-store'); h.send_header('X-Content-Type-Options','nosniff'); h.send_header('Accept-Ranges','bytes'); h.send_header('Content-Length',str(end-start+1))
    if status==206: h.send_header('Content-Range',f'bytes {start}-{end}/{size}')
    h.end_headers()
    with open(path,'rb') as f:
        f.seek(start); remaining=end-start+1
        while remaining:
            chunk=f.read(min(65536,remaining))
            if not chunk: break
            h.wfile.write(chunk); remaining-=len(chunk)

_UI_CSS = r''':root{--s-paper:#faf7f3;--s-ink:#2a1f1a;--s-mute:#7a6c63;--s-line:#e4dcd3;--s-brown:#7b4b2f;--s-tint:#f1e9e1;--s-dark:#3a2a21;--ok:#2c7a3f;--bad:#b3261e;--display:"Newsreader","Iowan Old Style",Georgia,serif;--ui:"Instrument Sans","Helvetica Neue",Arial,sans-serif;color-scheme:light}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;background:var(--s-paper);color:var(--s-ink);font:15px/1.5 var(--ui);-webkit-font-smoothing:antialiased}
button,input,textarea{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--s-brown);outline-offset:2px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.wrap{display:grid;gap:16px;padding:16px;max-width:1120px;margin:0 auto}
@media(min-width:860px){.wrap{grid-template-columns:minmax(0,1.6fr) minmax(280px,1fr);align-items:start}}
.chat{background:#fff;border:1px solid var(--s-line);border-radius:10px;display:flex;flex-direction:column;min-width:0;height:min(660px,calc(100vh - 32px));min-height:460px}
.chat-head{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--s-line)}
.chat-head b{font-size:13.5px}.chat-head .sub{font-size:12.5px;color:var(--s-mute)}
.ava{width:30px;height:30px;border-radius:50%;background:var(--s-dark);color:#fff;font:600 12px/30px var(--ui);text-align:center;flex:none}
.live{margin-left:auto;font-size:11.5px;color:var(--ok);display:inline-flex;align-items:center;gap:6px}
.live::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--ok)}
.msgs{flex:1;overflow-y:auto;padding:14px;display:grid;gap:10px;align-content:start}
.m{max-width:86%;padding:9px 12px;border-radius:12px;font-size:13.5px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere;justify-self:start}
.m.bot{background:var(--s-tint);border-top-left-radius:4px}
.m.me{background:var(--s-dark);color:#fff;justify-self:end;border-top-right-radius:4px}
.m .refs{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;white-space:normal}
.m .refs button{font:600 11.5px var(--ui);border:1px solid rgba(255,255,255,.35);background:transparent;color:inherit;border-radius:999px;padding:3px 9px;cursor:pointer}
.m.bot .refs button{border-color:var(--s-line);background:#fff}
.typing{display:inline-flex;gap:4px;align-items:center}
.typing i{width:6px;height:6px;border-radius:50%;background:var(--s-mute);animation:blink 1.2s infinite both}
.typing i:nth-child(2){animation-delay:.2s}.typing i:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}
@media (prefers-reduced-motion:reduce){.typing i{animation:none;opacity:.6}}
.quick{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 12px}
.quick button{font:500 12px var(--ui);border:1px solid var(--s-line);border-radius:999px;padding:4px 10px;background:#fff;color:var(--s-ink);cursor:pointer}
.quick button:hover{border-color:var(--s-brown)}
.status{font-size:12.5px;color:var(--s-mute);padding:0 14px;margin:0 0 8px}
.status:empty{display:none}
.chipfile{display:flex;align-items:center;gap:8px;margin:0 14px 8px;font-size:12.5px;padding:5px 6px 5px 10px;border-radius:999px;background:var(--s-tint);justify-self:start;align-self:flex-start;max-width:calc(100% - 28px)}
.chipfile span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chipfile button{border:0;background:#fff;border-radius:50%;width:22px;height:22px;line-height:1;cursor:pointer;flex:none}
.compose{display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--s-line);align-items:flex-end}
.compose textarea{flex:1;min-width:0;border:1px solid var(--s-line);border-radius:8px;padding:7px 10px;font:13.5px/1.4 var(--ui);color:var(--s-ink);background:#fff;resize:none;height:36px;max-height:140px}
.compose textarea::placeholder{color:var(--s-mute)}
.clip{position:relative;width:36px;height:36px;border:1px solid var(--s-line);border-radius:8px;display:grid;place-items:center;font-size:18px;color:var(--s-mute);cursor:pointer;flex:none;background:#fff}
.clip:hover{border-color:var(--s-brown);color:var(--s-ink)}
.clip input{position:absolute;inset:0;opacity:0;width:100%;height:100%;cursor:pointer}
.clip:focus-within{outline:2px solid var(--s-brown);outline-offset:2px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border-radius:6px;padding:9px 14px;font:600 13.5px var(--ui);border:1px solid transparent;cursor:pointer;white-space:nowrap;text-decoration:none}
.btn.p{background:var(--s-brown);color:#fff}
.btn.g{border-color:var(--s-line);background:#fff;color:var(--s-ink)}
.btn:disabled{opacity:.5;cursor:default}
.side{display:grid;gap:16px;align-content:start;min-width:0}
.card{background:#fff;border:1px solid var(--s-line);border-radius:10px;padding:16px;display:grid;gap:12px;min-width:0}
.card h2{font-family:var(--display);font-weight:500;font-size:20px;line-height:1.2;margin:0}
.card-top{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.small{font-size:12.5px}.muted{color:var(--s-mute)}
.fill{height:6px;background:var(--s-tint);border-radius:3px;overflow:hidden}.fill i{display:block;height:100%;width:0;background:var(--s-brown);border-radius:3px;transition:width .4s}
dl.brief{margin:0;display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px}
dl.brief dt{color:var(--s-mute)}dl.brief dd{margin:0;font-weight:600;text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
dl.brief dd.muted{font-weight:400;color:var(--s-mute)}
dl.brief dd em{display:block;font-style:normal;font-weight:400;font-size:11.5px;color:var(--s-mute)}
.lock{font-size:12px;color:var(--s-mute);display:flex;gap:8px;align-items:flex-start;margin:0}
.lock::before{content:"";flex:none;width:10px;height:12px;margin-top:3px;border:2px solid currentColor;border-radius:2px;box-shadow:inset 0 -4px 0 currentColor}
dl.brief dd.miss{color:#a2620a;font-weight:600}
dl.brief dd.miss::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:#a2620a;margin-right:6px;vertical-align:1px}
.todo{background:#fbf0dc;border-radius:8px;padding:10px 12px;display:grid;gap:8px;font-size:12.5px}
.todo b{font-weight:600}.todo .chips{display:flex;flex-wrap:wrap;gap:6px}
.todo .chip{font:12px var(--ui);padding:4px 10px;border-radius:999px;background:#fff;border:1px solid var(--s-line);color:var(--s-ink);cursor:pointer}
.todo .chip:hover{border-color:var(--s-brown)}
.why{font-size:12px;color:var(--s-mute);text-align:center;margin:-4px 0 0}
.ready{background:#e4f3e7;color:var(--ok);border-radius:8px;padding:8px 12px;font-size:12.5px;font-weight:600;margin:0}
#sendbrief{width:100%}
#sendbrief.sub,#sendbrief.sub:disabled{background:#e4f3e7;color:var(--ok);border-color:#bfe0c7;opacity:1}
#sendbrief.sub::before{content:"\2713";font-weight:700}
.done{display:grid;gap:6px;background:#e4f3e7;border:1px solid #bfe0c7;border-radius:8px;padding:12px}
.done .tick{color:var(--ok);font-size:14px}.done p{margin:0;font-size:12.5px}
[hidden]{display:none!important}
.proposal{display:grid;gap:8px;background:var(--s-tint);border-radius:8px;padding:12px}
.proposal a{color:var(--s-brown);font-weight:600}
.resume a{color:var(--s-brown);font-weight:600}
.asset{display:grid;gap:8px}
.asset video{width:100%;border-radius:6px;background:#000;max-height:260px}
.asset p{margin:0;font-size:12.5px;color:var(--s-mute);overflow-wrap:anywhere}
.ev{display:grid;gap:8px}
.ev div{display:flex;gap:6px;flex-wrap:wrap;align-items:flex-start;font-size:13px}
.ev .t{font:600 12px var(--ui);border:1px solid var(--s-line);background:var(--s-tint);border-radius:6px;padding:4px 8px;cursor:pointer;text-align:left;white-space:normal}
.linkbtn{font:600 12px var(--ui);color:var(--s-brown);background:none;border:0;padding:4px 0;cursor:pointer}
@media(max-width:859px){.chat{height:auto;min-height:0}.msgs{max-height:60vh;min-height:220px}}
'''

def _ui_head(title):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><title>' + title + '</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=Instrument+Sans:wght@400;500;600;700&display=swap">'
            '<style>' + _UI_CSS + '</style></head><body>')

def _ui_chat(heading, quick, placeholder='Type a message or drop a reference clip'):
    buttons = ''.join('<button type="button" data-quick="%s">%s</button>' % (html.escape(q), html.escape(q)) for q in quick)
    return ('<h1 class="sr">' + heading + '</h1><main class="wrap">'
            '<section class="chat" aria-label="Chat with the studio assistant">'
            '<div class="chat-head"><span class="ava" aria-hidden="true">AS</span><div><b>Studio assistant</b><div class="sub">Helps you scope your brief</div></div><span class="live">Live</span></div>'
            '<div class="msgs" id="turns" aria-live="polite"></div>'
            '<div class="quick" id="quick">' + buttons + '</div>'
            '<p class="status" id="status" role="status"></p><button class="btn g" type="button" id="retryassistant" hidden>Retry assistant</button>'
            '<div class="chipfile" id="chipfile" hidden><span id="chipname"></span><button type="button" id="chipclear" aria-label="Remove clip">&times;</button></div>'
            '<form class="compose" id="form"><label class="clip" title="Attach a reference clip (MP4 or MOV)">+<span class="sr">Attach a private video reference</span><input id="file" type="file" accept="video/mp4,video/quicktime,video/x-m4v"></label>'
            '<label class="sr" for="text">Your message</label><textarea id="text" rows="1" placeholder="' + placeholder + '"></textarea>'
            '<button class="btn p" id="send">Send</button></form></section>')

_UI_BRIEF = ('<aside class="side"><section class="card" aria-labelledby="briefh"><div class="card-top"><h2 id="briefh">Your brief so far</h2><span class="small muted" id="count">0 of 10 required</span></div>'
             '<div class="fill" aria-hidden="true"><i id="fillbar"></i></div><dl class="brief" id="facts"></dl>'
             '<div class="todo" id="todo" hidden></div><p class="ready" id="ready" hidden>Your brief is complete.</p>'
             '<button class="btn p" type="button" id="sendbrief" disabled aria-describedby="why">Send my brief</button>'
             '<p class="why" id="why" hidden></p><div class="done" id="done" hidden role="status"></div>'
             '<p class="lock">Only you and I see your budget. No price is shown until I\'ve reviewed your proposal myself.</p>'
             '<div id="proposal"></div></section>')

_UI_COMMON_JS_RAW = r'''
const FIELDS=__BRIEF_FIELDS__.map(([n,p])=>[n,new RegExp(p)]);
function label(s){s=String(s).split('.').pop().replace(/[_-]+/g,' ');return s.charAt(0).toUpperCase()+s.slice(1)}
function show(v){if(v===null||v===undefined)return '';if(typeof v==='boolean')return v?'Yes':'No';if(Array.isArray(v))return v.map(show).join(', ');if(typeof v==='object')return Object.entries(v).map(([k,x])=>label(k)+': '+show(x)).join(', ');return String(v)}
function renderBrief(facts){let gone=new Set(facts.map(f=>f.supersedes).filter(Boolean)),latest=new Map();for(let f of facts)if(!gone.has(f.id))latest.set(f.subject,f);let rows=FIELDS.map(([name])=>({name,fact:null})),extra=[];for(let f of latest.values()){let i=FIELDS.findIndex(([,re])=>re.test(f.subject));if(i>=0&&!rows[i].fact)rows[i].fact=f;else extra.push({name:label(f.subject),fact:f})}let dl=document.querySelector('#facts');dl.replaceChildren();let missing=[],req=FIELDS.filter(([n])=>!OPTIONAL.has(n)).length;for(let r of rows.concat(extra)){let dt=document.createElement('dt'),dd=document.createElement('dd'),must=rows.includes(r)&&!OPTIONAL.has(r.name);dt.textContent=r.name;if(!r.fact){if(must){dd.className='miss';dd.textContent='Missing';missing.push(r.name)}else{dd.className='muted';dd.textContent=r.name==='Your budget'?'Optional':'Ask in chat'}}else{dd.textContent=r.fact.status==='excluded'?'Not needed':show(r.fact.value);if(r.fact.status==='proposed'){let e=document.createElement('em');e.textContent='to confirm';dd.append(e);if(must)missing.push('Confirm '+r.name.toLowerCase())}}dl.append(dt,dd)}document.querySelector('#count').textContent=(req-missing.length)+' of '+req+' required';document.querySelector('#fillbar').style.width=Math.round((req-missing.length)/req*100)+'%';return missing}
const OPTIONAL=new Set(['Your budget']);
function briefUI(missing,o){o=o||{};let btn=document.querySelector('#sendbrief'),todo=document.querySelector('#todo'),why=document.querySelector('#why'),ready=document.querySelector('#ready'),done=document.querySelector('#done'),card=document.querySelector('#briefh');
let submitted=!!o.submitted;card.textContent=submitted?'Your submitted brief':'Your brief so far';document.querySelector('.fill').hidden=submitted;document.querySelector('#count').hidden=submitted;
btn.classList.toggle('sub',submitted);btn.classList.toggle('p',!submitted);
todo.replaceChildren();todo.hidden=submitted||!missing.length;ready.hidden=true;why.hidden=true;done.hidden=!submitted;
if(submitted){btn.disabled=true;btn.textContent='Brief submitted';done.replaceChildren();let t=document.createElement('b');t.className='tick';t.textContent='Alex has your brief';let p=document.createElement('p');p.textContent=(o.receipt?'Receipt '+o.receipt.slice(0,8).toUpperCase()+'. ':'')+'You\'ll get a written proposal by email, usually within 1 working day. No price or booking is confirmed yet. Questions or changes? Tell the assistant.';done.append(t,p);return}
btn.textContent=o.resubmit?'Send my updated brief':'Send my brief';
if(missing.length){let h=document.createElement('b');h.textContent=missing.length+(missing.length===1?' thing':' things')+' left before you can send:';let w=document.createElement('div');w.className='chips';for(let m of missing){let b=document.createElement('button');b.type='button';b.className='chip';b.textContent=m;b.onclick=()=>{ta.value=(m.startsWith('Confirm ')?'About the '+m.slice(8)+': ':m+': ');grow();ta.focus()};w.append(b)}todo.append(h,w);btn.disabled=true;why.hidden=false;why.textContent='Answer '+(missing.length===1?'this':'these')+' in the chat to unlock.';return}
btn.disabled=!!o.busy;if(o.busy){why.hidden=false;why.textContent='Wait for the assistant to finish updating your brief.'}else ready.hidden=false}
function bubble(who,text){let d=document.createElement('div');d.className='m '+who;d.textContent=text;return d}
function typing(){let d=document.createElement('div');d.className='m bot';d.setAttribute('aria-label','The studio assistant is replying');d.innerHTML='<span class="typing"><i></i><i></i><i></i></span>';return d}
const ta=document.querySelector('#text');if(ta.clientWidth&&ta.clientWidth<300)ta.placeholder='Message or drop a clip';function grow(){ta.style.height='36px';ta.style.height=Math.min(ta.scrollHeight+2,140)+'px'}ta.addEventListener('input',grow);
ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();document.querySelector('#form').requestSubmit()}});
const fileIn=document.querySelector('#file'),chip=document.querySelector('#chipfile');
function syncChip(){let f=fileIn.files[0];chip.hidden=!f;document.querySelector('#chipname').textContent=f?f.name:''}
document.querySelector('#chipclear').onclick=()=>{fileIn.value='';syncChip()};
function scrollEnd(){let m=document.querySelector('#turns');m.scrollTop=m.scrollHeight}
const TOKEN_KEY='bidpilot.inquiry.token';
function savedToken(){try{let t=localStorage.getItem(TOKEN_KEY)||'';return /^[A-Za-z0-9_-]{32,128}$/.test(t)?t:''}catch(e){return ''}}
function saveToken(t){try{localStorage.setItem(TOKEN_KEY,t)}catch(e){}}
function clearToken(){try{localStorage.removeItem(TOKEN_KEY)}catch(e){}}
'''
_UI_COMMON_JS = _UI_COMMON_JS_RAW.replace('__BRIEF_FIELDS__', json.dumps([[n,p] for n,p in BRIEF_FIELDS]))

def _start_page():
    return (_ui_head('Start an inquiry') + _ui_chat('Start a private inquiry', ['Social videos for a launch', 'A brand film', 'An interview or event']) + _UI_BRIEF +
            '<p class="small muted resume" id="resumebox" hidden><a id="resume">Continue your private inquiry</a></p></aside></main>'
            '<script>' + _UI_COMMON_JS + r'''
const turns=document.querySelector('#turns'),msg=document.querySelector('#status'),link=document.querySelector('#resume');
turns.append(bubble('bot','Hi! What are you making, and where will people watch it? Share a message, a video reference, or both.'));briefUI(renderBrief([]));
let resuming=false;
if(new URLSearchParams(location.search).has('new')){clearToken()}
else{let saved=savedToken();if(saved){resuming=true;msg.textContent='Resuming your conversation…';fetch('/i/'+saved+'/state').then(r=>{if(r.ok){location.replace('/i/'+saved)}else{clearToken();msg.textContent=''}}).catch(()=>{msg.textContent=''}).finally(()=>{resuming=false})}}
fileIn.onchange=syncChip;
document.querySelectorAll('[data-quick]').forEach(b=>b.onclick=()=>{ta.value=b.dataset.quick;grow();document.querySelector('#form').requestSubmit()});
let token='',busy=false;document.querySelector('#form').onsubmit=async e=>{e.preventDefault();if(busy||resuming)return;let text=ta.value,file=fileIn.files[0];if(!text.trim()&&!file){msg.textContent='Add a message or a video reference.';return}busy=true;document.querySelector('#send').disabled=true;msg.textContent='';if(text.trim())turns.append(bubble('me',text));if(file)turns.append(bubble('me','Reference clip: '+file.name));let t=typing();turns.append(t);scrollEnd();try{if(!token){let r=await fetch('/inquire',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,attachment_first:!!file,request_id:crypto.randomUUID()})});let x=await r.json();if(!r.ok)throw Error(x.error);token=x.token;saveToken(token);link.href='/i/'+token;document.querySelector('#resumebox').hidden=false;link.textContent='Continue your private inquiry (save this link)'}if(file){msg.textContent='Uploading…';let bytes=new Uint8Array(await file.arrayBuffer()),s='';for(let i=0;i<bytes.length;i+=32768)s+=String.fromCharCode(...bytes.subarray(i,i+32768));let r=await fetch('/i/'+token+'/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:file.name,mime:file.type,data:btoa(s)})});if(!r.ok)throw Error('Upload failed. Use the private link to return and retry.')}location.href='/i/'+token}catch(err){t.remove();busy=false;document.querySelector('#send').disabled=false;msg.textContent=err.message||'Could not start the conversation. Please try again.'}};
</script></body></html>''')

def _page():
    return (_ui_head('Your project brief') + _ui_chat('Tell us about your project', ['Not sure yet', 'Skip this one', 'Can you explain?']) + _UI_BRIEF +
            '<section class="card" id="refcard" hidden><h2>Your references</h2><div id="assets"></div><div class="ev" id="evidence"></div></section>'
            '<p class="small muted resume"><button class="btn g" type="button" id="newinquiry">Start a new inquiry</button></p></aside></main>'
            '<script>' + _UI_COMMON_JS + r'''
let base=location.pathname.replace(/\/$/,''); let token=base.split('/').pop(); const status=document.querySelector('#status');
saveToken(token);
let ni=document.querySelector('#newinquiry'),niUntil=0;
ni.onclick=()=>{if(Date.now()<niUntil){clearToken();location.href='/inquire?new=1';return}niUntil=Date.now()+4000;ni.textContent='Tap again to confirm (this chat keeps its link)';setTimeout(()=>{niUntil=0;ni.textContent='Start a new inquiry'},4000)};
async function request(url,opts){let r=await fetch(url,opts);let x=await r.json();if(!r.ok)throw Error(x.error||'Request failed');return x}
function seek(id,time){let v=[...document.querySelectorAll('video')].find(v=>v.dataset.id===id);if(v){v.currentTime=time;v.scrollIntoView({block:'nearest'});v.play().catch(()=>{})}}
let lastTurns='', respondingToUpdate=false;
async function refresh(){try{let x=await request(base+'/state');let key=JSON.stringify([x.turns,x.pending,x.assistant_error]);if(key!==lastTurns){lastTurns=key;let turns=document.querySelector('#turns');turns.replaceChildren();if(!x.turns.length)turns.append(bubble('bot','Hi! What are you making, and where will people watch it?'));for(let t of x.turns){let a=bubble(t.sender==='buyer'?'me':'bot',t.body);if(t.references.length){let w=document.createElement('div');w.className='refs';for(let ref of t.references){let b=document.createElement('button');b.type='button';b.textContent='Reference at '+(ref.start||0).toFixed(1)+'s';b.onclick=()=>seek(ref.asset_id,ref.start||0);w.append(b)}a.append(w)}turns.append(a)}if(x.pending&&!x.assistant_error)turns.append(typing());scrollEnd()}let missing=renderBrief(x.facts);
let card=document.querySelector('#refcard');card.hidden=!(x.assets.length||x.evidence.length);
let assets=document.querySelector('#assets');if(assets.dataset.ids!==JSON.stringify(x.assets.map(a=>[a.id,a.status]))){assets.dataset.ids=JSON.stringify(x.assets.map(a=>[a.id,a.status]));assets.replaceChildren();for(let a of x.assets){let box=document.createElement('div'),v=document.createElement('video');box.className='asset';v.controls=true;v.preload='metadata';v.dataset.id=a.id;v.src=base+'/asset/'+a.id;let b=document.createElement('button');b.type='button';b.className='btn g';b.textContent='Refer to this moment';b.onclick=()=>{ta.value+=' [Reference at '+v.currentTime.toFixed(1)+' seconds]';ta.dataset.ref=JSON.stringify([{asset_id:a.id,start:v.currentTime,end:v.currentTime}]);grow();ta.focus()};let lbl=document.createElement('p');lbl.textContent=a.filename+' — '+(a.status==='failed'?'Analysis failed; you can still describe this reference.':a.status||'processing');let rm=document.createElement('button');rm.type='button';rm.className='linkbtn';rm.style.color='var(--bad)';rm.textContent='Remove';let armed=false,armTimer=0;rm.onclick=async()=>{if(!armed){armed=true;rm.textContent='Tap again to remove';armTimer=setTimeout(()=>{armed=false;rm.textContent='Remove'},4000);return}clearTimeout(armTimer);rm.disabled=true;try{await request(base+'/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({asset_id:a.id})});if(ta.dataset.ref){try{let refs=JSON.parse(ta.dataset.ref).filter(r=>r.asset_id!==a.id);if(refs.length)ta.dataset.ref=JSON.stringify(refs);else delete ta.dataset.ref}catch(e){delete ta.dataset.ref}}await refresh();status.textContent='Reference removed.'}catch(e){rm.disabled=false;armed=false;rm.textContent='Remove';status.textContent=e.message}};box.append(lbl,v,b,rm);assets.append(box)}}
let ev=document.querySelector('#evidence');ev.replaceChildren();for(let o of x.evidence){let b=document.createElement('button');b.type='button';b.className='t';b.textContent=o.start.toFixed(1)+'s · '+o.body;b.onclick=()=>seek(o.asset_id,o.start);let correct=document.createElement('button');correct.type='button';correct.className='linkbtn';correct.textContent='Correct or discuss';correct.onclick=()=>{ta.dataset.ref=JSON.stringify([{asset_id:o.asset_id,start:o.start,end:o.end,observation_id:o.id}]);ta.value='About this observation: ';grow();ta.focus()};let row=document.createElement('div');row.append(b,correct);ev.append(row)}
let proposal=document.querySelector('#proposal');proposal.replaceChildren();if(x.proposal_url){let box=document.createElement('div');box.className='proposal';let a=document.createElement('a');a.href=x.proposal_url;a.textContent='View your approved proposal';box.append(a);proposal.append(box)}
window.briefState=x;
document.querySelector('#retryassistant').hidden=x.provider!=='gemini'||(!x.assistant_error&&!x.pending);
briefUI(x.missing||missing,{submitted:x.submitted,receipt:x.last_receipt&&x.last_receipt.id,resubmit:!x.submitted&&!!x.last_receipt,busy:!!(x.pending||x.media_update||x.assets.some(a=>['queued','running'].includes(a.status)))});
status.textContent=x.assistant_error||(x.pending?'Waiting for the assistant. You can return to this link.':'');
if((x.media_update||x.review_question)&&!x.assistant_error&&!respondingToUpdate){
  respondingToUpdate=true;
  try{await request(base+'/retry',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refresh()}
  finally{respondingToUpdate=false}
}
}catch(e){status.textContent=e.message}}
async function send(text,refs){let data={text,request_id:crypto.randomUUID(),references:refs||[]};await request(base+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})}
document.querySelector('#form').onsubmit=async e=>{e.preventDefault();if(!ta.value.trim()&&!ta.dataset.ref)return;try{await send(ta.value,JSON.parse(ta.dataset.ref||'[]'));ta.value='';delete ta.dataset.ref;grow();refresh()}catch(e){status.textContent=e.message}};
document.querySelectorAll('[data-quick]').forEach(b=>b.onclick=async()=>{try{await send(b.dataset.quick);refresh()}catch(e){status.textContent=e.message}});
document.querySelector('#retryassistant').onclick=async()=>{try{await request(base+'/retry',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refresh()}catch(e){status.textContent=e.message}};
document.querySelector('#sendbrief').onclick=async()=>{try{let x=window.briefState;let r=await request(base+'/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:x.revision,sequence:x.processed_sequence})});await refresh();status.textContent=''}catch(e){status.textContent=e.message==='brief_incomplete'?'A few brief items are still missing. Answer them in the chat first.':e.message}};
fileIn.onchange=async()=>{let f=fileIn.files[0];if(!f)return;syncChip();status.textContent='Uploading…';let bytes=new Uint8Array(await f.arrayBuffer()),s='';for(let i=0;i<bytes.length;i+=32768)s+=String.fromCharCode(...bytes.subarray(i,i+32768));try{await request(base+'/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:f.name,mime:f.type,data:btoa(s)})});fileIn.value='';syncChip();status.textContent='';refresh()}catch(e){status.textContent=e.message}};refresh();setInterval(()=>{let x=window.briefState;if(x&&(x.provider==='qwenwork'||x.submitted||x.media_update||x.assets.some(a=>['queued','running'].includes(a.status))))refresh()},5000);
</script></body></html>''')

def dispatch_get(h,path):
    if path=='/inquire':
        h.send_html(_start_page()); return True
    match=re.fullmatch(r'/i/([A-Za-z0-9_-]+)(?:/(state|asset/([a-f0-9-]+)))?',path)
    if not match: return False
    row=_by_token(h.conn,match.group(1))
    if not row: h.not_found(); return True
    action=match.group(2)
    if not action: h.send_html(_page()); return True
    if action=='state':
        import studio_assistant
        provider=studio_assistant.route(h.conn,row['id'])
        result=context(h.conn,{'inquiry_id':row['id']}); result['provider']=provider
        state=h.conn.execute('SELECT error FROM gemini_chats WHERE inquiry_id=?',(row['id'],)).fetchone()
        result['assistant_error']=state['error'] if state else None
        result['media_update'] = provider == 'gemini' and bool(h.conn.execute(
            "SELECT 1 FROM media_notifications WHERE inquiry_id=? AND status='gemini' LIMIT 1",
            (row['id'],)).fetchone())
        result['submitted'] = bool(h.conn.execute(
            'SELECT 1 FROM studio_submissions WHERE inquiry_id=? AND revision=? AND sequence=?',
            (row['id'], result['revision'], result['processed_sequence'])).fetchone())
        result['missing'] = brief_missing(result['facts'])
        last = h.conn.execute('SELECT id,created_at FROM studio_submissions WHERE inquiry_id=? ORDER BY created_at DESC LIMIT 1',
                              (row['id'],)).fetchone()
        result['last_receipt'] = dict(last) if last else None
        result['review_question'] = (provider == 'gemini' and result['submitted'] and bool(
            studio_assistant.decline_followup(h.conn, row['id'], result['revision'])))
        result.pop('pricing_history',None)
        result['assets']=[dict(x) for x in h.conn.execute('SELECT a.id,a.filename,r.status,r.error FROM reference_assets a LEFT JOIN reference_runs r ON r.asset_id=a.id WHERE a.inquiry_id=? ORDER BY a.created_at',(row['id'],))]; result['evidence']=[dict(x) for x in h.conn.execute("SELECT o.id,o.run_id,a.id AS asset_id,o.start,o.end,o.kind,o.body,o.method,o.uncertainty,o.supersedes FROM reference_observations o JOIN reference_runs r ON r.id=o.run_id JOIN reference_assets a ON a.id=r.asset_id WHERE a.inquiry_id=? ORDER BY o.start",(row['id'],))]; result['pending']=bool(h.conn.execute("SELECT 1 FROM inquiry_jobs WHERE inquiry_id=? AND status!='done'",(row['id'],)).fetchone())
        
        if row['deal_id']:
            import core
            if hasattr(core,'inquiry_proposal_url'):
                result['proposal_url']=core.inquiry_proposal_url(h.conn,row['deal_id'],match.group(1))
        h.send_json(result); return True
    asset=h.conn.execute('SELECT path,mime FROM reference_assets WHERE id=? AND inquiry_id=?',(match.group(3),row['id'])).fetchone()
    if not asset or not os.path.isfile(asset['path']): h.not_found()
    else: _binary(h,asset['path'],asset['mime'])
    return True

def dispatch_post(h,path):
    import studio_assistant
    match=re.fullmatch(r'/i/([A-Za-z0-9_-]+)/(messages|upload|remove|retry|submit)',path)
    if path!='/inquire' and not match: return False
    try:
        length=int(h.headers.get('Content-Length','0'))
        if length<0 or length>MAX_UPLOAD*4//3+10000: raise ValueError('body_too_large')
        data=h.body()
        if path=='/inquire':
            if not isinstance(data.get('text'),str) or (not data['text'].strip() and data.get('attachment_first') is not True): raise ValueError('message_required')
            result=create_inquiry(h.conn,data,provider=studio_assistant.configured_provider())
        else:
            row=_by_token(h.conn,match.group(1))
            if not row: h.not_found(); return True
            action=match.group(2)
            if action=='messages': result=add_message(h.conn,row['id'],data)
            elif action=='upload': result=upload(h.conn,row['id'],data)
            elif action=='remove': result=remove_reference(h.conn,row['id'],data)
            elif action=='submit':
                if brief_missing(context(h.conn,{'inquiry_id':row['id']})['facts']): raise ValueError('brief_incomplete')
                result=studio_assistant.submit_brief(h.conn,row['id'],data['revision'],data['sequence'])
            else: result={}
        inquiry=result.get('inquiry_id') if path=='/inquire' else row['id']
        provider=studio_assistant.route(h.conn,inquiry)
        result['provider']=provider
        if provider=='gemini' and (path=='/inquire' or action in ('messages','retry')):
            result.update(studio_assistant.respond(h.conn,inquiry))
        h.send_json({'ok':True,**result})
    except (ValueError,KeyError,TypeError) as exc:
        error=str(exc) or 'bad_request'; h.send_json({'ok':False,'error':error},409 if error in ('stale_revision','request_id_conflict') else 400)
    return True

def bind_deal(c,args):
    inquiry=args['inquiry_id']; deal=args['deal_id']
    try:
        _begin(c)
        row=_get(c,inquiry)
        if not c.execute("SELECT 1 FROM deals WHERE id=? AND source='site' AND source_id=?",(deal,'inquiry-'+inquiry)).fetchone(): raise ValueError('invalid_deal_source')
        if c.execute('SELECT 1 FROM inquiries WHERE deal_id=? AND id!=?',(deal,inquiry)).fetchone(): raise ValueError('already_bound')
        if row['deal_id'] and row['deal_id']!=deal: raise ValueError('already_bound')
        c.execute('UPDATE inquiries SET deal_id=? WHERE id=?',(deal,inquiry))
        import delivery
        delivery.bind_recipient(c, inquiry, deal)
        c.commit()
    except Exception:
        c.rollback(); raise
    return {'inquiry_id':inquiry,'deal_id':deal}

def ask_decline_question(c, args):
    """Save the agent's question once per evaluation, not a Python-selected question."""
    evaluation = args['evaluation_id']
    turn = {k: v for k, v in args.items() if k != 'evaluation_id'}
    turn.update(request_id='decline:'+evaluation, fact_changes=[])
    return commit(c, turn, decline_evaluation=evaluation)


MCP_TOOLS={
 'ask_decline_question':({'inquiry_id':{'type':'string'},'evaluation_id':{'type':'string'},'through_sequence':{'type':'integer'},'expected_revision':{'type':'integer'},'reply':{'type':'string'}},['inquiry_id','evaluation_id','through_sequence','expected_revision','reply'],ask_decline_question),
 'acknowledge_reference_update':({'inquiry_id':{'type':'string'},'analysis_id':{'type':'string'}},['inquiry_id','analysis_id'],acknowledge_media),
 'bind_inquiry_deal':({'inquiry_id':{'type':'string'},'deal_id':{'type':'string'}},['inquiry_id','deal_id'],bind_deal),
 'get_conversation_context':({'inquiry_id':{'type':'string'},'through_sequence':{'type':'integer'}},['inquiry_id'],context),
 'commit_chat_turn':({'inquiry_id':{'type':'string'},'through_sequence':{'type':'integer','minimum':0,'description':'Latest buyer sequence being answered. Use 0 for an attachment-only inquiry. This must equal the latest buyer sequence, not the processed_sequence returned by get_conversation_context.'},'expected_revision':{'type':'integer','minimum':0,'description':'Current conversation revision from get_conversation_context.'},'proposal_email':{'type':'object','additionalProperties':False,'required':['email','confirmed'],'properties':{'email':{'type':'string'},'confirmed':{'type':'boolean'}},'description':'Private validated recipient grounded in the latest buyer turn; false means ask confirmation, true means buyer explicitly supplied/confirmed or corrected it. Does not change priced scope.'},'fact_changes':{'type':'array','maxItems':50,'description':'Facts grounded in this buyer turn. proposed means requested or not yet verified; confirmed means the buyer explicitly verified it; excluded means the buyer explicitly ruled it out. These statuses do not approve price, availability, a proposal, or a booking.','items':{'type':'object','additionalProperties':False,'required':['subject','value','status','source_sequence'],'properties':{'subject':{'type':'string','pattern':'^[a-z][a-z0-9_.-]{0,99}$'},'value':{'description':'JSON value stated or verified by the buyer.'},'status':{'type':'string','enum':['proposed','confirmed','excluded']},'source_sequence':{'type':'integer','minimum':0,'description':'Buyer sequence that supports this fact; must equal through_sequence, including 0 for an attachment-only inquiry.'},'supersedes':{'type':'string','description':'Optional existing fact ID replaced by this fact.'}}}},'reply':{'type':'string','minLength':1,'maxLength':10000,'description':'Buyer-facing reply. It may clarify scope but cannot commit the studio to commercial terms or a booking.'},'request_id':{'type':'string','minLength':1,'maxLength':128,'description':'Stable idempotency key for this exact turn.'},'media_analysis_id':{'type':'string','description':'Queued reference analysis consumed atomically with this reply.'},'production_cues':{'type':'array','maxItems':50,'description':'Optional client-safe cues grounded in observations from media_analysis_id. Cues require media_analysis_id and create linked production-cue observations; they are not factual or commercial approval.','items':{'type':'object','additionalProperties':False,'required':['observation_id','body','uncertainty'],'properties':{'observation_id':{'type':'string','description':'Source observation ID from get_reference_evidence; it must belong to media_analysis_id.'},'body':{'type':'string','minLength':1,'maxLength':2000,'description':'Client-safe observable production cue.'},'uncertainty':{'type':'string','minLength':1,'maxLength':1000,'description':'What the evidence cannot establish or still needs clarification.'}}}}},['inquiry_id','through_sequence','expected_revision','fact_changes','reply','request_id'],commit),
 'start_reference_analysis':({'asset_id':{'type':'string'}},['asset_id'],start_analysis),
 'get_reference_evidence':({'analysis_id':{'type':'string'}},['analysis_id'],evidence),
 'get_reference_frame':({'observation_id':{'type':'string'}},['observation_id'],reference_frame),
 'correct_reference_observation':({'observation_id':{'type':'string'},'body':{'type':'string'},'reason':{'type':'string'}},['observation_id','body','reason'],correct),
 'claim_conversation_work':({'inquiry_id':{'type':'string'},'worker_id':{'type':'string'},'lease_seconds':{'type':'number'}},['inquiry_id','worker_id'],claim_work),
 'return_conversation_work':({'inquiry_id':{'type':'string'},'worker_id':{'type':'string'}},['inquiry_id','worker_id'],release_work),
}

def reset_sample(c):
    """Remove chat records attached to sample deals, before their deals are reset.

    Unbound inquiries are retained because they have no sample classification.
    Caller controls the transaction; media files are retained for separate retention cleanup.
    """
    ids=[r[0] for r in c.execute('SELECT i.id FROM inquiries i JOIN deals d ON d.id=i.deal_id WHERE d.sample_data=1')]
    for inquiry in ids:
        runs=[r[0] for r in c.execute('SELECT r.id FROM reference_runs r JOIN reference_assets a ON a.id=r.asset_id WHERE a.inquiry_id=?',(inquiry,))]
        for run in runs:
            c.execute('DELETE FROM reference_observations WHERE run_id=?',(run,))
            c.execute('DELETE FROM reference_runs WHERE id=?',(run,))
        for table in ('media_notifications','inquiry_turns','inquiry_facts','inquiry_jobs','inquiry_commits','reference_assets'):
            c.execute('DELETE FROM '+table+' WHERE inquiry_id=?',(inquiry,))
        c.execute('DELETE FROM inquiries WHERE id=?',(inquiry,))
