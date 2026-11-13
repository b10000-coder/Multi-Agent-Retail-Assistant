"""Unit tests for the semantic layer compiler (no GCP needed).

The integration test at the bottom runs the compiled SQL against BigQuery and is
skipped unless RUN_BQ_TESTS=1 is set.
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from pydantic import ValidationError

from retail_agent.semantic.compiler import (
    QuerySpec,
    SemanticError,
    compile_spec,
    default_model,
)

DATASET = "retail-agent-bk.retail"

# The demo question: "Geçen ay Kadıköy'de en çok satan 5 ürün ne?"
KADIKOY_TOP5 = QuerySpec(
    metrics=["units_sold"],
    dimensions=["product"],
    filters={"district": ["Kadıköy"]},
    start_date=date(2026, 10, 1),
    end_date=date(2026, 10, 31),
    limit=5,
)


def compile_(spec: QuerySpec) -> str:
    return compile_spec(spec, dataset=DATASET)


def test_kadikoy_top5_golden_sql() -> None:
    expected = """\
SELECT
  p.product_name AS product,
  SUM(s.quantity) AS units_sold
FROM `retail-agent-bk.retail.sales` AS s
JOIN `retail-agent-bk.retail.products` AS p ON s.product_id = p.product_id
JOIN `retail-agent-bk.retail.stores` AS st ON s.store_id = st.store_id
WHERE s.sale_date BETWEEN DATE '2026-10-01' AND DATE '2026-10-31'
  AND st.district IN ('Kadıköy')
GROUP BY 1
ORDER BY units_sold DESC
LIMIT 5"""
    assert compile_(KADIKOY_TOP5) == expected


def test_same_spec_same_sql() -> None:
    assert compile_(KADIKOY_TOP5) == compile_(KADIKOY_TOP5.model_copy())


def test_only_needed_joins() -> None:
    # category lives in products; no store dimension or filter -> no stores join
    sql = compile_(
        QuerySpec(
            metrics=["revenue"],
            dimensions=["category"],
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
        )
    )
    assert "products" in sql
    assert "stores" not in sql


def test_no_dimensions_no_group_by() -> None:
    sql = compile_(
        QuerySpec(
            metrics=["revenue", "units_sold"],
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
        )
    )
    assert "GROUP BY" not in sql
    assert "JOIN" not in sql


def test_filters_are_sorted_for_stable_sql() -> None:
    a = QuerySpec(
        metrics=["revenue"],
        filters={"region": ["Ege"], "category": ["Temizlik"]},
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
    )
    b = QuerySpec(
        metrics=["revenue"],
        filters={"category": ["Temizlik"], "region": ["Ege"]},
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
    )
    assert compile_(a) == compile_(b)


def test_injection_in_filter_value_is_escaped() -> None:
    spec = KADIKOY_TOP5.model_copy(update={"filters": {"district": ["Kadıköy') OR 1=1 --"]}})
    sql = compile_(spec)
    assert "IN ('Kadıköy\\') OR 1=1 --')" in sql


@pytest.mark.parametrize(
    "update, message",
    [
        ({"metrics": ["profit"]}, "unknown metric"),
        ({"dimensions": ["customer"]}, "unknown dimension"),
        ({"filters": {"customer": ["x"]}}, "unknown filter dimension"),
        ({"filters": {"month": ["2026-10"]}}, "cannot be filtered"),
        ({"order_by": "revenue"}, "order_by"),
    ],
)
def test_invalid_specs_raise(update: dict, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        compile_(KADIKOY_TOP5.model_copy(update=update))


def test_end_before_start_rejected() -> None:
    with pytest.raises(ValidationError, match="before start_date"):
        QuerySpec(metrics=["revenue"], start_date=date(2026, 10, 31), end_date=date(2026, 10, 1))


def test_limit_capped() -> None:
    with pytest.raises(ValidationError):
        QuerySpec(metrics=["revenue"], start_date=date(2026, 10, 1), end_date=date(2026, 10, 31), limit=10_000)


def test_bad_dataset_rejected() -> None:
    with pytest.raises(SemanticError, match="invalid dataset"):
        compile_spec(KADIKOY_TOP5, dataset="x; DROP TABLE sales")


def test_describe_lists_every_metric_and_dimension() -> None:
    model = default_model()
    text = model.describe()
    for name in [*model.metrics, *model.dimensions]:
        assert f"- {name}:" in text


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_BQ_TESTS") != "1", reason="set RUN_BQ_TESTS=1 to hit BigQuery")
def test_kadikoy_top5_against_bigquery() -> None:
    from google.cloud import bigquery

    rows = list(bigquery.Client().query(compile_spec(KADIKOY_TOP5)).result())
    got = [(r["product"], r["units_sold"]) for r in rows]
    # Known result from the seeded synthetic dataset (scripts/generate_data.py, size=small)
    assert got == [
        ("Gofret Tekli", 1281),
        ("Çamaşır Suyu 3 L", 680),
        ("Un 2 kg", 407),
        ("Yayla Toz Şeker 1 kg", 402),
        ("Siyah Zeytin 500 g", 383),
    ]


def test_revenue_share_uses_window_over_groups() -> None:
    sql = compile_(
        QuerySpec(
            metrics=["revenue", "revenue_share"],
            dimensions=["region"],
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
        )
    )
    assert (
        "SAFE_DIVIDE(SUM(s.quantity * s.unit_price), SUM(SUM(s.quantity * s.unit_price)) OVER ()) AS revenue_share"
        in sql
    )
    assert "GROUP BY 1" in sql


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_BQ_TESTS") != "1", reason="set RUN_BQ_TESTS=1 to hit BigQuery")
def test_region_revenue_shares_sum_to_one() -> None:
    from google.cloud import bigquery

    spec = QuerySpec(
        metrics=["revenue", "revenue_share"],
        dimensions=["region"],
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
    )
    rows = list(bigquery.Client().query(compile_spec(spec)).result())
    assert len(rows) == 5
    assert abs(sum(r["revenue_share"] for r in rows) - 1.0) < 1e-9
