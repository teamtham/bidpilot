"""Client-facing change chat on a released proposal link.

The studio assistant turns what the client says into the four structured
change terms the existing change request already takes (videos, captioned
versions, deadline, budget). Nothing here prices, approves or books: the
client confirms the terms and the page posts them to /p/<token>/request-change,
where the engine re-prices the change and Alex decides.
"""
import json
import os
import re
import ssl
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import core
import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposal_change_chats (version_id TEXT PRIMARY KEY,
  turns_json TEXT NOT NULL, terms_json TEXT NOT NULL, updated_at REAL NOT NULL);
"""

MAX_TEXT = 1000
MAX_TURNS = 40
TERM_KEYS = ("count", "captions", "deadline", "budget")
ORDINALS = {"second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
            "eighth": 8, "ninth": 9, "tenth": 10}
_COMMIT = re.compile(r"\b(?:we (?:agree|accept|guarantee|can do)|you(?: are|'re) booked|"
                     r"booking (?:is )?confirmed|approved|i (?:agree|accept|confirm))\b", re.I)


def dispatch_get(handler, path):
    return False


# -- terms -------------------------------------------------------------------

def base_terms(conn, version):
    """The approved public terms the chat starts from (the client's picked or
    suggested option), plus the client's own budget. Never private figures."""
    rows = core.offered_options_of(conn, version)
    chosen = next((r for r in rows if r["id"] == version["selected_option_id"]), None) or (rows[0] if rows else None)
    terms = db.uj(chosen["public_terms_json"]) if chosen else {}
    offer_row = conn.execute("SELECT offer_json FROM offers WHERE id=?", (version["offer_id"],)).fetchone()
    offer = db.uj(offer_row["offer_json"]) if offer_row else {}
    offer = offer.get("offer", offer)
    budget = offer.get("budget_sgd")
    return {"count": terms.get("deliverables"), "captions": terms.get("caption_versions"),
            "deadline": terms.get("deadline"),
            "budget": float(budget) if isinstance(budget, (int, float)) and not isinstance(budget, bool) else None}


def _as_of(conn, version):
    row = conn.execute("SELECT as_of FROM offers WHERE id=?", (version["offer_id"],)).fetchone()
    try:
        return date.fromisoformat(row["as_of"])
    except (TypeError, ValueError):
        return date.today()


def valid_terms(terms, as_of):
    """Drop anything that isn't a sane, explicit value. Returns a clean dict."""
    out = {}
    count = terms.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 30:
        out["count"] = count
    caps = terms.get("captions")
    if isinstance(caps, int) and not isinstance(caps, bool) and 0 <= caps <= 30:
        out["captions"] = caps
    deadline = terms.get("deadline")
    if isinstance(deadline, str):
        try:
            if date.fromisoformat(deadline) > as_of:
                out["deadline"] = deadline
        except ValueError:
            pass
    budget = terms.get("budget")
    if isinstance(budget, (int, float)) and not isinstance(budget, bool) and 0 < budget <= 1_000_000:
        out["budget"] = round(float(budget), 2)
    return out


