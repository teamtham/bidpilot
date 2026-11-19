"""BidPilot demo engine. Python standard library only."""

import argparse
import csv
import json
import math
import re
import statistics
import sys
import unicodedata
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path


DATA = Path(__file__).with_name("data")
COUNTS = ("shoot_days", "locations", "interviewees", "deliverables",
          "revision_rounds", "caption_versions")
MONEY = ("budget_sgd", "direct_costs_sgd")
ENUMS = {
    "format": {"product_promo", "social_reel", "testimonial", "event_recap", "explainer"},
    "production_mode": {"edit_only", "one_shoot_day", "multi_day"},
    "scripting": {"none", "light", "full"},
    "editing_level": {"simple", "standard", "heavy"},
    "graphics": {"none", "basic_titles", "light_motion", "custom_animation"},
}
COUNT_MAX = 100
MONEY_MAX = 10_000_000


def load_data(directory=DATA):
    """Read equivalent named arrays from one JSON file, or the five CSV files."""
    source = Path(directory)
    if source.is_file():
        return json.loads(source.read_text(encoding="utf-8"))
    tables = {}
    for name in ("business_rules", "effort_rules", "rate_card", "past_projects", "calendar_weeks"):
        with (source / f"{name}.csv").open(newline="", encoding="utf-8-sig") as stream:
            tables[name] = list(csv.DictReader(stream))
    return tables


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use YYYY-MM-DD.")
    return date.fromisoformat(value)


def validation_errors(request):
    if not isinstance(request, dict):
        return [("INVALID_FIELD", "request")]
    offer = request.get("offer")
    if not isinstance(offer, dict):
        return [("MISSING_FIELD" if offer is None else "INVALID_FIELD", "offer")]
    errors = []
    for field in ("as_of", "comparable_ids"):
        if request.get(field) is None:
            errors.append(("MISSING_FIELD", field))
    for field in ("client", "deadline", *ENUMS, *COUNTS, *MONEY):
        if offer.get(field) is None:
            errors.append(("MISSING_FIELD", f"offer.{field}"))
    if errors:
        return errors

    for field, choices in ENUMS.items():
        if not isinstance(offer[field], str) or offer[field] not in choices:
            errors.append(("INVALID_FIELD", f"offer.{field}"))
    if not isinstance(offer["client"], str) or not offer["client"].strip():
        errors.append(("INVALID_FIELD", "offer.client"))
    for field in COUNTS:
        minimum = 1 if field == "deliverables" else 0
        if type(offer[field]) is not int or not minimum <= offer[field] <= COUNT_MAX:
            errors.append(("INVALID_FIELD", f"offer.{field}"))
    for field in MONEY:
        value = offer[field]
        if (type(value) not in (int, float) or not 0 <= value <= MONEY_MAX
                or not math.isfinite(value) or round(value, 2) != value):
            errors.append(("INVALID_FIELD", f"offer.{field}"))
    ids = request["comparable_ids"]
    if (not isinstance(ids, list)
            or any(not isinstance(item, str) or not item.strip() for item in ids)):
        errors.append(("INVALID_FIELD", "comparable_ids"))
    for field, value in (("as_of", request["as_of"]), ("offer.deadline", offer["deadline"])):
        try:
            iso_date(value)
        except ValueError:
            errors.append(("INVALID_FIELD", field))
    if errors:
        return errors
    if offer["deadline"] < request["as_of"]:
        errors.append(("INVALID_FIELD", "offer.deadline"))
    if offer["caption_versions"] > offer["deliverables"]:
        errors.append(("INVALID_FIELD", "offer.caption_versions"))
    days = offer["shoot_days"]
    mode = "edit_only" if days == 0 else "one_shoot_day" if days == 1 else "multi_day"
    if offer["production_mode"] != mode:
        errors.append(("INVALID_FIELD", "offer.production_mode"))
    if (days == 0 and offer["locations"] != 0) or (days > 0 and offer["locations"] == 0):
        errors.append(("INVALID_FIELD", "offer.locations"))
    return errors


def answer(verdict, reasons, *, missing=None, invalid=None, comparables=None,
           estimate=None, options=None, option_metrics=None):
    return {
        "verdict": verdict,
        "reason_codes": list(dict.fromkeys(reasons)),
        "missing_fields": missing or [],
        "invalid_fields": invalid or [],
        "comparables": comparables or {"used": [], "excluded": []},
        "estimate": estimate,
        "options": options or [],
        "option_metrics": option_metrics or {},
    }


