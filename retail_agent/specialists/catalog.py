"""Product catalog agent: answers questions about weekly "aktüel" catalogs, with citations."""

from __future__ import annotations

from datetime import date
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext

from retail_agent import config, prompts, search
from retail_agent.guardrails import output_guardrail, record_tool_evidence


async def search_catalog(query: str) -> dict[str, Any]:
    """Searches the weekly aktüel product catalogs.

    Args:
        query: Short keyword query in Turkish, e.g. "bebek arabası" or "airfryer fiyat".

    Returns:
        {"status": "ok", "sources": [{"source_id", "title", "uri", "passages"}]},
        {"status": "no_results", "sources": []}, or {"status": "error", "message"}.
        Each source title contains the catalog date, e.g. "Aktüel Ürünler Kataloğu: 12 Kasım 2026".
    """
    return await search.search(config.CATALOG_SERVING_CONFIG, query)


def _instruction(_: ReadonlyContext) -> str:
    today = date.today()
    return prompts.PRODUCT_CATALOG_INSTRUCTION.format(
        today=today.isoformat(), weekday=prompts.WEEKDAYS_TR[today.weekday()]
    )


product_catalog_agent = LlmAgent(
    name="product_catalog",
    model=config.MODEL_ID,
    description=prompts.PRODUCT_CATALOG_DESCRIPTION,
    instruction=_instruction,
    tools=[search_catalog],
    after_tool_callback=record_tool_evidence,
    after_model_callback=output_guardrail,
)