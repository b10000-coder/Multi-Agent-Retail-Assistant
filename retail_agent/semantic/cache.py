"""Query cache keyed on the compiled query, not on the question text.

Why not a semantic (embedding) cache on the user's question?
"Top products in Kadıköy" and "top products in Ataşehir" are near-identical embeddings,
so a similarity cache can silently return Kadıköy's numbers for Ataşehir.

Here the key is the SQL compiled from the QuerySpec. The spec is canonical:
- paraphrases ("en çok satan" / "en çok satılan") produce the same spec -> cache hit,
- a different filter, metric or date range produces different SQL -> cache miss.
Relative dates ("geçen ay") are resolved to absolute dates before compiling, so entries
never leak across periods.

This is an in-process LRU with TTL. In production I'd use Memorystore (Redis) with the
same key, shared across Cloud Run instances, and invalidate on the daily data load.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

DEFAULT_TTL_SECONDS = 24 * 3600  # sales data is loaded daily
DEFAULT_MAX_ENTRIES = 1000


class QueryCache:
    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(sql: str) -> str:
        return hashlib.sha256(sql.encode("utf-8")).hexdigest()

    def get(self, sql: str) -> Any | None:
        k = self.key(sql)
        entry = self._data.get(k)
        if entry is None or self._clock() - entry[0] > self._ttl:
            self._data.pop(k, None)
            self.misses += 1
            return None
        self._data.move_to_end(k)  # LRU: recently used stays
        self.hits += 1
        return entry[1]

    def set(self, sql: str, value: Any) -> None:
        k = self.key(sql)
        self._data[k] = (self._clock(), value)
        self._data.move_to_end(k)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()
        self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._data)
