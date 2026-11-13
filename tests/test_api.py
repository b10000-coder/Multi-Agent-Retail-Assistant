"""API tests with a fake runner: SSE event order, PII masking before storage, sessions."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.genai import types

from app import main


def _event(text: str | None = None, call: str | None = None, partial: bool = False) -> Event:
    part = types.Part(function_call=types.FunctionCall(name=call, args={})) if call else types.Part(text=text)
    return Event(author="orchestrator", content=types.Content(role="model", parts=[part]), partial=partial)


class FakeRunner:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def run_async(self, *, user_id, session_id, new_message, run_config):
        self.messages.append(new_message.parts[0].text)
        yield _event(call="sales_analytics")
        yield _event("Gofret ", partial=True)
        yield _event("1.281 adet", partial=True)
        yield _event("Gofret 1.281 adet")


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    runner = FakeRunner()
    monkeypatch.setattr(main, "runner", runner)
    return runner


def test_chat_streams_events_in_order(fake: FakeRunner) -> None:
    with TestClient(main.app) as client:
        resp = client.post("/chat", json={"message": "Geçen ay Kadıköy'de en çok satan ürün?"})
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    assert [e for e, _ in events] == ["session", "status", "token", "token", "final"]
    assert events[1][1] == {"agent": "sales_analytics", "message": "Satış verisine bakılıyor..."}
    assert events[-1][1]["text"] == "Gofret 1.281 adet"


def test_pii_masked_before_reaching_the_runner(fake: FakeRunner) -> None:
    with TestClient(main.app) as client:
        client.post("/chat", json={"message": "TC'si 10000000146 olan müşteri iade istiyor"})
    assert fake.messages == ["TC'si [TCKN] olan müşteri iade istiyor"]


def test_session_is_reused(fake: FakeRunner) -> None:
    with TestClient(main.app) as client:
        first = parse_sse(client.post("/chat", json={"message": "a"}).text)[0][1]["session_id"]
        second = parse_sse(client.post("/chat", json={"message": "b", "session_id": first}).text)[0][1]["session_id"]
        unknown = parse_sse(client.post("/chat", json={"message": "c", "session_id": "nope"}).text)[0][1]["session_id"]
    assert second == first
    assert unknown != "nope"  # unknown session ids get a fresh session


def test_empty_message_rejected(fake: FakeRunner) -> None:
    with TestClient(main.app) as client:
        assert client.post("/chat", json={"message": ""}).status_code == 422


def test_healthz_and_index() -> None:
    with TestClient(main.app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert "Mağaza Operasyon Asistanı" in client.get("/").text