def numeric_table(rows, key, value):
    return {row[key]: float(row[value]) for row in rows if row[value] != "manual"}


def fixed_hours(shoot_days, effort):
    if shoot_days == 0:
        return effort["fixed_edit_only"]
    return effort["fixed_one_shoot_day"] + (shoot_days - 1) * effort["fixed_extra_shoot_day"]


def addons(offer, effort, prices, included_revisions):
    units = {
        "captions": (offer["caption_versions"], "caption_per_version", "caption_version"),
        "extra_revisions": (max(0, offer["revision_rounds"] - included_revisions),
                            "extra_revision_round", "extra_revision_round"),
        "extra_locations": (max(0, offer["locations"] - 1), "extra_location", "extra_location"),
        "light_motion": (offer["deliverables"] if offer["graphics"] == "light_motion" else 0,
                         "light_motion_per_video", "light_motion"),
        "full_script": (int(offer["scripting"] == "full"), "full_script", "full_script"),
    }
    return {
        name: {"hours": count * effort[hours], "price_sgd": count * prices[price]}
        for name, (count, hours, price) in units.items()
    }


def comparable_evidence(offer, ids, projects, effort, included_revisions):
    rows = {row["project_id"]: row for row in projects}
    used, excluded = [], []
    for project_id in dict.fromkeys(ids):
        row = rows.get(project_id)
        mismatches = []
        if row is None:
            mismatches.append("unknown_project_id")
        else:
            for field in ("format", "production_mode", "editing_level"):
                if row[field] != offer[field]:
                    mismatches.append(field)
            if int(row["shoot_days"]) != offer["shoot_days"]:
                mismatches.append("shoot_days")
            if int(row["locations"]) != min(offer["locations"], 1):
                mismatches.append("locations")
            if row["scripting"] == "full" or (
                    offer["scripting"] != "full" and row["scripting"] != offer["scripting"]):
                mismatches.append("scripting")
            if row["graphics"] not in ("none", "basic_titles") or (
                    offer["graphics"] != "light_motion" and row["graphics"] != offer["graphics"]):
                mismatches.append("graphics")
            if int(row["revision_rounds"]) != included_revisions:
                mismatches.append("revision_rounds")
            if int(row["caption_versions"]) != 0:
                mismatches.append("caption_versions")
            if (int(row["deliverables"]) < 1
                    or float(row["actual_hours"]) <= fixed_hours(int(row["shoot_days"]), effort)):
                mismatches.append("actual_hours_or_deliverables")
        if mismatches:
            excluded.append({"project_id": project_id, "reason": "COMPARABLE_MISMATCH",
                             "fields": mismatches})
        else:
            hours = (float(row["actual_hours"]) - fixed_hours(int(row["shoot_days"]), effort))
            used.append({"project_id": project_id, "unit_hours": hours / int(row["deliverables"])})
    return {"used": used, "excluded": excluded}


def capacity(weeks, as_of, deadline, reserve_pct):
    free = sum(
        max(0, float(row["working_hours"]) - float(row["calendar_busy_hours"])
            - float(row["unscheduled_owed_hours"]))
        for row in weeks if as_of + timedelta(days=4) <= iso_date(row["week_ending"]) <= deadline
    )
    return round(free * (1 - reserve_pct / 100), 2)


def estimate_offer(offer, config, unit_hours, buffer_pct, available):
    rules, effort, prices = config
    add = addons(offer, effort, prices, rules["included_revision_rounds"])
    fixed = fixed_hours(offer["shoot_days"], effort)
    editing = offer["deliverables"] * unit_hours
    base = fixed + editing + sum(item["hours"] for item in add.values())
    # Match the reference's binary-float Python round at this exact step.
    planned = round(base * (1 + buffer_pct / 100), 2)
    setup = prices["edit_only_setup"] if offer["shoot_days"] == 0 else prices["one_shoot_day"]
    if offer["shoot_days"] > 1:
        setup += (offer["shoot_days"] - 1) * prices["extra_shoot_day"]
    list_price = round(setup + offer["deliverables"] * prices["promo_video"]
                       + sum(item["price_sgd"] for item in add.values()), 2)
    budget = offer["budget_sgd"]
    return {
        "fixed_hours": round(fixed, 2), "unit_hours": round(unit_hours, 2),
        "editing_hours": round(editing, 2),
        "addon_hours": {key: round(value["hours"], 2) for key, value in add.items()},
        "base_hours": round(base, 2), "buffer_pct": buffer_pct, "planning_hours": planned,
        "capacity_hours": available, "list_sgd": list_price,
        "floor_sgd": round(planned * rules["min_hourly_rate_sgd"] + offer["direct_costs_sgd"], 2),
        "effective_rate_sgd": round(budget / planned, 2),
    }


