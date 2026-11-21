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
    content = row["content"]
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
    handler.send_header("Content-Type", row["mime"])
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", "private, no-store")
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


def library_page(conn):
    from server import CLIENT_STYLE
    import estimating
    projects = [(r["project_id"], r["client"]) for r in estimating.get_history(conn)]
    options = "".join(f'<option value="{html.escape(key)}">{html.escape(label)} · {html.escape(key)}</option>' for key,label in projects)
    cards = []
    for row in conn.execute("SELECT id,title,description,mime,shared FROM portfolio_assets ORDER BY title"):
        media = ""
        if row["shared"]:
            tag = "video" if row["mime"].startswith("video/") else "img"
            media = f'<{tag} controls preload="metadata" style="width:100%" src="/portfolio/media/{html.escape(row["id"])}"></{tag}>'
        cards.append(f'<article class="card">{media}<h2>{html.escape(row["title"])}</h2><p>{html.escape(row["description"])}</p>'
                     f'<button data-id="{html.escape(row["id"])}" data-share="{int(not row["shared"])}">'
                     f'{"Remove buyer access" if row["shared"] else "Allow buyer access"}</button></article>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>Studio portfolio</title><style>'+CLIENT_STYLE+'</style><main><h1>Studio portfolio</h1>'
            '<p>Choose the work buyers can discover in their proposal.</p><div class="cards">'+''.join(cards)+'</div>'
            '<form id="upload" class="card"><h2>Add work</h2><label>Project<select name="project_id">'+options+'</select></label>'
            '<label>Title<input name="title" required></label><label>Buyer-facing description<textarea name="description" required></textarea></label>'
            '<label>Style and subject keywords<input name="tags"></label><label>Video or image<input type="file" name="asset" required accept="video/mp4,image/jpeg,image/png"></label>'
            '<label><input type="checkbox" name="shared"> Allow buyers to discover this asset</label><button>Save work</button></form>'
            '<p id="message" role="status"></p></main>'+LIBRARY_SCRIPT+'</html>')


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
