"""Run with: python3 -B -m unittest -v test_engine.py"""

import json
import sys
import unittest
from datetime import date
from pathlib import Path

from engine import capacity, evaluate, load_data, outbound_check


HERE = Path(__file__).parent
DATA = load_data()
COMPARABLES = ["P-031", "P-036", "P-041", "P-044"]


def request(*, ids=COMPARABLES, **changes):
    return {
        "as_of": "2026-11-23",
        "comparable_ids": list(ids),
        "offer": {
            "client": "Juniper Cafe", "format": "product_promo", "production_mode": "one_shoot_day",
            "shoot_days": 1, "locations": 1, "interviewees": 0, "scripting": "light",
            "deliverables": 3, "editing_level": "standard", "graphics": "basic_titles",
            "revision_rounds": 2, "caption_versions": 3, "deadline": "2026-12-11",
            "budget_sgd": 2700, "direct_costs_sgd": 0, **changes,
        },
    }


def inputs():
    missing = request()
    del missing["offer"]["budget_sgd"]
    counter = request(deliverables=5, caption_versions=5, deadline="2026-12-04")
    original = evaluate(request(), DATA)
    counter_result = evaluate(counter, DATA)
    phased = next(option for option in counter_result["options"] if option["kind"] == "phased")
    private = {"floor_sgd": 2454.90, "min_hourly_rate_sgd": 70, "capacity_hours": 50.4}

    def check(draft, approved=None):
        return {"draft": draft, "approved_options": original["options"] if approved is None else approved,
                "private_values": private, "other_client_names": ["Glow Studio", "Kopi Corner"]}

    return [
        ("inquiry_1", "evaluate", request()),
        ("counteroffer", "evaluate", counter),
        ("no_comparables", "evaluate", request(ids=[])),
        ("missing_budget", "evaluate", missing),
        ("custom_animation", "evaluate", request(graphics="custom_animation")),
        ("within_discount", "evaluate", request(budget_sgd=2550)),
        ("over_discount", "evaluate", request(deliverables=1, caption_versions=1, budget_sgd=1250)),
        ("reject_comparables", "evaluate", request(ids=[*COMPARABLES, "P-043", "P-047"])),
        ("invalid_counts", "evaluate", request(caption_versions=4)),
        ("approved_draft", "outbound", check("We can deliver three videos by 11 Dec for S$2,700.")),
        ("phased_draft", "outbound", check("Deliver 2 videos by 4 Dec and 3 videos by 18 Dec for S$4,000.", [phased])),
        ("private_floor_and_client", "outbound", check("Our price floor is S$2,454.90. Ask Glow Studio.")),
        ("invented_terms", "outbound", check("We can do five videos by 4 Dec for S$2,000.")),
    ]


def run(mode, payload):
    if mode == "outbound":
        return outbound_check(**payload)
    return evaluate(payload, DATA)


