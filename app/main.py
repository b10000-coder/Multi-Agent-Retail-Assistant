"""HTTP API for the retail assistant: FastAPI + Server-Sent Events.

    GET  /            minimal chat page (demo)
    GET  /healthz     liveness probe for Cloud Run
    POST /chat        {"message", "session_id"?, "user_id"?} -> text/event-stream

SSE events sent by /chat:
    session   {"session_id"}                      first event; reuse it for follow-up questions
    status    {"agent", "message"}                a specialist was called ("Satış verisine bakılıyor...")
    token     {"text"}                            partial answer text, as the model generates it
    final     {"text"}                            the complete answer, after output guardrails
    error     {"message"}

PII is masked here, before the message is written to the session store, so raw
T.C. kimlik / phone / card numbers are never persisted (the agent-level guardrail
additionally masks every model request).

Run locally:  uv run uvicorn app.main:app --reload --port 8080
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "retail_agent" / ".env")  # local dev; on Cloud Run these come from env vars

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from retail_agent.agent import root_agent
from retail_agent.guardrails import mask_pii

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

APP_NAME = "retail_agent"
STATUS_MESSAGES = {
    "sales_analytics": "Satış verisine bakılıyor...",
    "policy_qa": "Politika dokümanlarında aranıyor...",
    "product_catalog": "Aktüel katalogda aranıyor...",
}

# In-memory sessions: fine for one instance. On Cloud Run with several instances, use
# VertexAiSessionService (Agent Engine Sessions) or DatabaseSessionService so a follow-up
# question that lands on another instance still sees the conversation.
session_service = InMemorySessionService()
runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=session_service)

app = FastAPI(title="Multi-Agent Retail Assistant")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    user_id: str = Field(default="demo", max_length=128)


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def chat_events(req: ChatRequest) -> AsyncIterator[str]:
    masked, found = mask_pii(req.message)
    if found:
        logger.info("api: masked %s before storing the message", sorted(set(found)))

    session_id = req.session_id
    if (
        session_id is None
        or await session_service.get_session(app_name=APP_NAME, user_id=req.user_id, session_id=session_id) is None
    ):
        session = await session_service.create_session(app_name=APP_NAME, user_id=req.user_id)
        session_id = session.id
    yield sse("session", {"session_id": session_id})

    final_text = ""
    try:
        async for event in runner.run_async(
            user_id=req.user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=masked)]),
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            # AgentTool runs specialists in their own sessions; here we only see the orchestrator.
            if event.author != "orchestrator" or not event.content:
                continue
            for call in event.get_function_calls():
                yield sse("status", {"agent": call.name, "message": STATUS_MESSAGES.get(call.name, "Çalışıyor...")})
            text = "".join(p.text or "" for p in event.content.parts or [] if p.text and not p.thought)
            if not text:
                continue
            if event.partial:
                yield sse("token", {"text": text})
            elif event.is_final_response():
                final_text = text  # aggregated answer, already checked by the output guardrail
    except Exception as e:
        logger.exception("api: chat failed")
        yield sse("error", {"message": f"{type(e).__name__}: {e}"})
        return
    yield sse("final", {"text": final_text})


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        chat_events(req),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "index.html")
