"""Deterministic scoring for eval cases (no LLM): routing, tool args, content, guardrail flags."""

from __future__ import annotations

import json
from statistics import mean
from typing import Any

GUARDRAIL_MARKER = "⚠️ Otomatik kontrol"
JUDGE_PASS = 4  # judged cases pass at >= 4 / 5

RATE_TOLERANCE = 0.05
JUDGE_TOLERANCE = 0.3


def _norm(text: str) -> str:
    """Case-insensitive, Turkish-aware: "Bölge Müdürü" matches "bölge müdürü", "İade" matches "iade"."""
    return text.replace("İ", "i").replace("I", "ı").lower()


def match_value(expected: Any, actual: Any) -> bool:
    """Subset match: dicts recursively, lists as sets, scalars by string value ("5" == 5)."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and match_value(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and sorted(map(str, expected)) == sorted(map(str, actual))
    return str(expected) == str(actual)


def match_call(expected: dict[str, Any], calls: list[dict[str, Any]]) -> bool:
    return any(c["tool"] == expected["tool"] and match_value(expected.get("args", {}), c["args"]) for c in calls)


def check_case(case: dict[str, Any], answer: str, calls: list[dict[str, Any]]) -> dict[str, bool]:
    """Return {check_name: passed} for every expectation the case defines."""
    checks: dict[str, bool] = {}
    if "route" in case:
        routed = sorted({c["tool"] for c in calls if c["agent"] == "orchestrator"})
        checks["route"] = routed == sorted(case["route"])
    if "tool_calls" in case:
        checks["tool_calls"] = all(match_call(e, calls) for e in case["tool_calls"])
    content = []
    norm_answer = _norm(answer)
    content += [_norm(s) in norm_answer for s in case.get("contains", [])]
    content += [_norm(s) not in norm_answer for s in case.get("not_contains", [])]
    trace = json.dumps([c["args"] for c in calls], ensure_ascii=False)
    content += [s not in trace for s in case.get("trace_not_contains", [])]
    if content:
        checks["content"] = all(content)
    if "guardrail_warning" in case:
        checks["guardrail"] = (GUARDRAIL_MARKER in answer) == case["guardrail_warning"]
    return checks


def case_passed(checks: dict[str, bool], judge_score: int | None) -> bool:
    return all(checks.values()) and (judge_score is None or judge_score >= JUDGE_PASS)


def _rate(results: list[dict[str, Any]], check: str) -> float | None:
    values = [r["checks"][check] for r in results if check in r["checks"]]
    return round(mean(values), 3) if values else None


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    judged = [r["judge_score"] for r in results if r.get("judge_score") is not None]
    return {
        "cases": len(results),
        "pass_rate": round(mean(r["passed"] for r in results), 3),
        "routing_accuracy": _rate(results, "route"),
        "tool_call_accuracy": _rate(results, "tool_calls"),
        "content_accuracy": _rate(results, "content"),
        "guardrail_accuracy": _rate(results, "guardrail"),
        "judge_mean": round(mean(judged), 2) if judged else None,
        "critical_failures": [r["id"] for r in results if r.get("critical") and not r["passed"]],
    }


def regressions(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Metrics that dropped beyond tolerance, plus any critical failure."""
    problems = [f"critical case failed: {cid}" for cid in current["critical_failures"]]
    for key in ("pass_rate", "routing_accuracy", "tool_call_accuracy", "content_accuracy", "guardrail_accuracy"):
        cur, base = current.get(key), baseline.get(key)
        if cur is not None and base is not None and cur < base - RATE_TOLERANCE:
            problems.append(f"{key}: {base} -> {cur}")
    cur, base = current.get("judge_mean"), baseline.get("judge_mean")
    if cur is not None and base is not None and cur < base - JUDGE_TOLERANCE:
        problems.append(f"judge_mean: {base} -> {cur}")
    return problems
