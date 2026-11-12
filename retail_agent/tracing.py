"""Records every tool call across all agents, so evals can see inside AgentTool sub-agents.

AgentTool runs a specialist in its own session, so the orchestrator's events only show
"called sales_analytics". The eval runner needs the specialist's own tool calls too
(e.g. run_query args). guardrails.record_tool_evidence (an after_tool_callback on every
agent) reports here. Outside an eval run nothing is recorded.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_calls: ContextVar[list[dict[str, Any]] | None] = ContextVar("tool_calls", default=None)


def start() -> list[dict[str, Any]]:
    """Begin recording for the current task (and the tasks it spawns). Returns the live list."""
    calls: list[dict[str, Any]] = []
    _calls.set(calls)
    return calls


def record(agent: str, tool: str, args: dict[str, Any], response: Any) -> None:
    calls = _calls.get()
    if calls is not None:
        calls.append({"agent": agent, "tool": tool, "args": dict(args), "response": response})
