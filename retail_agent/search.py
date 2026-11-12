"""Vertex AI Search retrieval shared by the RAG agents (policy_qa, product_catalog).

Returns numbered sources with title, URI and extracted passages, so the agent can cite
"[1]" and the output guardrail can later check that every citation exists.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

MAX_RESULTS = 5
_TAG_RE = re.compile(r"<[^>]+>")

RawSearch = Callable[[str, str], Awaitable[list[dict[str, Any]]]]


def _clean(text: str) -> str:
    """Snippets come back with <b> highlight tags and HTML entities."""
    return " ".join(html.unescape(_TAG_RE.sub("", text)).split())


async def vertex_search(serving_config: str, query: str) -> list[dict[str, Any]]:
    """Call Vertex AI Search and return each result's derived_struct_data as a plain dict."""
    from google.cloud import discoveryengine_v1 as de
    from google.protobuf.json_format import MessageToDict

    spec = de.SearchRequest.ContentSearchSpec
    request = de.SearchRequest(
        serving_config=serving_config,
        query=query,
        page_size=MAX_RESULTS,
        content_search_spec=spec(
            snippet_spec=spec.SnippetSpec(return_snippet=True),
            # Enterprise edition: verbatim passages from the document, used as citation evidence
            extractive_content_spec=spec.ExtractiveContentSpec(max_extractive_segment_count=3),
        ),
        query_expansion_spec=de.SearchRequest.QueryExpansionSpec(
            condition=de.SearchRequest.QueryExpansionSpec.Condition.AUTO
        ),
        spell_correction_spec=de.SearchRequest.SpellCorrectionSpec(mode=de.SearchRequest.SpellCorrectionSpec.Mode.AUTO),
    )
    client = de.SearchServiceAsyncClient()
    pager = await client.search(request=request)
    results: list[dict[str, Any]] = []
    async for r in pager:
        results.append(MessageToDict(r.document._pb.derived_struct_data))
        if len(results) >= MAX_RESULTS:
            break
    return results


# Swappable for tests.
_raw_search: RawSearch = vertex_search


async def search(serving_config: str | None, query: str) -> dict[str, Any]:
    """Search one corpus and return numbered, citation-ready sources."""
    if not serving_config:
        return {"status": "error", "message": "search is not configured (missing serving config in .env)"}
    if not query.strip():
        return {"status": "error", "message": "empty query"}

    logger.info("search %s: %r", serving_config.rsplit("/engines/", 1)[-1], query)
    raw = await _raw_search(serving_config, query)

    sources = []
    for item in raw:
        passages = [_clean(s.get("content", "")) for s in item.get("extractive_segments", [])]
        if not passages:  # fall back to snippets if no extractive segments came back
            passages = [_clean(s.get("snippet", "")) for s in item.get("snippets", [])]
        passages = [p for p in passages if p]
        if not passages:
            continue
        sources.append(
            {
                "source_id": len(sources) + 1,
                "title": item.get("title", "Untitled"),
                "uri": item.get("link", ""),
                "passages": passages,
            }
        )

    if not sources:
        return {"status": "no_results", "sources": []}
    return {"status": "ok", "sources": sources}