def parse(text, draft, as_of):
    """Deterministic reading of one client message (also the offline fallback)."""
    found = core.parse_change_body(text, {"as_of": as_of.isoformat()})
    low = " " + " ".join(text.lower().split()) + " "
    count = draft.get("count") or 0
    m = re.search(r"\b(?:a|an|the)\s+(" + "|".join(ORDINALS) + r")\s+(?:video|one|reel|clip)", low)
    if m and found["count"] is None:
        found["count"] = ORDINALS[m[1]]
    m = re.search(r"\b(?:add|plus|another|extra|more)\s+(\d+|one|two|three|four|five)?\s*(?:more\s+|extra\s+)?(?:video|reel|clip)s?\b", low)
    if m and found["count"] is None and count:
        extra = m[1]
        n = (int(extra) if extra and extra.isdigit() else core.WORD_NUMBERS.get(extra or "one", 1))
        found["count"] = count + n
    m = re.search(r"\b(?:one|1)\s+(?:less|fewer)\s+video", low) or re.search(r"\bdrop\s+(?:a|one)\s+video", low)
    if m and found["count"] is None and count > 1:
        found["count"] = count - 1
    target = found["count"] or count
    if found["captions"] is None:
        if re.search(r"\bno captions?\b|\bwithout captions?\b|\bdon'?t need captions?\b", low):
            found["captions"] = 0
        elif re.search(r"\bcaptions?\b[^.?!]*\b(?:all|every|each)\b|\b(?:all|every|each)\b[^.?!]*\bcaption", low) or \
                re.search(r"\byes\b[^.?!]*\b(?:all|every|each)\b", low):
            found["captions"] = target or None
        else:
            m = re.search(r"\bcaptions?\s+on\s+(\d+|one|two|three|four|five|six|seven|eight)\b", low) or \
                re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight)\s+(?:with\s+)?captions?\b", low) or \
                re.search(r"\bjust the first\s+(\d+|one|two|three|four|five)\b", low)
            if m:
                found["captions"] = int(m[1]) if m[1].isdigit() else core.WORD_NUMBERS[m[1]]
    if found["budget"] is None:
        m = re.search(r"\b(?:budget|up to|about|around|max(?:imum)?|stretch to|go to)\b[^0-9]{0,20}([0-9][0-9,]{2,8}(?:\.[0-9]{1,2})?)\b", low)
        if m:
            found["budget"] = round(float(m[1].replace(",", "")), 2)
        elif re.search(r"\b(?:same|keep(?: it| the)?)\s+(?:price|budget)\b|\bstill\b[^.?!]*\b(?:budget|price)\b", low) and draft.get("budget"):
            found["budget"] = draft["budget"]
    return valid_terms({k: v for k, v in found.items() if v is not None}, as_of)


# -- replies -------------------------------------------------------------------

def _money(v):
    return "S$" + (format(v, ",.0f") if float(v).is_integer() else format(v, ",.2f"))


def _day(iso):
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}"


def describe(t):
    parts = []
    if t.get("count"):
        parts.append(f"{t['count']} video{'s' if t['count'] != 1 else ''}")
    if t.get("deadline"):
        parts.append("by " + _day(t["deadline"]))
    text = " ".join(parts)
    if t.get("captions") is not None:
        text += f", {t['captions']} with captions" if t["captions"] else ", no captions"
    return text


def template_reply(base, draft, asked_budget):
    """Plain, safe reply: restate the draft and ask about the next gap."""
    changed = [k for k in TERM_KEYS if draft.get(k) != base.get(k)]
    if not changed:
        return ("Tell me what you'd like to change: the number of videos, captions, the deadline "
                "or your budget.", False)
    summary = "Got it: " + describe(draft) + "."
    if "count" in changed and "captions" not in changed and draft.get("count", 0) > (base.get("captions") or 0):
        return summary + " Should the new videos have captions too?", False
    if "budget" not in changed and not asked_budget:
        return summary + " Anything to share about budget? It stays private, and Alex sets the price.", False
    return (summary + " Your change is on the card. Send it to Alex when it looks right. "
            "Nothing changes until Alex replies with an updated proposal.", True)


SYSTEM = """You are the studio assistant on a client's private video proposal page for Alex Tan
Productions. The client wants to change their proposal. Read the conversation and work out the
four change terms: count (number of videos), captions (number of captioned versions), deadline
(ISO date YYYY-MM-DD, after as_of) and budget (the client's budget in SGD). Start from
draft_terms and only change a term the client clearly stated in their latest message
("a fourth video" means count 4; "one more" means current count plus one; "captions on all"
means captions equals count). Reply in one to three short, friendly sentences: restate the
change and ask about the most important missing detail (captions for new videos, then budget).
Never quote a price, never say a change is accepted, approved, possible or booked, never
promise dates. Alex reviews and prices every change. When nothing is left to ask, tell the
client to check the card and press Send to Alex. Return JSON: {"reply": string,
"terms": {"count": int|null, "captions": int|null, "deadline": string|null, "budget": number|null}}
where null means unchanged."""


def _gemini(payload):
    key = os.environ.get("GEMINI_API_KEY")
    if not key or os.environ.get("BIDPILOT_STUDIO_ASSISTANT", "gemini") != "gemini":
        raise ValueError("assistant_offline")
    model = os.environ.get("BIDPILOT_GEMINI_MODEL", "gemini-3.1-flash-lite")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", model):
        raise ValueError("invalid_model")
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
            "generationConfig": {"responseMimeType": "application/json"}}
    req = Request("https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                  data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
                                                           "x-goog-api-key": key})
    context = ssl.create_default_context()
    if os.sys.platform == "darwin" and Path("/etc/ssl/cert.pem").is_file():
        context.load_verify_locations(cafile="/etc/ssl/cert.pem")
    with urlopen(req, timeout=20, context=context) as response:
        candidate = json.load(response)["candidates"][0]
    answer = json.loads("".join(p["text"] for p in candidate["content"]["parts"]
                                if "text" in p and not p.get("thought")))
    if not isinstance(answer, dict):
        raise ValueError("bad_answer")
    return answer