def public_option(kind, offer, price, phases=None):
    return {
        "option_id": kind, "kind": kind, "deliverables": offer["deliverables"],
        "caption_versions": offer["caption_versions"], "revision_rounds": offer["revision_rounds"],
        "shoot_days": offer["shoot_days"], "locations": offer["locations"],
        "deadline": offer["deadline"], "price_sgd": round(price, 2), "phases": phases or [],
    }


def alternatives(offer, weeks, as_of, config, unit_hours, buffer_pct):
    rules = config[0]
    original_deadline = iso_date(offer["deadline"])
    budget = offer["budget_sgd"]
    fridays = sorted({
        iso_date(row["week_ending"]) for row in weeks
        if as_of <= iso_date(row["week_ending"]) <= as_of + timedelta(weeks=8)
        and iso_date(row["week_ending"]).weekday() == 4
    })
    options, metrics, seen = [], {}, set()

    def candidate(count, due):
        draft = {**offer, "deliverables": count, "deadline": due.isoformat(),
                 "caption_versions": min(count, offer["caption_versions"])}
        available = capacity(weeks, as_of, due, rules["capacity_buffer_pct"])
        return draft, estimate_offer(draft, config, unit_hours, buffer_pct, available)

    def fits(estimate, price):
        minimum = estimate["list_sgd"] * (1 - rules["discount_limit_pct"] / 100)
        return (estimate["planning_hours"] <= estimate["capacity_hours"]
                and price >= estimate["floor_sgd"] and price >= round(minimum, 2))

    def save(kind, draft, estimate, price, phases=None, early=None):
        option = public_option(kind, draft, price, phases)
        signature = json.dumps({key: value for key, value in option.items()
                                if key not in ("kind", "option_id")}, sort_keys=True)
        if signature in seen:
            return
        seen.add(signature)
        options.append(option)
        metrics[kind] = {**estimate, "effective_rate_sgd": round(price / estimate["planning_hours"], 2)}
        if early is not None:
            metrics[kind]["first_phase_planning_hours"] = early["planning_hours"]
            metrics[kind]["first_phase_capacity_hours"] = early["capacity_hours"]

    count = offer["deliverables"]
    early_fit = None
    for size in range(count, 0, -1):
        draft, checked = candidate(size, original_deadline)
        if fits(checked, checked["list_sgd"]):
            early_fit = (draft, checked)
            save("hold_date", draft, checked, checked["list_sgd"])
            break

    budget_fit = None
    for size in range(count, 0, -1):
        for due in fridays:
            draft, checked = candidate(size, due)
            price = min(budget, checked["list_sgd"])
            if fits(checked, price):
                budget_fit = (draft, checked, price)
                break
        if budget_fit:
            save("hold_budget", *budget_fit)
            break

    full_fit = None
    for due in fridays:
        draft, checked = candidate(count, due)
        if fits(checked, checked["list_sgd"]):
            full_fit = (draft, checked)
            save("hold_scope", draft, checked, checked["list_sgd"])
            break

    if early_fit and full_fit:
        early, early_estimate = early_fit
        full, full_estimate = full_fit
        if early["deliverables"] < count and early["deadline"] < full["deadline"]:
            phases = [
                {"deliverables": early["deliverables"], "deadline": early["deadline"]},
                {"deliverables": count - early["deliverables"], "deadline": full["deadline"]},
            ]
            save("phased", full, full_estimate, full_estimate["list_sgd"], phases, early_estimate)
    return options, metrics


