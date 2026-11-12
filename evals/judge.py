"""LLM-as-judge: scores an answer 1-5 against the expected behaviour and the tool outputs.

The judge sees what the tools actually returned, so it can catch claims the regex guardrails
cannot (e.g. a rule stated more broadly than the source says). Temperature 0 and a fixed rubric
keep scores stable; JUDGE_MODEL lets you use a different (ideally stronger) model than the agent,
so the system is not grading itself.
"""

from __future__ import annotations

import json
import os
from typing import Any

JUDGE_PROMPT = """\
You are grading an answer from a retail store-operations assistant. The assistant answers in Turkish.

Conversation (user turns):
{turns}

Final answer:
<<<
{answer}
>>>

What a correct answer does:
{expectation}

Tool outputs the assistant had (the only allowed source of facts):
<<<
{evidence}
>>>

Score 1-5:
5 = fully correct, every fact supported by the tool outputs, nothing important missing
4 = correct with a minor omission or wording issue
3 = partly correct, or an unsupported / overstated claim
2 = mostly wrong or missing the key point
1 = wrong, fabricated facts, or unsafe

Return JSON only: {{"score": <1-5>, "reason": "<one sentence>"}}
"""


def _compact(output: Any) -> Any:
    """Keep what the judge needs (rows, titles, passages); drop SQL and URIs to save space."""
    if isinstance(output, dict) and "sources" in output:
        return [
            {"id": src["source_id"], "title": src["title"], "passages": src["passages"]} for src in output["sources"]
        ]
    if isinstance(output, dict) and "rows" in output:
        return output["rows"]
    return output


def _evidence(calls: list[dict[str, Any]], limit: int = 60_000) -> str:
    # Specialists' raw tool outputs; skip the orchestrator's AgentTool calls (they are the specialists' answers).
    # The limit must hold every source: a truncated evidence makes the judge flag correct claims as unsupported.
    items = [
        {"tool": c["tool"], "args": c["args"], "output": _compact(c["response"])}
        for c in calls
        if c["agent"] != "orchestrator"
    ]
    if not items:
        return "(no tool was called)"
    text = json.dumps(items, ensure_ascii=False, default=str)
    return text[:limit] + (" ...[truncated]" if len(text) > limit else "")


async def judge(turns: list[str], answer: str, expectation: str, calls: list[dict[str, Any]]) -> tuple[int, str]:
    from google import genai
    from google.genai import types

    client = genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
    )
    prompt = JUDGE_PROMPT.format(
        turns="\n".join(f"- {t}" for t in turns),
        answer=answer,
        expectation=expectation,
        evidence=_evidence(calls),
    )
    model = os.environ.get("JUDGE_MODEL") or os.environ.get("MODEL_ID", "gemini-3.8-flash")
    resp = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
    )
    try:
        data = json.loads(resp.text or "{}")
        return max(1, min(5, int(data["score"]))), str(data.get("reason", ""))
    except (ValueError, KeyError, TypeError):
        return 1, f"unparseable judge output: {(resp.text or '')[:200]}"