def _safe_reply(reply, draft):
    if not isinstance(reply, str) or not reply.strip() or len(reply) > 600 or _COMMIT.search(reply):
        return None
    for amount in re.findall(r"(?:S\$|\$|SGD\s*)\s*([0-9][0-9,]*(?:\.\d+)?)", reply, re.I):
        if draft.get("budget") is None or float(amount.replace(",", "")) != draft["budget"]:
            return None
    return reply.strip()


# -- state -------------------------------------------------------------------

def _live_version(conn, token):
    version = core.published_by_token(conn, token)
    if not version or version["status"] != "published" or version["accepted_at"]:
        raise ValueError("not_found")
    if core.release_integrity_errors(conn, version):
        raise ValueError("not_found")
    return version


def _sentence(text):
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def state(conn, version):
    base = base_terms(conn, version)
    row = conn.execute("SELECT * FROM proposal_change_chats WHERE version_id=?", (version["id"],)).fetchone()
    turns = db.uj(row["turns_json"]) if row else []
    draft = dict(base, **(db.uj(row["terms_json"]) if row else {}))
    changed = [k for k in TERM_KEYS if draft.get(k) != base.get(k)]
    ready = bool(changed) and bool(turns) and turns[-1].get("ready", False)
    return {"base": base, "terms": draft, "changed": changed, "turns": turns, "ready": ready,
            "client_words": " ".join(_sentence(t["text"]) for t in turns if t["sender"] == "client")}


def greeting(base):
    if not base.get("count"):
        return "Hi! What would you like to change in this proposal?"
    return ("Hi! This proposal is " + describe(base) + ". What would you like to change? "
            "I'll put it on a card for you to check before it goes to Alex.")


def message(conn, token, text):
    """Add one client message, update the draft terms and reply."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError("invalid_message")
    text = " ".join(text.split())
    version = _live_version(conn, token)
    current = state(conn, version)
    if len(current["turns"]) >= MAX_TURNS:
        raise ValueError("chat_full")
    as_of = _as_of(conn, version)
    draft = dict(current["terms"])
    turns = current["turns"] + [{"sender": "client", "text": text}]
    asked_budget = any("budget" in t["text"].lower() for t in current["turns"] if t["sender"] == "assistant")
    reply, ready = None, False
    try:
        answer = _gemini({"as_of": as_of.isoformat(), "approved_terms": current["base"],
                          "draft_terms": draft, "turns": turns[-20:]})
        proposed = answer.get("terms") if isinstance(answer.get("terms"), dict) else {}
        cleaned = valid_terms({k: v for k, v in proposed.items() if v is not None}, as_of)
        # Deterministic reading wins where it found an explicit value.
        cleaned.update(parse(text, draft, as_of))
        draft.update(cleaned)
        reply = _safe_reply(answer.get("reply"), draft)
        if reply:
            changed = any(draft.get(k) != current["base"].get(k) for k in TERM_KEYS)
            ready = changed and ("send to alex" in reply.lower())
    except (ValueError, KeyError, TypeError, IndexError, HTTPError, URLError, OSError, json.JSONDecodeError):
        draft.update(parse(text, draft, as_of))
    if not reply:
        reply, ready = template_reply(current["base"], draft, asked_budget)
    turns.append({"sender": "assistant", "text": reply, "ready": ready})
    stored = {k: draft[k] for k in TERM_KEYS if draft.get(k) != current["base"].get(k)}
    with conn:
        conn.execute("INSERT INTO proposal_change_chats(version_id, turns_json, terms_json, updated_at)"
                     " VALUES (?,?,?,?) ON CONFLICT(version_id) DO UPDATE SET turns_json=excluded.turns_json,"
                     " terms_json=excluded.terms_json, updated_at=excluded.updated_at",
                     (version["id"], db.j(turns), db.j(stored), db.now()))
    return state(conn, version)


def reset(conn, token):
    version = _live_version(conn, token)
    with conn:
        conn.execute("DELETE FROM proposal_change_chats WHERE version_id=?", (version["id"],))
    return state(conn, version)
