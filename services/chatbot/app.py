"""RAG chatbot: POST /chat answers from the Qdrant corpus with Z.AI GLM; GET / serves a chat page.

Retrieval and generation live in rag.py (see its docstring for config). Here:
  MAX_HISTORY_TURNS  earlier question/answer pairs kept per session and sent to the LLM (default 6)
  MAX_SESSIONS       sessions kept in memory; the oldest is dropped beyond this (default 1000)
"""

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

import httpx
import openai
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import rag

MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "6"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "1000"))
STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("chatbot")

app = FastAPI(title="chatbot")

# In-memory history: session_id -> list of {"role", "content"} messages. Lost on restart; fine for a lab.
sessions: OrderedDict[str, list[dict]] = OrderedDict()
sessions_lock = threading.Lock()


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
    session_id: str
    answer: str
    model: str
    sources: list[Source]
    usage: dict


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
    started = time.monotonic()
    question = req.message.strip()
    try:
        chunks = rag.retrieve(question)
    except Exception as exc:  # embedder or Qdrant down
        log.exception("retrieval failed")
        raise HTTPException(status_code=502, detail=f"retrieval failed: {type(exc).__name__}") from exc

    messages = rag.build_messages(question, get_history(req.session_id), chunks)
    try:
        answer, usage = rag.generate(messages)
    except (openai.APIError, httpx.HTTPError) as exc:
        # Log the type and status only: SDK errors carry the response body, never the API key.
        status = getattr(exc, "status_code", None)
        log.error("LLM call failed: %s status=%s", type(exc).__name__, status)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {type(exc).__name__} {status or ''}".strip()) from exc

    save_turn(req.session_id, question, answer)
    log.info(
        "chat session=%s chunks=%d tokens=%s latency=%.2fs",
        req.session_id, len(chunks), usage.get("total_tokens"), time.monotonic() - started,
    )
    return ChatResponse(
        message_id=str(uuid.uuid4()),
        session_id=req.session_id,
        answer=answer,
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
