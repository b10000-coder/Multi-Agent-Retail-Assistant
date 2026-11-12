"""Tests for the shared search helper with a fake Vertex AI Search backend."""

from __future__ import annotations

import pytest

from retail_agent import search
from retail_agent.specialists import catalog, policy

FAKE_RESULTS = [
    {
        "title": "İade ve Değişim Politikası",
        "link": "gs://bucket/policies/iade-ve-degisim.html",
        "extractive_segments": [
            {"content": "4.1. Fiş veya fatura ibraz edilemeyen iade talepleri kabul edilmez."},
            {
                "content": "4.3. Madde 4.2 kapsamındaki işlemlerde iade tutarı 500 TL&#39;yi aşıyorsa bölge müdürü onayı gerekir."
            },
        ],
    },
    {
        "title": "Müşteri Şikayet Yönetimi",
        "link": "gs://bucket/policies/musteri-sikayet.html",
        "snippets": [{"snippet": "Mağazada çözülemeyen <b>şikayetler</b> için çağrı merkezi..."}],
    },
    {"title": "Boş sonuç", "link": "gs://bucket/x.html"},
]


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def fake(serving_config: str, query: str) -> list[dict]:
        calls.append((serving_config, query))
        return FAKE_RESULTS

    monkeypatch.setattr(search, "_raw_search", fake)
    return calls


async def test_sources_are_numbered_and_cleaned(fake_backend: list) -> None:
    result = await search.search("projects/p/engines/policies-app/servingConfigs/default_search", "fişsiz iade")
    assert result["status"] == "ok"
    sources = result["sources"]
    assert [s["source_id"] for s in sources] == [1, 2]  # result without passages is dropped
    assert sources[0]["title"] == "İade ve Değişim Politikası"
    assert "500 TL'yi" in sources[0]["passages"][1]  # HTML entity unescaped
    assert sources[1]["passages"] == ["Mağazada çözülemeyen şikayetler için çağrı merkezi..."]  # tags stripped


async def test_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    async def empty(serving_config: str, query: str) -> list[dict]:
        return []

    monkeypatch.setattr(search, "_raw_search", empty)
    assert await search.search("cfg", "uzay mekiği") == {"status": "no_results", "sources": []}


async def test_missing_config_returns_error(fake_backend: list) -> None:
    result = await search.search("", "fişsiz iade")
    assert result["status"] == "error"
    assert fake_backend == []


async def test_each_agent_searches_its_own_corpus(fake_backend: list, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("retail_agent.config.POLICY_SERVING_CONFIG", "policies-cfg")
    monkeypatch.setattr("retail_agent.config.CATALOG_SERVING_CONFIG", "catalog-cfg")
    await policy.search_policies("fişsiz iade")
    await catalog.search_catalog("bebek")
    assert fake_backend == [("policies-cfg", "fişsiz iade"), ("catalog-cfg", "bebek")]


def test_catalog_instruction_has_today_and_weekday() -> None:
    text = catalog._instruction(None)  # type: ignore[arg-type]
    assert "Bugünün tarihi" in text
    assert any(day in text for day in ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"])