class DemoContract(unittest.TestCase):
    def test_capacities(self):
        for deadline, expected in (("2026-11-27", 18.0), ("2026-12-04", 28.8),
                                   ("2026-12-11", 50.4), ("2026-12-18", 72.0)):
            with self.subTest(deadline=deadline):
                self.assertEqual(capacity(DATA["calendar_weeks"], date(2026, 11, 23),
                                          date.fromisoformat(deadline), 10), expected)

    def test_inquiry_numbers(self):
        inquiry = evaluate(request(), DATA)
        self.assertEqual(inquiry["verdict"], "accept")
        self.assertEqual(inquiry["reason_codes"], [])
        self.assertEqual([row["unit_hours"] for row in inquiry["comparables"]["used"]], [6.5, 7, 7, 7.5])
        self.assertEqual(inquiry["estimate"], {
            "fixed_hours": 8, "unit_hours": 7, "editing_hours": 21,
            "addon_hours": {"captions": 1.5, "extra_revisions": 0, "extra_locations": 0,
                            "light_motion": 0, "full_script": 0},
            "base_hours": 30.5, "buffer_pct": 15, "planning_hours": 35.07,
            "capacity_hours": 50.4, "list_sgd": 2700, "floor_sgd": 2454.90,
            "effective_rate_sgd": 76.99,
        })

    def test_counter_numbers_and_all_four_options(self):
        result = evaluate(request(deliverables=5, caption_versions=5, deadline="2026-12-04"), DATA)
        self.assertEqual(result["verdict"], "counter")
        self.assertEqual(result["reason_codes"], ["CAPACITY_SHORTFALL", "BELOW_FLOOR"])
        keys = ("base_hours", "planning_hours", "capacity_hours", "list_sgd", "floor_sgd", "effective_rate_sgd")
        self.assertEqual(tuple(result["estimate"][key] for key in keys),
                         (45.5, 52.32, 28.8, 4000, 3662.40, 51.61))
        self.assertEqual([(o["kind"], o["deliverables"], o["deadline"], o["price_sgd"])
                          for o in result["options"]],
                         [("hold_date", 2, "2026-12-04", 2050),
                          ("hold_budget", 3, "2026-12-11", 2700),
                          ("hold_scope", 5, "2026-12-18", 4000),
                          ("phased", 5, "2026-12-18", 4000)])
        self.assertEqual(result["options"][-1]["phases"],
                         [{"deliverables": 2, "deadline": "2026-12-04"},
                          {"deliverables": 3, "deadline": "2026-12-18"}])
        self.assertEqual(result["option_metrics"]["hold_date"]["planning_hours"], 26.45)
        self.assertEqual(result["option_metrics"]["hold_date"]["floor_sgd"], 1851.50)

    def test_no_history_exact_values(self):
        result = evaluate(request(ids=[]), DATA)
        self.assertEqual(result["verdict"], "accept")
        self.assertEqual(result["estimate"]["planning_hours"], 38.12)
        self.assertEqual(result["estimate"]["floor_sgd"], 2668.40)

    def test_safety_and_validation_cases(self):
        outputs = {name: run(mode, payload) for name, mode, payload in inputs()}
        for name, verdict, codes in (
            ("missing_budget", "clarify", ["MISSING_FIELD"]),
            ("custom_animation", "clarify", ["MANUAL_ESTIMATE_REQUIRED"]),
            ("within_discount", "accept_with_approval", ["BELOW_LIST_NEEDS_APPROVAL"]),
            ("over_discount", "counter", ["OVER_DISCOUNT_LIMIT"]),
            ("reject_comparables", "accept", ["COMPARABLE_MISMATCH"]),
            ("invalid_counts", "clarify", ["INVALID_FIELD"]),
        ):
            self.assertEqual((outputs[name]["verdict"], outputs[name]["reason_codes"]), (verdict, codes))
        self.assertEqual([r["project_id"] for r in outputs["reject_comparables"]["comparables"]["excluded"]],
                         ["P-043", "P-047"])
        for name in ("approved_draft", "phased_draft"):
            self.assertEqual(outputs[name], {"passed": True, "violations": []})
        self.assertEqual(outputs["private_floor_and_client"], {
            "passed": False,
            "violations": [
                {"code": "PRIVATE_LABEL", "text": "internal pricing or capacity label"},
                {"code": "OTHER_CLIENT_NAME", "text": "Glow Studio"},
                {"code": "PRIVATE_NUMBER", "text": "2,454.90"},
                {"code": "UNAPPROVED_AMOUNT", "text": "S$2,454.90"},
            ],
        })
        self.assertEqual(outputs["invented_terms"], {
            "passed": False,
            "violations": [
                {"code": "UNAPPROVED_AMOUNT", "text": "S$2,000"},
                {"code": "UNAPPROVED_DATE", "text": "4 Dec"},
                {"code": "UNAPPROVED_COUNT", "text": "five videos"},
            ],
        })

    def test_full_json_fixtures(self):
        cases = json.loads((HERE / "test_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 13)
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(run(case["mode"], case["input"]), case["expected"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--print-fixtures"]:
        print(json.dumps([
            {"name": name, "mode": mode, "input": payload, "expected": run(mode, payload)}
            for name, mode, payload in inputs()
        ], indent=2))
    else:
        unittest.main()