def evaluate(request, data):
    errors = validation_errors(request)
    if errors:
        return answer("clarify", [code for code, _ in errors],
                      missing=[field for code, field in errors if code == "MISSING_FIELD"],
                      invalid=[field for code, field in errors if code == "INVALID_FIELD"])
    offer = request["offer"]
    rules = numeric_table(data["business_rules"], "key", "value")
    effort = numeric_table(data["effort_rules"], "rule", "hours")
    prices = numeric_table(data["rate_card"], "item", "price_sgd")
    if (offer["graphics"] == "custom_animation" or offer["interviewees"] > 0
            or offer["format"] != "product_promo"
            or (offer["shoot_days"] > 1 and "extra_shoot_day" not in prices)):
        return answer("clarify", ["MANUAL_ESTIMATE_REQUIRED"])
    comparables = comparable_evidence(offer, request["comparable_ids"], data["past_projects"],
                                      effort, rules["included_revision_rounds"])
    evidence = comparables["used"]
    unit_hours = (statistics.median(row["unit_hours"] for row in evidence)
                  if evidence else effort["baseline_hours_per_promo_video"])
    key = ("buffer_pct_3plus_comparables" if len(evidence) >= 3 else
           "buffer_pct_1to2_comparables" if evidence else "buffer_pct_no_comparables")
    buffer_pct = rules[key]
    as_of = iso_date(request["as_of"])
    deadline = iso_date(offer["deadline"])
    weeks = data["calendar_weeks"]
    available = capacity(weeks, as_of, deadline, rules["capacity_buffer_pct"])
    config = rules, effort, prices
    estimate = estimate_offer(offer, config, unit_hours, buffer_pct, available)
    budget = offer["budget_sgd"]
    list_price = estimate["list_sgd"]
    minimum = round(list_price * (1 - rules["discount_limit_pct"] / 100), 2)
    reasons = []
    if estimate["planning_hours"] > available:
        reasons.append("CAPACITY_SHORTFALL")
    if budget < estimate["floor_sgd"]:
        reasons.append("BELOW_FLOOR")
    # Report excessive discount independently when it is the reason a viable-price
    # offer fails. Below-floor quotes already fail the stronger pricing rule.
    if budget >= estimate["floor_sgd"] and budget < minimum:
        reasons.append("OVER_DISCOUNT_LIMIT")
    if reasons:
        options, metrics = alternatives(offer, weeks, as_of, config, unit_hours, buffer_pct)
        verdict = "counter" if options else "decline"
        if not options:
            reasons.append("NO_FEASIBLE_OPTIONS")
    else:
        verdict = "accept_with_approval" if budget < list_price else "accept"
        if verdict == "accept_with_approval":
            reasons.append("BELOW_LIST_NEEDS_APPROVAL")
        options = [public_option("as_offered", offer, min(budget, list_price))]
        metrics = {}
    if comparables["excluded"]:
        reasons.append("COMPARABLE_MISMATCH")
    return answer(verdict, reasons, comparables=comparables, estimate=estimate,
                  options=options, option_metrics=metrics)


AMOUNT = re.compile(
    r"(?<!\w)(?:S\$|SGD\s*|\$)\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?)"
    r"(?!\w|[.,][0-9])", re.IGNORECASE,
)
ISO = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
MONTHS = ("January February March April May June July August September October November December").split()
MONTH = "|".join(sorted({part for month in MONTHS for part in (month, month[:3])},
                        key=len, reverse=True))
DATE_TEXT = re.compile(
    rf"\b(?:(\d{{1,2}})\s+({MONTH})|({MONTH})\s+(\d{{1,2}}))(?:,?\s+(\d{{4}}))?\b",
    re.IGNORECASE,
)
WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
         "fifteen sixteen seventeen eighteen nineteen twenty").split()
NUMBER_WORDS = re.compile(r"\b(?:" + "|".join(WORDS) + r"|thirty|forty|fifty|sixty|seventy|eighty|"
                          r"ninety|hundred|thousand|million|billion|first|second|third|fourth|"
                          r"fifth|sixth|seventh|eighth|ninth|tenth)\b", re.IGNORECASE)
COUNT = re.compile(
    r"\b(\d+|(?:" + "|".join(WORDS) + r"))\s+"
    r"(videos?|caption(?:ed)? versions?|revision rounds?|revisions?|shoot days?|locations?)\b",
    re.IGNORECASE,
)
PRIVATE_LABEL = re.compile(
    r"\b(?:price floor|minimum (?:hourly )?rate|capacity (?:in )?hours|available hours|"
    r"planning hours|internal floor)\b", re.IGNORECASE
)
RAW_NUMBER = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")


