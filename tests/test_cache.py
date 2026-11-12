"""Unit tests for the query cache (TTL and LRU) with a fake clock."""

from __future__ import annotations

from retail_agent.semantic.cache import QueryCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_hit_and_miss_counters() -> None:
    cache = QueryCache()
    assert cache.get("SELECT 1") is None
    cache.set("SELECT 1", [{"x": 1}])
    assert cache.get("SELECT 1") == [{"x": 1}]
    assert (cache.hits, cache.misses) == (1, 1)


def test_entries_expire_after_ttl() -> None:
    clock = FakeClock()
    cache = QueryCache(ttl_seconds=60, clock=clock)
    cache.set("q", "rows")
    clock.now = 59
    assert cache.get("q") == "rows"
    clock.now = 61
    assert cache.get("q") is None
    assert len(cache) == 0


def test_lru_evicts_least_recently_used() -> None:
    cache = QueryCache(max_entries=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # a is now most recent
    cache.set("c", 3)  # evicts b
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3
