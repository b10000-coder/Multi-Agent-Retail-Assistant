"""Tests for the run_query tool with a fake SQL executor (no Toolbox, no BigQuery, no LLM)."""

from __future__ import annotations

import pytest

from retail_agent.specialists import analytics


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sqls: list[str] = []

    async def fake_executor(sql: str) -> list[dict]:
        sqls.append(sql)
        return [{"product": "Gofret Tekli", "units_sold": 1281}]

    monkeypatch.setattr(analytics, "_executor", fake_executor)
    analytics._cache.clear()
    return sqls


async def test_valid_call_compiles_and_executes(captured: list[str]) -> None:
    result = await analytics.run_query(
        metrics=["units_sold"],
        dimensions=["product"],
        filters={"district": ["Kadıköy"]},
        start_date="2026-10-01",
        end_date="2026-10-31",
        limit=5,
    )
    assert result["status"] == "ok"
    assert result["row_count"] == 1
    assert len(captured) == 1
    assert "st.district IN ('Kadıköy')" in captured[0]
    assert result["sql"] == captured[0]


async def test_unknown_metric_returns_error_without_executing(captured: list[str]) -> None:
    result = await analytics.run_query(metrics=["profit"], start_date="2026-10-01", end_date="2026-10-31")
    assert result["status"] == "error"
    assert "unknown metric" in result["message"]
    assert captured == []


async def test_bad_date_returns_error(captured: list[str]) -> None:
    result = await analytics.run_query(metrics=["revenue"], start_date="geçen ay", end_date="2026-10-31")
    assert result["status"] == "error"
    assert captured == []


def test_instruction_contains_catalog_and_today() -> None:
    text = analytics._instruction(None)  # type: ignore[arg-type]
    assert "units_sold" in text
    assert "district" in text
    assert "Bugünün tarihi" in text


KADIKOY = {
    "metrics": ["units_sold"],
    "dimensions": ["product"],
    "start_date": "2026-10-01",
    "end_date": "2026-10-31",
    "limit": 5,
}


async def test_same_spec_hits_cache(captured: list[str]) -> None:
    first = await analytics.run_query(**KADIKOY, filters={"district": ["Kadıköy"]})
    second = await analytics.run_query(**KADIKOY, filters={"district": ["Kadıköy"]})
    assert (first["cache"], second["cache"]) == ("miss", "hit")
    assert second["rows"] == first["rows"]
    assert len(captured) == 1  # BigQuery queried once


async def test_different_filter_misses_cache(captured: list[str]) -> None:
    await analytics.run_query(**KADIKOY, filters={"district": ["Kadıköy"]})
    other = await analytics.run_query(**KADIKOY, filters={"district": ["Ataşehir"]})
    assert other["cache"] == "miss"
    assert len(captured) == 2  # the Kadıköy answer is never served for Ataşehir


async def test_filter_order_does_not_matter(captured: list[str]) -> None:
    a = {"district": ["Kadıköy"], "category": ["Temizlik"]}
    b = {"category": ["Temizlik"], "district": ["Kadıköy"]}
    await analytics.run_query(**KADIKOY, filters=a)
    second = await analytics.run_query(**KADIKOY, filters=b)
    assert second["cache"] == "hit"