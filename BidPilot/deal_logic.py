"""Founder-facing deal logic: one clear phase per deal and plain-English text.

Pure helpers used by state() and the owner routes. Nothing here writes to the
database or loosens any check; it only explains what the core already decided.
"""
import re
from datetime import date

OPTION_LABELS = {"as_offered": "As offered", "hold_date": "Keep the date",
                 "hold_budget": "Keep the budget", "hold_scope": "Keep all {count}",
                 "phased": "Phased"}
_COUNT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def option_label(kind, terms=None):
    n = (terms or {}).get("deliverables")
    count = _COUNT_WORDS[n] if isinstance(n, int) and 0 <= n < len(_COUNT_WORDS) else (n or "videos")
    return OPTION_LABELS.get(kind, kind.replace("_", " ").title()).format(count=count)

# -- refusals ---------------------------------------------------------------

_MESSAGES = {
    "version_not_found": "That proposal version no longer exists. Refresh the page.",
    "deal_not_found": "That deal no longer exists. Refresh the page.",
    "not_latest_version": "A newer version of this proposal exists. Review the newest one instead.",
    "stale_version": "A newer version of this proposal exists. Review the newest one instead.",
    "outbound_not_passed": "The message hasn't passed the check. Run the check and fix anything it flags first.",
    "digest_mismatch": "The draft or its options changed after the check. Run the check again.",
    "capacity_changed": "Your calendar changed since this estimate, so it may no longer fit. Re-run the estimate first.",
    "review_required": "This needs a fresh estimate and your review before it can go out.",
    "resource_unavailable": "The people or kit this job needs aren't free on those dates any more. Re-run the estimate.",
    "option_not_in_version": "That option isn't part of this proposal.",
    "option_not_offered": "That option is unticked, so it can't be the suggested one.",
    "invalid_offered_options": "Tick at least one of this proposal's options to offer.",
    "option_engine_output_empty": "This deal has no price yet. Enter your own estimate before anything can be checked or sent.",
    "no_priced_options": "This deal has no price yet. Enter your own estimate before anything can be checked or sent.",
    "option_evaluation_missing": "This proposal has no estimate behind it. Run the estimate first.",
    "option_ids_not_exact": "The options no longer match the estimate. Re-run the estimate.",
    "option_ids_out_of_order": "The options no longer match the estimate. Re-run the estimate.",
    "option_keys_not_complete": "The options no longer match the estimate. Re-run the estimate.",
    "option_import_subset_invalid": "The imported options don't match the estimate. Re-run the estimate.",
    "option_metric_mismatch": "The option figures don't match the estimate. Re-run the estimate.",
    "seed_data_stale": "The studio's reference data changed since this estimate. Re-run the estimate.",
    "seed_evaluation_missing": "This proposal has no estimate behind it. Run the estimate first.",
    "busy": "Another action is still saving. Try again in a moment.",
    "atomic_failure": "Something went wrong while saving. Nothing changed; try again.",
    "accepted_pending_confirmation": "The client has already picked an option. Confirm the booking instead.",
    "no_pending_acceptance": "The client hasn't picked an option yet, so there's nothing to confirm.",
    "acceptance_option_missing": "The option the client picked can't be found. Re-run the estimate and send a new proposal.",
    "scope_revision_changed": "The brief changed after this estimate. Re-run the estimate.",
    "buyer_reply_pending": "The client's latest chat message hasn't been answered yet. Wait for that reply first.",
}

_STATUS_WORDS = {
    "draft": "still a draft", "checked": "checked and waiting for you", "blocked": "blocked by the check",
    "published": "already sent", "confirmed": "already booked", "rejected": "already rejected",
    "superseded": "replaced by a newer version", "evaluated": "still being reviewed",
    "closed": "closed", "needs_information": "waiting for details from the client",
}


def explain(code):
    """Plain-English sentence for one refusal code (never empty)."""
    code = str(code or "")
    if code in _MESSAGES:
        return _MESSAGES[code]
    if code.startswith("status="):
        status = code.split("=", 1)[1]
        return f"This is {_STATUS_WORDS.get(status, status.replace('_', ' '))}, so that action isn't available."
    if code.startswith("unsafe_options:"):
        names = [n.replace("_", " ") for n in code.split(":", 1)[1].split(",") if n]
        return "These options no longer fit your calendar: " + ", ".join(names) + ". Re-run the estimate."
    if code.startswith("option_terms_mismatch"):
        return "The option terms changed after the estimate. Re-run the estimate."
    if code.startswith("manual_review_required"):
        return "A booked job can't be re-released or re-estimated here."
    if code.startswith("seed_") or code.startswith("mirror_"):
        return "The studio's reference data changed since this estimate. Re-run the estimate."
    return "That action was refused (" + code.replace("_", " ") + ")."


def refusal(reasons):
    """{'refusals': [{code, message}], 'message': one paragraph} for a refusal."""
    items, seen = [], set()
    for code in reasons or []:
        text = explain(code)
        items.append({"code": code, "message": text})
        if text not in seen:
            seen.add(text)
    return {"refusals": items, "message": " ".join(dict.fromkeys(i["message"] for i in items))}


# -- phase ------------------------------------------------------------------

PHASE_LABELS = {
    "new": "New inquiry", "needs_info": "Needs info", "needs_estimate": "Needs your estimate",
    "needs_fresh_estimate": "Needs a fresh estimate", "ready_for_review": "Ready for review",
    "counter_ready": "Counter ready", "draft_blocked": "Draft blocked", "sent": "Waiting on client",
    "client_picked": "Client picked an option", "change_requested": "Change requested",
    "booked": "Booked", "rejected": "Rejected", "closed": "Closed", "no_fit": "Doesn’t fit",
}

