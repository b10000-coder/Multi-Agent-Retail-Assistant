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