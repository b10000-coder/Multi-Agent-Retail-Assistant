"""Sales analytics agent: question -> QuerySpec -> SQL (semantic layer) -> BigQuery (via MCP Toolbox)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from pydantic import ValidationError

from retail_agent import config, prompts
from retail_agent.guardrails import output_guardrail, record_tool_evidence
from retail_agent.semantic.cache import QueryCache
from retail_agent.semantic.compiler import QuerySpec, SemanticError, compile_spec, default_model

# Range covered by the synthetic dataset (scripts/generate_data.py, size=small).
DATA_START = date(2026, 5, 5)
DATA_END = date(2026, 10, 31)

logger = logging.getLogger(__name__)

SqlExecutor = Callable[[str], Awaitable[Any]]


async def toolbox_execute_sql(sql: str) -> Any:
    """Run SQL through the MCP Toolbox `execute_sql` tool. The agent process never holds BigQuery credentials."""
    from toolbox_core import ToolboxClient

    async with ToolboxClient(config.TOOLBOX_URL) as client:
        tool = await client.load_tool("execute_sql")
        raw = await tool(sql=sql)
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


# Swappable for tests: tests replace this with a fake executor.
_executor: SqlExecutor = toolbox_execute_sql
_cache = QueryCache()


async def run_query(
    metrics: list[str],
    start_date: str,
    end_date: str,
    dimensions: list[str] | None = None,
    filters: dict[str, list[str]] | None = None,
    order_by: str | None = None,
    order: str = "desc",
    limit: int = 100,
) -> dict[str, Any]:
    """Runs a sales query defined by semantic-layer names. Never pass SQL.

    Args:
        metrics: Metric names, e.g. ["units_sold"] or ["revenue", "units_sold"].
        start_date: First day included, YYYY-MM-DD.
        end_date: Last day included, YYYY-MM-DD.
        dimensions: Dimensions to group by, e.g. ["product"]. Empty for a single total.
        filters: Dimension to allowed values, e.g. {"district": ["Kadıköy"]}.
        order_by: A selected metric or dimension to sort by. Defaults to the first metric.
        order: "desc" or "asc".
        limit: Maximum rows to return (1-1000).

    Returns:
        {"status": "ok", "rows": [...], "row_count": n, "sql": "...", "cache": "hit"|"miss"} on success,
        {"status": "error", "message": "..."} if the request is invalid (fix and retry).
    """
    try:
        spec = QuerySpec(
            metrics=metrics,
            dimensions=dimensions or [],
            filters=filters or {},
            start_date=start_date,
            end_date=end_date,
            order_by=order_by,
            order=order,
            limit=limit,
        )
        sql = compile_spec(spec)
    except (ValidationError, SemanticError) as e:
        # Returned to the model, not raised: the agent reads the message and corrects its call.
        return {"status": "error", "message": str(e)}

    rows = _cache.get(sql)
    cache_status = "hit"
    if rows is None:
        cache_status = "miss"
        logger.info("run_query executing:\n%s", sql)
        rows = await _executor(sql)
        _cache.set(sql, rows)
    else:
        logger.info("run_query cache hit (hits=%d misses=%d)", _cache.hits, _cache.misses)
    row_list = rows if isinstance(rows, list) else []
    return {"status": "ok", "rows": rows, "row_count": len(row_list), "sql": sql, "cache": cache_status}


def _instruction(_: ReadonlyContext) -> str:
    # A callable instruction so "today" is evaluated per request, not at import time.
    return prompts.SALES_ANALYTICS_INSTRUCTION.format(
        today=date.today().isoformat(),
        data_start=DATA_START.isoformat(),
        data_end=DATA_END.isoformat(),
        catalog=default_model().describe(),
    )


sales_analytics_agent = LlmAgent(
    name="sales_analytics",
    model=config.MODEL_ID,
    description=prompts.SALES_ANALYTICS_DESCRIPTION,
    instruction=_instruction,
    tools=[run_query],
    after_tool_callback=record_tool_evidence,
    after_model_callback=output_guardrail,
)