def outbound_check(draft, approved_options, private_values, other_client_names):
    """Check a draft before review. Always render actual commitments from structured options."""
    if not isinstance(draft, str) or not draft.strip():
        raise ValueError("The outbound draft must contain text.")
    draft = unicodedata.normalize("NFKC", draft)
    normalized = " ".join(draft.casefold().split())
    findings = []

    def flag(code, text):
        item = {"code": code, "text": text}
        if item not in findings:
            findings.append(item)

    def scan(pattern, text, check):
        def visit(match):
            check(match)
            return " " * len(match[0])
        return pattern.sub(visit, text)

    if PRIVATE_LABEL.search(normalized):
        flag("PRIVATE_LABEL", "internal pricing or capacity label")
    for name in other_client_names:
        clean = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
        if clean and re.search(r"(?<!\w)" + re.escape(clean) + r"(?!\w)", normalized):
            flag("OTHER_CLIENT_NAME", name)
    private = {Decimal(str(value)) for value in private_values.values() if value is not None}
    without_dates = DATE_TEXT.sub(" ", ISO.sub(" ", draft))
    for match in RAW_NUMBER.finditer(without_dates):
        if Decimal(match[0].replace(",", "")) in private:
            flag("PRIVATE_NUMBER", match[0])

    amounts = {Decimal(str(option["price_sgd"])) for option in approved_options}
    dates = {option["deadline"] for option in approved_options}
    counts = {key: {option.get(key) for option in approved_options} for key in
              ("deliverables", "caption_versions", "revision_rounds", "shoot_days", "locations")}
    for option in approved_options:
        for phase in option.get("phases", []):
            dates.add(phase["deadline"])
            counts["deliverables"].add(phase["deliverables"])

    def amount(match):
        if Decimal(match[1].replace(",", "")) not in amounts:
            flag("UNAPPROVED_AMOUNT", match[0])

    def iso(match):
        if match[0] not in dates:
            flag("UNAPPROVED_DATE", match[0])

    def written_date(match):
        day, month = (match[1], match[2]) if match[1] else (match[4], match[3])
        month_number = next(i for i, name in enumerate(MONTHS, 1)
                            if name.lower().startswith(month.lower()))
        allowed = [value for value in dates if iso_date(value).month == month_number
                   and iso_date(value).day == int(day)
                   and (not match[5] or iso_date(value).year == int(match[5]))]
        if len(allowed) != 1:
            flag("UNAPPROVED_DATE", match[0])

    def count(match):
        raw, unit = match[1].lower(), match[2].lower()
        value = int(raw) if raw.isdigit() else WORDS.index(raw)
        if unit.startswith("video"):
            key = "deliverables"
        elif unit.startswith("caption"):
            key = "caption_versions"
        elif unit.startswith("revision"):
            key = "revision_rounds"
        elif unit.startswith("shoot"):
            key = "shoot_days"
        else:
            key = "locations"
        if value not in counts[key]:
            flag("UNAPPROVED_COUNT", match[0])

    rest = scan(AMOUNT, draft, amount)
    rest = scan(ISO, rest, iso)
    rest = scan(DATE_TEXT, rest, written_date)
    rest = scan(COUNT, rest, count)
    for match in re.finditer(r"\d+|S\$|\bSGD\b|\$", rest, re.IGNORECASE):
        flag("UNRECOGNIZED_NUMBER", match[0])
    for match in NUMBER_WORDS.finditer(rest):
        flag("UNRECOGNIZED_NUMBER", match[0])
    return {"passed": not findings, "violations": findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="-", help="JSON file, or - for stdin")
    parser.add_argument("--data", type=Path, default=DATA, help="CSV directory or JSON tables")
    parser.add_argument("--outbound", action="store_true", help="Check a draft instead of an offer")
    args = parser.parse_args()
    try:
        if args.input == "-":
            request = json.load(sys.stdin)
        else:
            with open(args.input, encoding="utf-8") as stream:
                request = json.load(stream)
        if args.outbound:
            output = outbound_check(request["draft"], request["approved_options"],
                                    request["private_values"], request["other_client_names"])
        else:
            output = evaluate(request, load_data(args.data))
        print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
    except (OSError, KeyError, TypeError, ValueError, ArithmeticError) as error:
        print(json.dumps({"error": "ENGINE_INPUT_OR_DATA_ERROR"}))
        print(f"Internal error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
