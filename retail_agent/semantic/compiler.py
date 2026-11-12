"""Semantic layer: compile a structured QuerySpec into BigQuery SQL.

The LLM never writes SQL. It fills a QuerySpec (which metrics, which dimensions,
which filters, which dates); this module turns that into SQL using the
definitions in metrics.yaml.

Why:
- No hallucinated SQL: only metrics and dimensions defined in metrics.yaml exist.
- Consistent numbers: "revenue" means the same formula in every answer.
- Testable: the same spec always produces the same SQL, so it is covered by pytest.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

METRICS_PATH = Path(__file__).with_name("metrics.yaml")
MAX_LIMIT = 1000
_DATASET_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]\.[A-Za-z0-9_]+$")


class SemanticError(ValueError):
    """The spec refers to something the semantic layer does not define."""


class QuerySpec(BaseModel):
    """What the analytics agent fills in instead of writing SQL."""

    metrics: list[str] = Field(min_length=1, description="Metric names from the semantic layer.")
    dimensions: list[str] = Field(default_factory=list, description="Dimensions to group by.")
    filters: dict[str, list[str]] = Field(
        default_factory=dict,
        description='Dimension -> allowed values, e.g. {"district": ["Kadıköy"]}.',
    )
    start_date: date = Field(description="First day included (inclusive).")
    end_date: date = Field(description="Last day included (inclusive).")
    order_by: str | None = Field(default=None, description="A selected metric or dimension. Default: first metric.")
    order: Literal["desc", "asc"] = "desc"
    limit: int = Field(default=100, ge=1, le=MAX_LIMIT)

    @model_validator(mode="after")
    def _check(self) -> QuerySpec:
        if self.end_date < self.start_date:
            raise ValueError(f"end_date {self.end_date} is before start_date {self.start_date}")
        for dim, values in self.filters.items():
            if not values:
                raise ValueError(f"filter on '{dim}' has no values")
        return self


@dataclass(frozen=True)
class SemanticModel:
    base_table: str
    base_alias: str
    time_column: str
    joins: dict[str, dict[str, Any]]
    metrics: dict[str, dict[str, Any]]
    dimensions: dict[str, dict[str, Any]]

    @classmethod
    def from_yaml(cls, path: Path = METRICS_PATH) -> SemanticModel:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        base = raw["base_table"]
        return cls(
            base_table=base["name"],
            base_alias=base["alias"],
            time_column=base["time_column"],
            joins=raw.get("joins", {}),
            metrics=raw["metrics"],
            dimensions=raw["dimensions"],
        )

    def describe(self) -> str:
        """Compact catalog of metrics and dimensions, for the agent's instruction."""
        lines = ["Metrics:"]
        for name, m in self.metrics.items():
            lines.append(f"- {name}: {m['description']} (synonyms: {', '.join(m.get('synonyms', []))})")
        lines.append("Dimensions:")
        for name, d in self.dimensions.items():
            note = "" if d.get("filterable", True) else " [group by only, not filterable]"
            lines.append(f"- {name}: {d['description']}{note} (synonyms: {', '.join(d.get('synonyms', []))})")
        return "\n".join(lines)


@lru_cache(maxsize=1)
def default_model() -> SemanticModel:
    return SemanticModel.from_yaml()


def default_dataset() -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "retail-agent-bk")
    dataset = os.environ.get("BQ_DATASET", "retail")
    return f"{project}.{dataset}"


def _string_literal(value: str) -> str:
    """BigQuery string literal with backslash and quote escaped.

    Filter values come from the LLM, so they are treated as untrusted:
    "Kadıköy' OR 1=1 --" becomes a harmless string that matches nothing.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{escaped}'"


def compile_spec(
    spec: QuerySpec,
    model: SemanticModel | None = None,
    dataset: str | None = None,
) -> str:
    model = model or default_model()
    dataset = dataset or default_dataset()
    if not _DATASET_RE.match(dataset):
        raise SemanticError(f"invalid dataset reference: {dataset!r}")

    # ---- validate names against the semantic model ----
    for m in spec.metrics:
        if m not in model.metrics:
            raise SemanticError(f"unknown metric '{m}'. Available: {sorted(model.metrics)}")
    for d in spec.dimensions:
        if d not in model.dimensions:
            raise SemanticError(f"unknown dimension '{d}'. Available: {sorted(model.dimensions)}")
    for d in spec.filters:
        if d not in model.dimensions:
            raise SemanticError(f"unknown filter dimension '{d}'. Available: {sorted(model.dimensions)}")
        if not model.dimensions[d].get("filterable", True):
            raise SemanticError(f"dimension '{d}' cannot be filtered; use start_date/end_date")
    if len(set(spec.metrics)) != len(spec.metrics) or len(set(spec.dimensions)) != len(spec.dimensions):
        raise SemanticError("metrics and dimensions must not repeat")

    selected = [*spec.dimensions, *spec.metrics]
    order_by = spec.order_by or spec.metrics[0]
    if order_by not in selected:
        raise SemanticError(f"order_by '{order_by}' must be one of the selected fields: {selected}")

    # ---- SELECT ----
    select = [f"{model.dimensions[d]['sql']} AS {d}" for d in spec.dimensions]
    select += [f"{model.metrics[m]['sql']} AS {m}" for m in spec.metrics]

    # ---- FROM / JOIN: only the joins that are actually needed, in YAML order ----
    needed = {model.dimensions[d].get("join") for d in [*spec.dimensions, *spec.filters]} - {None}
    joins = [
        f"JOIN `{dataset}.{name}` AS {j['alias']} ON {j['condition']}"
        for name, j in model.joins.items()
        if name in needed
    ]

    # ---- WHERE: date range always, then filters in a stable order ----
    where = [f"{model.time_column} BETWEEN DATE '{spec.start_date.isoformat()}' AND DATE '{spec.end_date.isoformat()}'"]
    for d in sorted(spec.filters):
        values = ", ".join(_string_literal(v) for v in spec.filters[d])
        where.append(f"{model.dimensions[d]['sql']} IN ({values})")

    parts = [
        "SELECT\n  " + ",\n  ".join(select),
        f"FROM `{dataset}.{model.base_table}` AS {model.base_alias}",
        *joins,
        "WHERE " + "\n  AND ".join(where),
    ]
    if spec.dimensions:
        parts.append("GROUP BY " + ", ".join(str(i) for i in range(1, len(spec.dimensions) + 1)))
    parts.append(f"ORDER BY {order_by} {spec.order.upper()}")
    parts.append(f"LIMIT {spec.limit}")
    return "\n".join(parts)