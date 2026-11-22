"""Studio-owned portfolio. Private fees live on projects, never on public media."""
import base64
import hashlib
import html
import json
import re
import sqlite3
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_assets (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
 tags TEXT NOT NULL, mime TEXT NOT NULL, content BLOB NOT NULL, shared INTEGER NOT NULL,
 digest TEXT NOT NULL, UNIQUE(project_id,digest));
CREATE INDEX IF NOT EXISTS portfolio_share ON portfolio_assets(shared,project_id);
CREATE TABLE IF NOT EXISTS portfolio_search_requests (
 request_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, query TEXT NOT NULL,
 results_json TEXT, created_at REAL NOT NULL);
"""
MAX = 24 * 1024 * 1024
MEDIA = Path(__file__).with_name("sample-portfolio")
SAMPLES = (
    ("cafe", "P-041", "Opening morning", "A warm café launch title film with espresso and opening-day typography.",
     "café cafe launch promo morning warm coffee espresso title animation"),
    ("bakery", "P-036", "From the oven", "An editorial bakery promo with product details and a slower reveal.",
     "food bakery bread artisan warm product reveal promo"),
    ("motion", "P-038", "Signal in motion", "A geometric motion study for a technology explainer.",
     "corporate finance tech explainer geometric motion graphics"),
)


def seed(conn):
    for key, project, title, desc, tags in SAMPLES:
        if conn.execute("SELECT 1 FROM portfolio_assets WHERE id=?", (key,)).fetchone():
            continue
        clip = (MEDIA / (key + ".mp4")).read_bytes()
        conn.execute("INSERT INTO portfolio_assets VALUES (?,?,?,?,?,?,?,?,?)",
                     (key, project, title, "Illustrative studio sample. " + desc, tags, "video/mp4",
                      clip, 1, hashlib.sha256(clip).hexdigest()))


def _asset(row):
    return {"id": row["id"], "title": row["title"], "description": row["description"],
            "tags": row["tags"], "media_url": "/portfolio/media/" + row["id"], "mime": row["mime"]}


def public_asset(conn, asset_id):
    row = conn.execute("SELECT id,title,description,tags,mime FROM portfolio_assets WHERE id=? AND shared=1",
                       (asset_id,)).fetchone()
    return _asset(row) if row else None


def search(conn, query):
    words = set(re.findall(r"\w+", query.casefold())) - {"of","a","the","in","for","and","an","we","our","show","me","something","i","want"}
    ranked = []
    for row in conn.execute("SELECT id,title,description,tags,mime FROM portfolio_assets WHERE shared=1 ORDER BY title"):
        item = _asset(row)
        text = (item["title"] + " " + item["description"] + " " + item["tags"]).casefold()
        hits = sorted(words.intersection(re.findall(r"\w+", text)))
        if hits or not words:
            ranked.append((len(hits), dict(item, matches=hits)))
    ranked.sort(key=lambda pair: -pair[0])
    return [item for _, item in ranked]


def _live(conn, token):
    import core
    version = core.published_by_token(conn, token)
    if not version or core.release_integrity_errors(conn, version):
        raise ValueError('not_found')
    return version


def client_search(conn, token, data):
    version = _live(conn, token)
    request_id, query = data.get('request_id'), data.get('query')
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 100 or not isinstance(query, str) or not 0 < len(query.strip()) <= 2000:
        raise ValueError('invalid_request')
    with conn:
        old = conn.execute('SELECT * FROM portfolio_search_requests WHERE request_id=?', (request_id,)).fetchone()
        if old and (old['version_id'] != version['id'] or old['query'] != query):
            raise ValueError('request_id_conflict')
        if not old:
            conn.execute('INSERT INTO portfolio_search_requests VALUES (?,?,?,?,?)', (request_id, version['id'], query, None, db.now()))
    return search_status(conn, token, request_id)


def search_status(conn, token, request_id):
    version = _live(conn, token)
    row = conn.execute('SELECT * FROM portfolio_search_requests WHERE request_id=? AND version_id=?', (request_id, version['id'])).fetchone()
    if not row:
        raise ValueError('not_found')
    if row['results_json'] is None:
        return {'status': 'pending', 'assets': search(conn, row['query'])}
    assets = []
    for result in json.loads(row['results_json']):
        asset = public_asset(conn, result['asset_id'])
        if asset:
            assets.append(dict(asset, why=result['why']))
    return {'status': 'complete', 'assets': assets}


def pending(conn):
    return [dict(r) for r in conn.execute("SELECT s.request_id,s.version_id,s.query FROM portfolio_search_requests s JOIN proposal_versions v ON v.id=s.version_id JOIN deals d ON d.id=v.deal_id WHERE s.results_json IS NULL AND v.status IN ('published','confirmed') AND d.sample_data=1 ORDER BY s.created_at")]


def commit_search(conn, args):
    import core
    row = conn.execute("SELECT s.*,v.deal_id,v.status,d.sample_data,d.client FROM portfolio_search_requests s JOIN proposal_versions v ON v.id=s.version_id JOIN deals d ON d.id=v.deal_id WHERE s.request_id=? AND s.version_id=?", (args['request_id'], args['version_id'])).fetchone()
    version = conn.execute('SELECT * FROM proposal_versions WHERE id=?', (args['version_id'],)).fetchone()
    if not row or not row['sample_data'] or row['status'] not in ('published','confirmed') or core.release_integrity_errors(conn, version):
        raise ValueError('stale_version')
    results = args['results']
    if not isinstance(results, list) or len(results) > 50:
        raise ValueError('invalid_results')
    seen = set()
    for item in results:
        if not isinstance(item, dict) or set(item) != {'asset_id','why'} or item['asset_id'] in seen or not public_asset(conn, item['asset_id']):
            raise ValueError('invalid_asset')
        why = item['why']
        if not isinstance(why, str) or not 0 < len(why.strip()) <= 400 or re.search(r'\b(price|cost|budget|discount|quote|deliver|deadline|book|guarantee|commit|approve|free)\w*\b', why, re.I):
            raise ValueError('invalid_explanation')
        # Numeric terms and private labels/names must never escape via curation.
        check = core.engine.outbound_check(why, [], {}, core.other_client_names(conn, row['client']))
        if not check['passed']:
            raise ValueError('unsafe_explanation')
        seen.add(item['asset_id'])
    payload = db.j(results)
    if row['results_json'] is not None and row['results_json'] != payload:
        raise ValueError('request_already_completed')
    conn.execute('UPDATE portfolio_search_requests SET results_json=? WHERE request_id=?', (payload, args['request_id']))
    return {'ok': True}


def _browse(conn, args):
    assets = search(conn, args.get("query", ""))
    # Project linkage is for the authenticated scoping worker only; buyer
    # projections use search/public_asset directly and never include it.
    for asset in assets:
        row = conn.execute('SELECT project_id FROM portfolio_assets WHERE id=?', (asset['id'],)).fetchone()
        asset['project_id'] = row['project_id']
    return {"assets": assets}


def get_frame(conn, args):
    seconds = args['seconds']
    if isinstance(seconds, bool) or not isinstance(seconds, (int,float)) or not math.isfinite(seconds) or not 0 <= seconds <= 180:
        raise ValueError('invalid_seconds')
    row = conn.execute('SELECT mime,content FROM portfolio_assets WHERE id=? AND shared=1', (args['asset_id'],)).fetchone()
    if not row:
        raise ValueError('not_found')
    if row['mime'] in ('image/jpeg','image/png'):
        if seconds != 0:
            raise ValueError('invalid_seconds')
        return {'mime':row['mime'],'data_base64':base64.b64encode(row['content']).decode('ascii')}
    if row['mime'] != 'video/mp4':
        raise ValueError('unsupported_media')
    with tempfile.TemporaryDirectory() as directory:
        source, target = Path(directory)/'source.mp4', Path(directory)/'frame.jpg'
        source.write_bytes(row['content'])
        from conversation import ffmpeg_executable
        ffmpeg = ffmpeg_executable()
        if ffmpeg:
            cmd = [ffmpeg,'-v','error','-ss',str(seconds),'-i',str(source),'-frames:v','1','-vf','scale=640:640:force_original_aspect_ratio=decrease',str(target)]
        elif shutil.which('swift'):
            cmd = ['swift',str(Path(__file__).with_name('media_probe.swift')),'--frame',str(source),str(seconds),str(target)]
        else:
            raise ValueError('frame_decoder_unavailable')
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
        except (subprocess.SubprocessError, OSError):
            raise ValueError('frame_unavailable')
        if not target.is_file() or not 0 < target.stat().st_size <= 5*1024*1024:
            raise ValueError('frame_unavailable')
        return {'mime':'image/jpeg','data_base64':base64.b64encode(target.read_bytes()).decode('ascii')}


MCP = {"search_portfolio": ({"query": {"type": "string"}}, ["query"], _browse),
       "get_portfolio_frame": ({"asset_id":{"type":"string"}, "seconds":{"type":"number"}}, ["asset_id","seconds"], get_frame),
       "commit_portfolio_search": ({"request_id":{"type":"string"}, "version_id":{"type":"string"}, "results":{"type":"array"}}, ["request_id","version_id","results"], commit_search)}


def _serve(handler, asset_id, private=False):
    row = handler.conn.execute("SELECT mime,content,shared FROM portfolio_assets WHERE id=?", (asset_id,)).fetchone()
    if not row or not (row["shared"] or private):
        return handler.not_found()
    send_bytes(handler, row["content"], row["mime"])


def send_bytes(handler, content, mime, cache="private, no-store"):
    """Send media bytes with single-range support (Safari needs ranges for video)."""
    size = len(content)
    start, end = 0, size-1
    requested = handler.headers.get("Range")
    if requested:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
        if match and any(match.groups()):
            low, high = match.groups()
            if low:
                start = int(low)
                end = min(int(high), end) if high else end
            else:
                start = max(0, size - int(high))
        else:
            start = size
        if not 0 <= start <= end < size:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.end_headers()
            return
    handler.send_response(206 if requested else 200)
    handler.send_header("Content-Type", mime)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", cache)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(end-start+1))
    if requested:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    handler.wfile.write(content[start:end+1])


def dispatch_get(h, path):
    if path.startswith("/portfolio/media/"):
        _serve(h, path.rsplit("/", 1)[-1])
    elif path == "/portfolio":
        if not h.owner_ok():
            h.forbidden("Owner access required")
        else:
            h.send_html(library_page(h.conn))
    else:
        return False
    return True


def dispatch_post(h, path):
    if path not in ("/api/portfolio/upload", "/api/portfolio/share"):
        return False
    if not h.owner_ok():
        h.forbidden("Owner access required")
        return True
    try:
        if not 0 < int(h.headers.get("Content-Length", 0)) < MAX * 4//3 + 10000:
            raise ValueError("Choose a file up to 24 MB.")
        data = h.body()
        if type(data.get("shared")) is not bool:
            raise ValueError("Choose the sharing permission.")
        if path.endswith("/share"):
            with h.conn:
                updated = h.conn.execute("UPDATE portfolio_assets SET shared=? WHERE id=?",
                                         (data["shared"], data["id"])).rowcount
                if not updated:
                    raise ValueError("Unknown asset.")
        else:
            fields = ("title", "description", "tags", "project_id")
            if any(not isinstance(data.get(k), str) or len(data[k])>1000 for k in fields) or not data["title"].strip():
                raise ValueError("Provide a title and project.")
            import estimating
            if data["project_id"] not in {r["project_id"] for r in estimating.get_history(h.conn)}:
                raise ValueError("Choose an existing project.")
            body = base64.b64decode(data["content"], validate=True)
            if not 0 < len(body) <= MAX:
                raise ValueError("Choose a file up to 24 MB.")
            if body[4:8] == b"ftyp":
                mime = "video/mp4"
            elif body.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif body.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            else:
                raise ValueError("Use an MP4 video, JPEG or PNG.")
            with h.conn:
                h.conn.execute("INSERT INTO portfolio_assets VALUES (?,?,?,?,?,?,?,?,?)",
                               (db.new_id(),data["project_id"],data["title"],data["description"],data["tags"],
                                mime,body,data["shared"],hashlib.sha256(body).hexdigest()))
        h.send_json({"ok": True})
    except (ValueError, TypeError, KeyError, sqlite3.IntegrityError):
        h.send_json({"ok":False, "error":"Check the project, file format, size and whether the file already exists."},400)
    return True


FORMAT_NAMES = {"product_promo": "Product promo", "social_reel": "Social reel", "testimonial": "Testimonial",
                "event_recap": "Event recap", "explainer": "Explainer"}

LIBRARY_STYLE = """
:root{--b-bg:#f3f5f7;--b-panel:#fff;--b-ink:#1d2430;--b-mute:#64707e;--b-line:#dde2e8;--b-teal:#146c62;--b-teal-tint:#e2f1ee;
 --ok:#2c7a3f;--ok-tint:#e4f3e7;--ui:"Instrument Sans","Helvetica Neue",Arial,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--b-bg);color:var(--b-ink);font:15px/1.5 var(--ui);-webkit-font-smoothing:antialiased}
a{color:var(--b-teal)}
:focus-visible{outline:2px solid var(--b-teal);outline-offset:2px}
.topbar{display:flex;align-items:center;gap:18px;padding:12px 24px;background:#fff;border-bottom:1px solid var(--b-line);flex-wrap:wrap}
.logo{font:700 15px var(--ui);display:flex;align-items:center;gap:8px;color:var(--b-ink);text-decoration:none}
.logo i{width:20px;height:20px;background:url(data:image/svg+xml,%3Csvg%20xmlns=%27http://www.w3.org/2000/svg%27%20viewBox=%270%200%2024%2024%27%3E%3Cpath%20fill=%27%231e9e5a%27%20d=%27M22%202%202%209.5l8.2%203.3L13.5%2021z%27/%3E%3Cpath%20fill=%27none%27%20stroke=%27%23fff%27%20stroke-width=%271.4%27%20stroke-linecap=%27round%27%20d=%27M22%202%2010.2%2012.8%27/%3E%3C/svg%3E) center/contain no-repeat;display:inline-block}
.topnav{display:flex;gap:16px;font-size:13px;flex-wrap:wrap}
.topnav a{color:var(--b-mute);text-decoration:none;padding:2px 0;border-bottom:2px solid transparent}
.topnav a:hover{color:var(--b-ink)}
.topnav a.here{color:var(--b-ink);font-weight:600;border-bottom-color:var(--b-teal)}
.back{margin-left:auto;font:600 13px var(--ui);text-decoration:none}
.wrap{max-width:1180px;margin:0 auto;padding:28px 24px 64px;display:grid;gap:20px}
.head h1{margin:0;font:600 24px/1.2 var(--ui)}
.head p{margin:4px 0 0;color:var(--b-mute);font-size:14px;max-width:62ch}
.filters{display:flex;flex-wrap:wrap;gap:8px}
.fchip{font:600 12.5px var(--ui);padding:6px 12px;border-radius:999px;border:1px solid var(--b-line);background:#fff;color:var(--b-ink);cursor:pointer}
.fchip:hover{border-color:var(--b-teal)}
.fchip[aria-pressed="true"]{background:var(--b-teal);border-color:var(--b-teal);color:#fff}
.fchip span{opacity:.7;font-weight:500;margin-left:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px;align-items:stretch}
.item{background:var(--b-panel);border:1px solid var(--b-line);border-radius:10px;overflow:hidden;display:grid;grid-template-rows:auto 1fr;transition:border-color .15s,box-shadow .15s}
.item:hover{border-color:#c5ccd3;box-shadow:0 10px 24px -18px rgba(20,30,40,.45)}
.thumb{aspect-ratio:16/9;background:#e9edf1;position:relative;overflow:hidden}
.thumb video,.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;background:#000}
.thumb .private{position:absolute;inset:0;display:grid;place-items:center;text-align:center;padding:16px;color:var(--b-mute);font-size:12.5px}
.body{padding:14px;display:grid;gap:8px;align-content:start;grid-template-rows:auto auto 1fr auto}
.body h2{margin:0;font:600 16px/1.3 var(--ui)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.st{font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:999px;background:#eef1f4;color:#4b5663;white-space:nowrap}
.st.ok{background:var(--ok-tint);color:var(--ok)}
.st.new{background:var(--b-teal-tint);color:var(--b-teal)}
.desc{margin:0;color:var(--b-mute);font-size:13px;line-height:1.5;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.foot{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:10px;padding-top:10px;border-top:1px solid var(--b-line);font-size:12px;color:var(--b-mute)}
.foot span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.thumb video{cursor:pointer}
.thumb .play{position:absolute;right:12px;bottom:12px;width:34px;height:34px;border-radius:50%;background:rgba(255,255,255,.92);display:grid;place-items:center;pointer-events:none;box-shadow:0 2px 8px rgba(0,0,0,.25)}
.thumb .play::before{content:"";margin-left:3px;border-left:11px solid var(--b-ink);border-top:7px solid transparent;border-bottom:7px solid transparent}
.thumb.playing .play{display:none}
.bbtn{display:inline-flex;align-items:center;justify-content:center;border-radius:7px;padding:8px 12px;font:600 13px var(--ui);border:1px solid var(--b-line);background:#fff;color:var(--b-ink);white-space:nowrap;cursor:pointer}
.bbtn:hover{background:#f6f2ee}
.bbtn.p{background:var(--b-teal);border-color:var(--b-teal);color:#fff;padding:10px 16px;font-size:13.5px}
.empty{background:#fff;border:1px dashed var(--b-line);border-radius:10px;padding:40px 20px;text-align:center;color:var(--b-mute)}
.panel{background:#fff;border:1px solid var(--b-line);border-radius:10px;padding:18px;display:grid;gap:14px}
.panel h2{margin:0;font:600 13px var(--ui);color:var(--b-mute)}
.form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 16px}
.form label{display:grid;gap:5px;font-size:12.5px;font-weight:600;color:var(--b-mute)}
.form .wide{grid-column:1/-1}
.form input,.form select,.form textarea{font:14px var(--ui);color:var(--b-ink);border:1px solid var(--b-line);border-radius:7px;padding:9px 11px;background:#fff;width:100%}
.form textarea{min-height:84px;resize:vertical}
.form .check{display:flex;align-items:center;gap:8px;font-weight:500;color:var(--b-ink)}
.form .check input{width:auto;accent-color:var(--b-teal)}
.actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
#message{margin:0;font-size:13px;color:#b3261e}
#message:empty{display:none}
@media(max-width:640px){.topbar{padding:12px 16px;gap:10px}.back{margin-left:0}.wrap{padding:20px 16px 48px}.form{grid-template-columns:1fr}.grid{grid-template-columns:1fr}}
"""

FILTER_SCRIPT = """<script>
document.querySelectorAll('[data-filter]').forEach(chip=>chip.onclick=()=>{
  const f=chip.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(c=>c.setAttribute('aria-pressed',String(c===chip)));
  let shown=0;
  document.querySelectorAll('.item').forEach(card=>{const on=f==='all'||card.dataset.format===f||card.dataset.access===f;card.hidden=!on;if(on)shown++;});
  const none=document.querySelector('#nomatch'); if(none) none.hidden=shown>0;
});
document.querySelectorAll('.thumb video').forEach(v=>v.onclick=()=>{v.controls=true;v.closest('.thumb').classList.add('playing');v.play().catch(()=>{});});
document.querySelectorAll('[data-owner-link]').forEach(link=>{const t=new URLSearchParams(location.search).get('owner');if(t)link.search='?owner='+encodeURIComponent(t);});
</script>"""


def library_page(conn):
    import estimating
    history = {r["project_id"]: r for r in estimating.get_history(conn)}
    projects = [(r["project_id"], r["client"]) for r in history.values()]
    options = "".join(f'<option value="{html.escape(key)}">{html.escape(label)} · {html.escape(key)}</option>' for key,label in projects)
    esc = html.escape
    cards, formats = [], {}
    rows = conn.execute("SELECT id,project_id,title,description,mime,shared FROM portfolio_assets ORDER BY title").fetchall()
    for row in rows:
        project = history.get(row["project_id"])
        fmt = (project or {}).get("format") or "other"
        formats[fmt] = formats.get(fmt, 0) + 1
        if row["shared"]:
            src = "/portfolio/media/" + esc(row["id"])
            media = (f'<video muted playsinline preload="metadata" title="Play preview" src="{src}#t=0.5"></video><span class="play" aria-hidden="true"></span>'
                     if row["mime"].startswith("video/") else f'<img alt="" loading="lazy" src="{src}">')
        else:
            media = '<span class="private">Private: not shown to buyers, so no preview here</span>'
        chips = [f'<span class="st new">{esc(FORMAT_NAMES.get(fmt, fmt.replace("_", " ").title()))}</span>']
        if project:
            n = project["deliverables"]
            chips.append(f'<span class="st">{n} video{"s" if n != 1 else ""}</span>')
            days = project["shoot_days"]
            chips.append(f'<span class="st">{days} shoot day{"s" if days != 1 else ""}</span>' if days
                         else '<span class="st">Edit only</span>')
        chips.append('<span class="st ok">Buyers can see</span>' if row["shared"] else '<span class="st">Private</span>')
        client = f'{esc(project["client"])} · ' if project else ''
        cards.append(
            f'<article class="item" data-format="{esc(fmt)}" data-access="{"shared" if row["shared"] else "private"}">'
            f'<div class="thumb">{media}</div><div class="body"><h2>{esc(row["title"])}</h2>'
            f'<div class="chips">{"".join(chips)}</div><p class="desc">{esc(row["description"])}</p>'
            f'<div class="foot"><span title="{client}{esc(row["project_id"])}">{client}{esc(row["project_id"])}</span>'
            f'<button class="bbtn" data-id="{esc(row["id"])}" data-share="{int(not row["shared"])}">'
            f'{"Remove buyer access" if row["shared"] else "Allow buyer access"}</button></div></div></article>')
    shared = sum(1 for r in rows if r["shared"])
    chip_html = (f'<button type="button" class="fchip" data-filter="all" aria-pressed="true">All<span>{len(rows)}</span></button>'
                 + "".join(f'<button type="button" class="fchip" data-filter="{esc(k)}" aria-pressed="false">'
                           f'{esc(FORMAT_NAMES.get(k, k.replace("_", " ").title()))}<span>{v}</span></button>'
                           for k, v in sorted(formats.items()))
                 + f'<button type="button" class="fchip" data-filter="shared" aria-pressed="false">Buyers can see<span>{shared}</span></button>')
    grid = (f'<nav class="filters" aria-label="Filter portfolio">{chip_html}</nav><div class="grid">{"".join(cards)}</div>'
            '<p class="empty" id="nomatch" hidden>Nothing in this category yet.</p>') if rows else (
            '<div class="empty"><b>No work in the library yet.</b><br>Add a finished piece below and choose whether buyers can see it.</div>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Studio portfolio</title><link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&display=swap">'
            '<style>'+LIBRARY_STYLE+'</style></head><body>'
            '<header class="topbar"><a class="logo" href="/dashboard" data-owner-link><i aria-hidden="true"></i>BidPilot</a>'
            '<nav class="topnav" aria-label="Studio tools"><a class="here" href="/portfolio" data-owner-link>Portfolio library</a>'
            '<a href="/estimating" data-owner-link>Production estimates</a><a href="/commerce" data-owner-link>Proposals and amendments</a>'
            '<a href="/inquire" target="_blank" rel="noopener">Buyer inquiry</a></nav>'
            '<a class="back" href="/dashboard" data-owner-link>← Back to dashboard</a></header>'
            '<main class="wrap"><div class="head"><h1>Studio portfolio</h1>'
            '<p>Choose the work buyers can discover in their proposal. Private pieces stay here and are never shown to buyers.</p></div>'
            + grid +
            '<form id="upload" class="panel"><h2>Add work</h2><div class="form">'
            '<label>Project<select name="project_id">'+options+'</select></label>'
            '<label>Title<input name="title" required></label>'
            '<label class="wide">Buyer-facing description<textarea name="description" required></textarea></label>'
            '<label>Style and subject keywords<input name="tags" placeholder="e.g. cafe, warm, launch"></label>'
            '<label>Video or image<input type="file" name="asset" required accept="video/mp4,image/jpeg,image/png"></label>'
            '<label class="check wide"><input type="checkbox" name="shared"> Allow buyers to discover this asset</label>'
            '<div class="actions wide"><button class="bbtn p">Save work</button><p id="message" role="status"></p></div></div></form>'
            '</main>'+LIBRARY_SCRIPT+FILTER_SCRIPT+'</body></html>')


LIBRARY_SCRIPT = """<script>
const token=new URLSearchParams(location.search).get('owner');
async function post(action, data) {
  const response=await fetch('/api/portfolio/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Owner-Token':token},body:JSON.stringify(data)});
  const result=await response.json();
  if(response.ok) location.reload(); else document.querySelector('#message').textContent=result.error;
}
document.querySelectorAll('[data-share]').forEach(button=>button.onclick=()=>post('share',{id:button.dataset.id,shared:button.dataset.share==='1'}));
document.querySelector('#upload').onsubmit=event=>{
  event.preventDefault();
  const form=new FormData(event.currentTarget), file=form.get('asset');
  if(file.size>24*1024*1024){document.querySelector('#message').textContent='Choose a file up to 24 MB.';return;}
  const reader=new FileReader();
  reader.onload=()=>post('upload',{project_id:form.get('project_id'),title:form.get('title'),description:form.get('description'),tags:form.get('tags'),shared:form.has('shared'),content:reader.result.split(',')[1]});
  reader.readAsDataURL(file);
};
</script>"""
