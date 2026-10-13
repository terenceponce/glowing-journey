"""RAG chatbot: POST /chat answers from the Qdrant corpus with Z.AI GLM; GET / serves a chat page.

Retrieval and generation live in rag.py (see its docstring for config). Here:
  MAX_HISTORY_TURNS  earlier question/answer pairs kept per session and sent to the LLM (default 6)
  MAX_SESSIONS       sessions kept in memory; the oldest is dropped beyond this (default 1000)

Every POST /chat is one Langfuse trace (docs/observability.md): a root `chat` span carrying user_id, session_id
and tags, with embed-query, retrieve and generate inside it. Tracing is asynchronous and best-effort: if Langfuse
is down, the chat still works and the SDK drops what it can't send. Spans are flushed on shutdown.

POST /feedback records a thumbs up/down on a turn as the Langfuse score `user-feedback` (1 = up, 0 = down) on its
trace. The score ID is derived from the trace ID, so a changed vote overwrites the score instead of adding one.
"""

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import httpx
import openai
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from langfuse import propagate_attributes
from pydantic import BaseModel, Field

import rag

MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "6"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "1000"))
STATIC_DIR = Path(__file__).parent / "static"
APP_VERSION = version("chatbot")
ENVIRONMENT = os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "default")
FEEDBACK_SCORE = "user-feedback"
TRACE_TAGS = [f"env:{ENVIRONMENT}", f"version:{APP_VERSION}", f"model:{rag.LLM_MODEL}"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("chatbot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the prompt cache so the first chat doesn't wait for it (falls back to the built-in prompt if it can't).
    prompt = rag.get_prompt("", [])
    log.info("prompt %s version=%s (fallback=%s), app version %s", prompt.name, prompt.version, prompt.client is None, APP_VERSION)
    yield
    # Kubernetes sends SIGTERM; uvicorn runs this before exiting. Send any spans still queued.
    log.info("flushing Langfuse")
    rag.langfuse.shutdown()


# FastAPI has built-in OpenTelemetry that uses the global tracer provider, which the Langfuse SDK sets up.
# Its HTTP spans would become parents of `chat` but are filtered out on export, leaving `chat` with a
# parent that doesn't exist in Langfuse. Turn it off so `chat` is the trace root.
app = FastAPI(title="chatbot", lifespan=lifespan, telemetry={"tracing": False, "metrics": False, "logs": False})

# In-memory history: session_id -> list of {"role", "content"} messages. Lost on restart; fine for a lab.
sessions: OrderedDict[str, list[dict]] = OrderedDict()
sessions_lock = threading.Lock()

# Langfuse public API (read side), for looking up existing feedback scores.
langfuse_api = httpx.Client(
    base_url=os.environ.get("LANGFUSE_BASE_URL", ""),
    auth=(os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")),
    timeout=5,
)

class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)


class Source(BaseModel):
    n: int
    score: float
    source: str
    title: str
    section: str
    chunk_index: int
    snippet: str


class ChatResponse(BaseModel):
    message_id: str  # for attaching feedback (thumbs up/down) to this answer later
    trace_id: str | None  # Langfuse trace of this turn
    session_id: str
    answer: str
    model: str
    sources: list[Source]
    usage: dict


class FeedbackRequest(BaseModel):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")  # from ChatResponse.trace_id
    value: Literal[0, 1]  # 1 = thumbs up, 0 = thumbs down
    comment: str | None = Field(default=None, max_length=1000)
    user_id: str | None = Field(default=None, max_length=128)
    source: str = Field(default="chat-page", max_length=64)  # where the vote came from, e.g. "make"


class FeedbackResponse(BaseModel):
    score_id: str
    trace_id: str
    name: str
    value: int
    comment: str | None


def get_history(session_id: str) -> list[dict]:
    with sessions_lock:
        return list(sessions.get(session_id, []))


def save_turn(session_id: str, question: str, answer: str) -> None:
    with sessions_lock:
        history = sessions.pop(session_id, [])
        history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
        sessions[session_id] = history[-2 * MAX_HISTORY_TURNS :]
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "model": rag.LLM_MODEL, "collection": rag.COLLECTION}


# Plain `def`: FastAPI runs it in a worker thread, so the blocking HTTP calls don't stall the event loop.
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    message_id = str(uuid.uuid4())
    question = req.message.strip()
    history = get_history(req.session_id)
    with rag.langfuse.start_as_current_observation(
        as_type="span", name="chat",
        input={"message": question, "history_turns": len(history) // 2},
        metadata={"message_id": message_id},
    ) as root, propagate_attributes(
        user_id=req.user_id, session_id=req.session_id, tags=TRACE_TAGS, version=APP_VERSION, trace_name="chat",
    ):
        try:
            response = answer(req, message_id, question, history)
        except HTTPException as exc:
            root.update(level="ERROR", status_message=exc.detail)
            raise
        root.update(output={"answer": response.answer, "sources": [s.source for s in response.sources]})
        response.trace_id = rag.langfuse.get_current_trace_id()
        return response


def answer(req: ChatRequest, message_id: str, question: str, history: list[dict]) -> ChatResponse:
    started = time.monotonic()
    try:
        chunks = rag.retrieve(question)
    except Exception as exc:  # embedder or Qdrant down
        log.exception("retrieval failed")
        raise HTTPException(status_code=502, detail=f"retrieval failed: {type(exc).__name__}") from exc

    prompt = rag.get_prompt(question, chunks)
    messages = rag.build_messages(prompt, history)
    try:
        text, usage = rag.generate(messages, prompt)
    except (openai.APIError, httpx.HTTPError) as exc:
        # Log the type and status only: SDK errors carry the response body, never the API key.
        status = getattr(exc, "status_code", None)
        log.error("LLM call failed: %s status=%s", type(exc).__name__, status)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {type(exc).__name__} {status or ''}".strip()) from exc

    save_turn(req.session_id, question, text)
    log.info(
        "chat session=%s chunks=%d tokens=%s prompt=%s@%s latency=%.2fs",
        req.session_id, len(chunks), usage.get("total_tokens"), prompt.name, prompt.version, time.monotonic() - started,
    )
    return ChatResponse(
        message_id=message_id,
        trace_id=None,
        session_id=req.session_id,
        answer=text,
        model=rag.LLM_MODEL,
        sources=[
            Source(
                n=c.n, score=round(c.score, 4), source=c.source, title=c.title, section=c.section,
                chunk_index=c.chunk_index, snippet=" ".join(c.text.split())[:240],
            )
            for c in chunks
        ],
        usage=usage,
    )


def existing_score_timestamp(score_id: str) -> datetime | None:
    """Timestamp of an already ingested score, or None. Langfuse only overwrites a score when the ID, the name and
    the timestamp's date all match, so a changed vote must reuse the first vote's timestamp."""
    try:
        resp = langfuse_api.get("/api/public/v3/scores", params={"id": score_id, "limit": 1})
        resp.raise_for_status()
        data = resp.json()["data"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("score lookup failed: %s", type(exc).__name__)
        return None
    return datetime.fromisoformat(data[0]["timestamp"].replace("Z", "+00:00")) if data else None


@app.post("/feedback", response_model=FeedbackResponse)
def feedback(req: FeedbackRequest) -> FeedbackResponse:
    score_id = f"{FEEDBACK_SCORE}-{req.trace_id}"
    comment = (req.comment or "").strip() or None
    timestamp = existing_score_timestamp(score_id) or datetime.now(timezone.utc)
    # Sent in the background like the spans; the SDK's score queue is flushed on shutdown too.
    # The SDK leaves None fields out of the body and Langfuse then keeps the stored value, so removing a comment
    # needs an explicit empty string.
    rag.langfuse.create_score(
        score_id=score_id, trace_id=req.trace_id, name=FEEDBACK_SCORE, value=req.value, data_type="NUMERIC",
        comment=comment or "", timestamp=timestamp, metadata={"user_id": req.user_id, "source": req.source},
    )
    log.info("feedback trace=%s value=%d comment=%s", req.trace_id, req.value, comment is not None)
    return FeedbackResponse(score_id=score_id, trace_id=req.trace_id, name=FEEDBACK_SCORE, value=req.value, comment=comment)
