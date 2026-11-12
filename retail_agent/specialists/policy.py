"""Policy Q&A agent: answers procedure questions from policy documents, with citations."""

from __future__ import annotations

from typing import Any

from google.adk.agents import LlmAgent

from retail_agent import config, prompts, search
from retail_agent.guardrails import output_guardrail, record_tool_evidence


async def search_policies(query: str) -> dict[str, Any]:
    """Searches the company's store policy documents.

    Args:
        query: Short keyword query in Turkish, e.g. "fişsiz iade" or "kasa farkı tolerans".

    Returns:
        {"status": "ok", "sources": [{"source_id", "title", "uri", "passages"}]},
        {"status": "no_results", "sources": []}, or {"status": "error", "message"}.
    """
    return await search.search(config.POLICY_SERVING_CONFIG, query)


policy_qa_agent = LlmAgent(
    name="policy_qa",
    model=config.MODEL_ID,
    description=prompts.POLICY_QA_DESCRIPTION,
    instruction=prompts.POLICY_QA_INSTRUCTION,
    tools=[search_policies],
    after_tool_callback=record_tool_evidence,
    after_model_callback=output_guardrail,
)