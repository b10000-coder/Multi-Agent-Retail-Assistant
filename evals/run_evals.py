"""Run the eval set against the real agent and compare with the stored baseline.

Usage (MCP Toolbox must be running for analytics cases):
    uv run python evals/run_evals.py                    # run, print table, compare with baseline
    uv run python evals/run_evals.py --only policy_     # cases whose id starts with "policy_"
    uv run python evals/run_evals.py --save-baseline    # accept current scores as the new baseline
    uv run python evals/run_evals.py --no-judge         # deterministic checks only (no judge calls)

Exit code 1 if a critical case fails or a metric regresses beyond tolerance -> fails CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
sys.path.insert(0, str(ROOT))

from evals import scoring


def load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / "retail_agent" / ".env")
    # Freeze "today" so relative dates in the cases ("geçen ay", "bu hafta") always mean the same period.
    os.environ.setdefault("TODAY_OVERRIDE", "2026-11-13")


async def run_case(case: dict[str, Any], use_judge: bool) -> dict[str, Any]:
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from evals.judge import judge
    from retail_agent import tracing
    from retail_agent.agent import root_agent

    runner = InMemoryRunner(agent=root_agent, app_name="retail_eval")
    session = await runner.session_service.create_session(app_name="retail_eval", user_id="eval")
    started = time.monotonic()
    answer, calls = "", []
    try:
        for turn in case["turns"]:
            calls = tracing.start()  # expectations apply to the last turn's calls
            answer = ""
            message = types.Content(role="user", parts=[types.Part(text=turn)])
            async for event in runner.run_async(user_id="eval", session_id=session.id, new_message=message):
                if event.author == "orchestrator" and event.is_final_response() and event.content:
                    answer = "".join(p.text or "" for p in event.content.parts or [] if not p.thought)
        error = None
    except Exception as e:  # noqa: BLE001  (a crash is a failed case, not a crashed run)
        error = f"{type(e).__name__}: {e}"

    checks = scoring.check_case(case, answer, calls) if error is None else {"error": False}
    score, reason = None, ""
    if use_judge and case.get("judge") and error is None:
        score, reason = await judge(case["turns"], answer, case["judge"], calls)
    return {
        "id": case["id"],
        "critical": bool(case.get("critical")),
        "checks": checks,
        "judge_score": score,
        "judge_reason": reason,
        "passed": error is None and scoring.case_passed(checks, score),
        "answer": answer,
        "routed": sorted({c["tool"] for c in calls if c["agent"] == "orchestrator"}),
        "tool_calls": [{"agent": c["agent"], "tool": c["tool"], "args": c["args"]} for c in calls],
        "error": error,
        "seconds": round(time.monotonic() - started, 1),
    }


def print_table(results: list[dict[str, Any]]) -> None:
    def cell(r: dict[str, Any], key: str) -> str:
        return "-" if key not in r["checks"] else ("ok" if r["checks"][key] else "FAIL")

    print(f"\n{'case':38} {'route':6} {'tools':6} {'content':8} {'guard':6} {'judge':6} result")
    for r in results:
        judge = "-" if r["judge_score"] is None else str(r["judge_score"])
        result = "PASS" if r["passed"] else ("FAIL*" if r["critical"] else "FAIL")
        print(
            f"{r['id']:38} {cell(r, 'route'):6} {cell(r, 'tool_calls'):6} {cell(r, 'content'):8} "
            f"{cell(r, 'guardrail'):6} {judge:6} {result}"
        )
        if not r["passed"]:
            failed = [k for k, ok in r["checks"].items() if not ok]
            if r["error"]:
                print(f"    -> error: {r['error']}")
            if failed:
                print(f"    -> failed checks: {failed}; routed={r['routed']}")
                print(f"    -> answer: {r['answer'][:300]!r}")
            if r["judge_score"] is not None and r["judge_score"] < scoring.JUDGE_PASS:
                print(f"    -> judge {r['judge_score']}: {r['judge_reason']}")


def write_github_summary(results: list[dict[str, Any]], summary: dict[str, Any], problems: list[str]) -> None:
    """In GitHub Actions, render the result table on the run / PR checks page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Eval results", "", "| case | checks | judge | result |", "|---|---|---|---|"]
    for r in results:
        checks = " ".join(f"{k}:{'✅' if ok else '❌'}" for k, ok in r["checks"].items()) or "-"
        judge = "-" if r["judge_score"] is None else str(r["judge_score"])
        result = "PASS" if r["passed"] else ("**FAIL (critical)**" if r["critical"] else "**FAIL**")
        lines.append(f"| `{r['id']}` | {checks} | {judge} | {result} |")
    lines += ["", "```json", json.dumps(summary, indent=2, ensure_ascii=False), "```"]
    lines += ["", "**Regressions:** " + ("; ".join(problems) if problems else "none")]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="", help="run cases whose id starts with this prefix")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    load_env()
    cases = [
        c for c in yaml.safe_load((EVALS / "cases.yaml").read_text(encoding="utf-8")) if c["id"].startswith(args.only)
    ]
    print(f"Running {len(cases)} cases (TODAY_OVERRIDE={os.environ['TODAY_OVERRIDE']}) ...")

    sem = asyncio.Semaphore(args.concurrency)

    async def bounded(case: dict[str, Any]) -> dict[str, Any]:
        async with sem:  # each task gets its own tracing context
            return await run_case(case, use_judge=not args.no_judge)

    results = await asyncio.gather(*(bounded(c) for c in cases))
    print_table(results)
    summary = scoring.aggregate(results)
    print("\n" + json.dumps(summary, indent=2, ensure_ascii=False))

    out_dir = EVALS / "results"
    out_dir.mkdir(exist_ok=True)
    report = {"summary": summary, "results": results, "today": os.environ["TODAY_OVERRIDE"]}
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    baseline_path = EVALS / "baseline.json"
    if args.save_baseline:
        if args.only:
            print("refusing to save a baseline from a partial run (--only)")
            return 1
        baseline_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        print(f"baseline saved to {baseline_path.relative_to(ROOT)}")
        return 0
    if not baseline_path.exists() or args.only:
        print("no comparison (no baseline yet, or partial run)")
        return 1 if summary["critical_failures"] else 0

    problems = scoring.regressions(summary, json.loads(baseline_path.read_text()))
    write_github_summary(results, summary, problems)
    if problems:
        print("\nREGRESSION:\n  " + "\n  ".join(problems))
        return 1
    print("\nNo regression against baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
