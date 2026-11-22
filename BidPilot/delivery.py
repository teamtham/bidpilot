"""Private recipient state and durable version-bound SMTP delivery.

Publication is atomic with the outbox. SMTP acceptance is not inbox delivery.
An interrupted SMTP attempt requires operator reconciliation, not blind retry.
"""
import hashlib
import ipaddress
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

import db

SCHEMA = '''
CREATE TABLE IF NOT EXISTS proposal_recipients (
 inquiry_id TEXT PRIMARY KEY, deal_id TEXT UNIQUE, email TEXT NOT NULL,
 confirmed INTEGER NOT NULL, source_seq INTEGER NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS proposal_outbox (
 version_id TEXT PRIMARY KEY, recipient TEXT NOT NULL, review_digest TEXT NOT NULL,
 option_ids_json TEXT NOT NULL, url TEXT NOT NULL, subject TEXT NOT NULL,
 body TEXT NOT NULL, message_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL,
 accepted_at REAL);
CREATE TABLE IF NOT EXISTS proposal_compositions (
 source_version_id TEXT PRIMARY KEY, version_id TEXT NOT NULL UNIQUE,
 page_hash TEXT NOT NULL);
'''


def validate_email(value):
    if (not isinstance(value, str) or len(value) > 254 or value != value.strip()
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", value)):
        raise ValueError('invalid_proposal_email')
    local, domain = value.rsplit('@', 1)
    if len(local) > 64 or local.startswith('.') or local.endswith('.') or '..' in local:
        raise ValueError('invalid_proposal_email')
    return local + '@' + domain.lower()


def recipient(conn, deal_id):
    row = conn.execute('SELECT * FROM proposal_recipients WHERE deal_id=?', (deal_id,)).fetchone()
    return dict(row) if row else None


def bind_recipient(conn, inquiry_id, deal_id):
    conn.execute('UPDATE proposal_recipients SET deal_id=? WHERE inquiry_id=?', (deal_id, inquiry_id))


def save_recipient(conn, inquiry, email, confirmed, seq):
    email = validate_email(email)
    if type(confirmed) is not bool:
        raise ValueError('invalid_email_confirmation')
    old = conn.execute('SELECT revision FROM proposal_recipients WHERE inquiry_id=?', (inquiry['id'],)).fetchone()
    conn.execute('INSERT INTO proposal_recipients VALUES (?,?,?,?,?,?) ON CONFLICT(inquiry_id) DO UPDATE SET deal_id=excluded.deal_id,email=excluded.email,confirmed=excluded.confirmed,source_seq=excluded.source_seq,revision=excluded.revision',
                 (inquiry['id'], inquiry['deal_id'], email, int(confirmed), seq, (old['revision'] if old else 0) + 1))


def configuration():
    base = os.environ.get('BIDPILOT_PUBLIC_URL', '').rstrip('/')
    try:
        parts = urlsplit(base)
        host = parts.hostname or ''
        invalid = (parts.scheme != 'https' or not host or parts.username or parts.password
                   or parts.query or parts.fragment or parts.path not in ('', '/')
                   or host.lower() == 'localhost' or '.' not in host or host.endswith('.local'))
        try:
            invalid = invalid or not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
    except ValueError:
        invalid = True
    if invalid:
        return {'ready': False, 'reason': 'Public HTTPS proposal URL is not configured'}
    required = ('BIDPILOT_SMTP_HOST', 'BIDPILOT_MAIL_FROM', 'BIDPILOT_SMTP_USER', 'BIDPILOT_SMTP_PASSWORD')
    if any(not os.environ.get(k) for k in required):
        return {'ready': False, 'public_url': base, 'reason': 'SMTP sender or credentials are missing'}
    try:
        validate_email(os.environ['BIDPILOT_MAIL_FROM'])
        port = int(os.environ.get('BIDPILOT_SMTP_PORT', '587'))
        if not 1 <= port <= 65535 or os.environ.get('BIDPILOT_SMTP_SECURITY', 'starttls') not in ('starttls', 'ssl'):
            raise ValueError()
    except ValueError:
        return {'ready': False, 'public_url': base, 'reason': 'SMTP configuration is invalid'}
    return {'ready': True, 'public_url': base}


def review(conn, version, option_ids=None):
    import core
    rows = core.options_of(conn, version)
    ids = [r['id'] for r in rows]
    chosen = ids if option_ids is None else option_ids
    if (not isinstance(chosen, list) or not chosen or not all(isinstance(i, str) for i in chosen)
            or len(set(chosen)) != len(chosen) or not set(chosen) <= set(ids)):
        raise ValueError('invalid_offered_options')
    chosen = [i for i in ids if i in chosen]
    payload = {'version': core.digest(version, rows), 'options': chosen,
               'recipient': recipient(conn, version['deal_id']),
               'capacity_revision': core.cap.ledger_revision(conn)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(), chosen


def state(conn, version):
    target = recipient(conn, version['deal_id'])
    composed = conn.execute('SELECT 1 FROM proposal_compositions WHERE version_id=?', (version['id'],)).fetchone()
    row = conn.execute('SELECT status,attempts,error,url,recipient FROM proposal_outbox WHERE version_id=?', (version['id'],)).fetchone()
    ready = bool(composed and version['page_json'] and version['outbound_status'] == 'passed')
    reason = ('Preparing your proposal' if not ready else
              "Waiting for buyer's email" if not target or not target['confirmed'] else 'Ready for review')
    try:
        digest, _ = review(conn, version)
    except ValueError:
        digest = None
    return {'composition_ready': ready, 'proposal_status': reason,
            'proposal_recipient': target, 'release_review_digest': digest,
            'email_delivery': dict(row) if row else None, 'mail_configuration': configuration()}


def queue(conn, version, token, review_digest, chosen):
    config = configuration()
    target = recipient(conn, version['deal_id'])
    base = config.get('public_url', '')
    url = base + '/p/' + token if base else '/p/' + token
    page = json.loads(version['page_json'])
    counter = conn.execute("SELECT 1 FROM proposal_versions WHERE deal_id=? AND id<>? AND status IN ('published','superseded','confirmed')", (version['deal_id'], version['id'])).fetchone()
    subject = 'Alex Tan Productions: ' + ('your revised proposal' if counter else 'your proposal')
    body = page['headline'] + '\n\n' + page['introduction'] + '\n\nReview your private proposal:\n' + url
    conn.execute('INSERT INTO proposal_outbox(version_id,recipient,review_digest,option_ids_json,url,subject,body,message_id,status,error,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (version['id'], target['email'], review_digest, db.j(chosen), url, subject, body,
                  '<proposal-' + version['id'] + '@bidpilot>', 'queued' if config['ready'] else 'failed',
                  None if config['ready'] else config['reason'], db.now()))


def deliver_once(conn, version_id, retry=False, transport=None):
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = conn.execute('SELECT * FROM proposal_outbox WHERE version_id=?', (version_id,)).fetchone()
        if not row:
            raise ValueError('outbox_not_found')
        if row['status'] in ('accepted', 'sending', 'unknown'):
            conn.commit()
            return {'status': row['status'], 'error': row['error']}
        if row['status'] == 'failed' and not retry:
            conn.commit()
            return {'status': 'failed', 'error': row['error']}
        import core
        version = conn.execute('SELECT * FROM proposal_versions WHERE id=?', (version_id,)).fetchone()
        if not version or version['status'] != 'published' or core.release_integrity_errors(conn, version):
            raise ValueError('release_not_current')
        config = configuration()
        if not config['ready']:
            conn.execute("UPDATE proposal_outbox SET status='failed',error=? WHERE version_id=?", (config['reason'], version_id))
            conn.commit()
            return {'status': 'failed', 'error': config['reason']}
        url = row['url'] if row['url'].startswith('https://') else config['public_url'] + row['url']
        body = row['body'].replace(row['url'], url)
        conn.execute("UPDATE proposal_outbox SET status='sending',attempts=attempts+1,url=?,body=?,error=NULL WHERE version_id=?", (url, body, version_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    message = EmailMessage()
    message['From'] = os.environ['BIDPILOT_MAIL_FROM']
    message['To'] = row['recipient']
    message['Subject'] = row['subject']
    message['Message-ID'] = row['message_id']
    message.set_content(body)
    status, error = 'accepted', None
    try:
        if transport:
            transport(message)
        else:
            security = os.environ.get('BIDPILOT_SMTP_SECURITY', 'starttls')
            cls = smtplib.SMTP_SSL if security == 'ssl' else smtplib.SMTP
            smtp = cls(os.environ['BIDPILOT_SMTP_HOST'], int(os.environ.get('BIDPILOT_SMTP_PORT', '587')), timeout=30)
            try:
                if security == 'starttls':
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(os.environ['BIDPILOT_SMTP_USER'], os.environ['BIDPILOT_SMTP_PASSWORD'])
                if smtp.send_message(message):
                    raise smtplib.SMTPRecipientsRefused({row['recipient']: 'refused'})
            finally:
                # A QUIT failure after DATA acceptance must not turn an
                # accepted message into a retryable failure and duplicate it.
                try:
                    smtp.quit()
                except Exception:
                    try:
                        smtp.close()
                    except Exception:
                        pass
    except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused):
        status, error = 'failed', 'SMTP rejected this delivery; retry is available'
    except Exception:
        status, error = 'unknown', 'SMTP outcome unknown; reconcile with provider before retrying'
    with conn:
        conn.execute('UPDATE proposal_outbox SET status=?,error=?,accepted_at=? WHERE version_id=?',
                     (status, error, db.now() if status == 'accepted' else None, version_id))
    return {'status': status, 'error': error}


def dispatch_get(handler, path):
    return False


def dispatch_post(handler, path):
    return False


MCP = {}
