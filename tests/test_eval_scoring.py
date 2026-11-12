"""Tests for the deterministic eval scoring (the eval harness itself must be correct)."""

from __future__ import annotations

from pathlib import Path

import yaml

from evals import scoring

CALLS = [
    {"agent": "orchestrator", "tool": "sales_analytics", "args": {"request": "..."}, "response": "..."},
    {
        "agent": "sales_analytics",
        "tool": "run_query",
        "args": {
            "metrics": ["revenue_share", "revenue"],
            "dimensions": ["region"],
            "start_date": "2026-10-01",
            "limit": 100,
        },
        "response": {},
    },
]


def test_match_value_subset_and_sets() -> None:
    assert scoring.match_value({"metrics": ["revenue", "revenue_share"]}, CALLS[1]["args"])
    assert not scoring.match_value({"metrics": ["revenue"]}, CALLS[1]["args"])  # lists are exact sets
    assert scoring.match_value({"limit": "100"}, CALLS[1]["args"])  # "100" == 100
    assert not scoring.match_value({"filters": {"district": ["Kadıköy"]}}, CALLS[1]["args"])


def test_check_case_all_checks() -> None:
    case = {
        "route": ["sales_analytics"],
        "tool_calls": [{"tool": "run_query", "args": {"dimensions": ["region"]}}],
        "contains": ["Marmara"],
        "not_contains": ["kâr"],
        "trace_not_contains": ["10000000146"],
        "guardrail_warning": False,
    }
    checks = scoring.check_case(case, "Marmara %55,64", CALLS)
    assert checks == {"route": True, "tool_calls": True, "content": True, "guardrail": True}
    assert scoring.check_case(case, "Marmara ⚠️ Otomatik kontrol: ...", CALLS)["guardrail"] is False
    assert scoring.check_case({"route": []}, "x", CALLS)["route"] is False


def test_case_passed_needs_judge_threshold() -> None:
    assert scoring.case_passed({"route": True}, 4)
    assert not scoring.case_passed({"route": True}, 3)
    assert not scoring.case_passed({"route": False}, 5)
    assert scoring.case_passed({}, None)


def test_regressions() -> None:
    base = {"pass_rate": 0.9, "routing_accuracy": 1.0, "judge_mean": 4.5, "critical_failures": []}
    same = dict(base)
    assert scoring.regressions(same, base) == []
    worse = {**base, "routing_accuracy": 0.9, "judge_mean": 4.0, "critical_failures": ["guardrail_prompt_injection"]}
    problems = scoring.regressions(worse, base)
    assert "critical case failed: guardrail_prompt_injection" in problems
    assert any(p.startswith("routing_accuracy") for p in problems)
    assert any(p.startswith("judge_mean") for p in problems)


def test_cases_file_is_valid() -> None:
    cases = yaml.safe_load((Path(__file__).parent.parent / "evals" / "cases.yaml").read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    allowed = {
        "id",
        "turns",
        "route",
        "tool_calls",
        "contains",
        "not_contains",
        "trace_not_contains",
        "guardrail_warning",
        "judge",
        "critical",
    }
    for c in cases:
        assert c["turns"], c["id"]
        assert set(c) <= allowed, f"{c['id']}: unknown keys {set(c) - allowed}"


def test_content_checks_are_case_insensitive_turkish() -> None:
    case = {"contains": ["bölge müdürü", "iade"], "not_contains": ["kâr"]}
    assert scoring.check_case(case, "**Bölge Müdürü** onayı ile İADE yapılır", [])["content"] is True