# Phases where the estimate's fit warnings and the outbound check still matter.
REVIEW_PHASES = {"no_fit", "ready_for_review", "counter_ready", "draft_blocked", "needs_fresh_estimate",
                 "needs_estimate"}


def phase(deal_status, versions, offers):
    """One clear phase for a deal from its stored status and latest version."""
    latest = versions[-1] if versions else None
    latest_offer = offers[-1] if offers else None
    if deal_status == "closed":
        return "closed"
    if deal_status == "rejected":
        return "rejected"
    if deal_status == "needs_information":
        return "needs_info"
    if latest is None:
        return "new"
    status = latest["status"]
    if status == "confirmed" or latest.get("confirmed") or deal_status == "confirmed":
        return "booked"
    # A newer offer round without its own version yet = a change being priced.
    if latest_offer and not latest_offer.get("evaluated") and latest_offer.get("round", 1) > 1:
        return "change_requested"
    if status == "published":
        return "sent"
    if status == "rejected":
        return "rejected"
    if status == "superseded":
        return "sent"
    if latest.get("verdict") == "decline" and latest.get("estimate"):
        return "no_fit"          # priced, but no option fits the calendar
    if latest.get("verdict") == "clarify" or not latest.get("options"):
        return "needs_estimate"
    if latest.get("verdict") == "counter":
        return "counter_ready"
    return "ready_for_review"


def fresh_estimate_reason(version, approvals):
    """Why a version needs a fresh estimate, or None."""
    if version is None or version.get("status") in ("published", "confirmed", "rejected", "superseded"):
        return None
    if version.get("capacity_stale"):
        return ("Your calendar changed after this estimate (another booking was confirmed), "
                "so the hours and the fit may be wrong. Re-run the estimate before approving.")
    refused = [a for a in approvals or [] if a.get("action") == "approve_rejected"
               and "capacity_changed" in (a.get("note") or "")]
    if refused:
        return ("Approve was refused because your calendar changed since this estimate. "
                "Re-run the estimate before approving.")
    return None


# -- test data ----------------------------------------------------------------

_TEST_SOURCE = re.compile(r"^(eval\d|qwen-|test|demo|smoke|fixture|ov-|s7-|order-|change-)", re.I)
_TEST_TEXT = re.compile(r"\b(sample data|synthetic|not a real (client|project)|demo inquiry|test)\b", re.I)


def is_test_deal(source, source_id, raw_inquiry):
    """Best guess whether a deal is evaluation or test data (never acts on it)."""
    if _TEST_SOURCE.search(source_id or ""):
        return True
    if source == "manual" and (source_id or "").startswith(("eval", "qwen")):
        return True
    return bool(_TEST_TEXT.search(raw_inquiry or ""))


# -- client-facing wording ----------------------------------------------------

def plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def client_date(iso, today=None):
    """'2026-12-11' -> '11 Dec' (adds the year when it isn't the current one)."""
    try:
        d = date.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    year = (today or date.today()).year
    text = f"{d.day} {d.strftime('%b')}"
    return text if d.year == year else f"{text} {d.year}"


def client_amount(value):
    """2700.0 -> 'S$2,700'; 2454.9 -> 'S$2,454.90'."""
    value = float(value)
    return f"S${value:,.0f}" if value == int(value) else f"S${value:,.2f}"


# -- outbound-check violations ------------------------------------------------

_VIOLATIONS = {
    "PRIVATE_NUMBER": "shows a private number ({text}), such as your minimum price, hours or capacity. Take it out",
    "PRIVATE_LABEL": "mentions internal pricing or capacity wording (e.g. floor, margin, capacity). Reword it",
    "OTHER_CLIENT_NAME": "names another client ({text}). Take the name out",
    "UNAPPROVED_AMOUNT": "has an amount that isn't one of the approved option prices ({text})",
    "UNAPPROVED_COUNT": "has a quantity that doesn't match the approved options ({text})",
    "UNAPPROVED_DATE": "has a date that doesn't match the approved options ({text})",
    "UNRECOGNIZED_NUMBER": "has a number the check can't tie to an approved option ({text})",
    "MULTI_OPTION_NUMERIC_PROSE": "repeats numbers in the message while several options are offered. Keep numbers on the option cards",
    "NO_PRICED_OPTIONS": "has no price behind it yet. Enter your own estimate first",
    "OPTION_SET_INVALID": "no longer matches the estimate's options. Re-run the estimate",
    "OPTION_ASSOCIATION_INVALID": "refers to an option that isn't part of this proposal",
    "INVALID_PAGE": "has a proposal page that couldn't be read. Re-save the page",
}


def explain_violation(v):
    code = (v or {}).get("code", "")
    text = str((v or {}).get("text", ""))[:80]
    body = _VIOLATIONS.get(code)
    if body is None:
        return "The draft was blocked (" + code.replace("_", " ").lower() + ")."
    return "The draft " + body.format(text=text) + "."


_DESCRIPTIVE = re.compile(r"\(|\binquiry\b|\b(promo|promos|video|videos|reel|reels|brief|project|shoot|campaign)\b", re.I)


def looks_like_description(client):
    """True when a 'client name' is really a project description or label."""
    name = (client or "").strip()
    if not name:
        return True
    return bool(_DESCRIPTIVE.search(name)) and len(name.split()) >= 3
