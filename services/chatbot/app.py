"""RAG chatbot: POST /chat answers from the Qdrant corpus with Z.AI GLM; GET / serves a chat page.

Retrieval and generation live in rag.py (see its docstring for config). Here:
  MAX_HISTORY_TURNS  earlier question/answer pairs kept per session and sent to the LLM (default 6)
  MAX_SESSIONS       sessions kept in memory; the oldest is dropped beyond this (default 1000)

Every POST /chat is one Langfuse trace (docs/observability.md): a root `chat` span carrying user_id, session_id
and tags, with embed-query, retrieve and generate inside it. Tracing is asynchronous and best-effort: if Langfuse
is down, the chat still works and the SDK drops what it can't send. Spans are flushed on shutdown.

POST /chat also takes optional per-request overrides, used by the experiment runner (scripts/run_experiment.py,
docs/experiments.md): top_k, prompt_label or prompt_version, model (only one of LLM_ALLOWED_MODELS), a fixed
history instead of the session's, an LLM retry budget, and an experiment context. Langfuse v4 links a trace to an
experiment (dataset run) item through experiment attributes on every observation, so the chatbot sets them on its
own spans; the `chat` span is the item's root observation. Without these fields nothing changes.

POST /feedback records a thumbs up/down on a turn as the Langfuse score `user-feedback` (1 = up, 0 = down) on its
trace. The score ID is derived from the trace ID, so a changed vote overwrites the score instead of adding one.
"""

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import httpx
import openai
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from langfuse import propagate_attributes
# Not public API: the SDK's own experiment runner uses these to put experiment attributes on observations, and
# propagate_attributes() has no experiment parameter (langfuse 4.17).
from langfuse._client.attributes import LangfuseOtelSpanAttributes, _serialize
from langfuse._client.propagation import PropagatedExperimentAttributes, _propagate_attributes
from pydantic import BaseModel, Field, model_validator

import rag

MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "6"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "1000"))
STATIC_DIR = Path(__file__).parent / "static"
APP_VERSION = version("chatbot")
ENVIRONMENT = os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "default")
FEEDBACK_SCORE = "user-feedback"

def trace_tags(model: str, experiment: str | None = None) -> list[str]:
    return [f"env:{ENVIRONMENT}", f"version:{APP_VERSION}", f"model:{model}", *([f"experiment:{experiment}"] if experiment else [])]

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

class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class Experiment(BaseModel):
    """Links the turn to a Langfuse experiment (dataset run) item. Ids come from the experiment runner."""
    id: str = Field(min_length=1, max_length=128)  # the dataset run id
    name: str = Field(min_length=1, max_length=128, pattern=r"^[\w.:-]+$")  # the run name, also a trace tag
    description: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, str] | None = None
    dataset_id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    item_metadata: dict[str, str] | None = None
    expected_output: Any = None


class Retry(BaseModel):
    max_retries: int = Field(ge=0, le=12)
    backoff_base: float = Field(default=rag.LLM_BACKOFF_BASE, gt=0, le=60)
    backoff_max: float = Field(default=rag.LLM_BACKOFF_MAX, gt=0, le=300)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    # Optional overrides for experiments. Left out, the chatbot uses its config and the session history.
    top_k: int | None = Field(default=None, ge=1, le=20)
    prompt_label: str | None = Field(default=None, min_length=1, max_length=64)
    prompt_version: int | None = Field(default=None, ge=1)
    model: str | None = Field(default=None, min_length=1, max_length=64)
    # Replaces the session history for this turn, and the turn isn't added to the session.
    history: list[HistoryMessage] | None = Field(default=None, max_length=2 * MAX_HISTORY_TURNS)
    retry: Retry | None = None  # LLM retry budget for this turn instead of LLM_MAX_RETRIES
    experiment: Experiment | None = None

    @model_validator(mode="after")
    def check_overrides(self) -> "ChatRequest":
        if self.prompt_label and self.prompt_version:
            raise ValueError("set prompt_label or prompt_version, not both")
        if self.model and self.model not in rag.LLM_ALLOWED_MODELS:
            raise ValueError(f"model must be one of {sorted(rag.LLM_ALLOWED_MODELS)}")
        return self


class Source(BaseModel):
    n: int
    score: float
    source: str
    title: str
    section: str
    chunk_index: int
    snippet: str


class PromptInfo(BaseModel):
    name: str
    version: int | None  # None: the built-in fallback was used
    fallback: bool


class ChatResponse(BaseModel):
    message_id: str  # for attaching feedback (thumbs up/down) to this answer later
    trace_id: str | None  # Langfuse trace of this turn
    session_id: str
    answer: str
    model: str
    sources: list[Source]
    usage: dict
    top_k: int
    prompt: PromptInfo


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
    history = [m.model_dump() for m in req.history] if req.history is not None else get_history(req.session_id)
    overrides = req.model_dump(include={"top_k", "prompt_label", "prompt_version", "model", "retry"}, exclude_none=True)
    if req.history is not None:
        overrides["history"] = "fixed"
    if req.experiment:
        overrides["experiment"] = req.experiment.name
    experiment = req.experiment.name if req.experiment else None
    with ExitStack() as stack:
        root = stack.enter_context(rag.langfuse.start_as_current_observation(
            as_type="span", name="chat",
            input={"message": question, "history_turns": len(history) // 2},
            metadata={"message_id": message_id, **({"overrides": overrides} if overrides else {})},
        ))
        stack.enter_context(propagate_attributes(
            user_id=req.user_id, session_id=req.session_id, tags=trace_tags(req.model or rag.LLM_MODEL, experiment),
            version=APP_VERSION, trace_name="chat",
        ))
        if req.experiment:
            stack.enter_context(experiment_attributes(req.experiment, root))
        try:
            response = answer(req, message_id, question, history)
        except HTTPException as exc:
            root.update(level="ERROR", status_message=exc.detail)
            raise
        root.update(output={"answer": response.answer, "sources": [s.source for s in response.sources]})
        response.trace_id = rag.langfuse.get_current_trace_id()
        return response


def experiment_attributes(exp: Experiment, root):
    """Put the experiment attributes on the root span and every observation under it, like the SDK's experiment
    runner does (Langfuse.run_experiment), with the `chat` span as the item's root observation."""
    root._otel_span.set_attributes({
        k: v for k, v in {
            LangfuseOtelSpanAttributes.EXPERIMENT_DESCRIPTION: exp.description,
            LangfuseOtelSpanAttributes.EXPERIMENT_ITEM_EXPECTED_OUTPUT: _serialize(exp.expected_output),
        }.items() if v is not None
    })
    return _propagate_attributes(experiment=PropagatedExperimentAttributes(
        experiment_id=exp.id, experiment_name=exp.name, experiment_metadata=exp.metadata,
        experiment_dataset_id=exp.dataset_id, experiment_item_id=exp.item_id,
        experiment_item_metadata=exp.item_metadata, experiment_item_root_observation_id=root.id,
    ))


def answer(req: ChatRequest, message_id: str, question: str, history: list[dict]) -> ChatResponse:
    started = time.monotonic()
    top_k = req.top_k or rag.TOP_K
    model = req.model or rag.LLM_MODEL
    try:
        chunks = rag.retrieve(question, top_k)
    except Exception as exc:  # embedder or Qdrant down
        log.exception("retrieval failed")
        raise HTTPException(status_code=502, detail=f"retrieval failed: {type(exc).__name__}") from exc

    try:
        prompt = rag.get_prompt(question, chunks, req.prompt_label, req.prompt_version)
    except Exception as exc:  # only for a requested label/version: no such prompt, or Langfuse unreachable
        log.error("prompt fetch failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"prompt unavailable: {type(exc).__name__}") from exc
    messages = rag.build_messages(prompt, history)
    try:
        retry = req.retry or Retry(max_retries=rag.LLM_MAX_RETRIES)
        text, usage = rag.generate(messages, prompt, model, retry.max_retries, retry.backoff_base, retry.backoff_max)
    except (openai.APIError, httpx.HTTPError) as exc:
        # Log the type and status only: SDK errors carry the response body, never the API key.
        status = getattr(exc, "status_code", None)
        log.error("LLM call failed: %s status=%s", type(exc).__name__, status)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {type(exc).__name__} {status or ''}".strip()) from exc

    if req.history is None:
        save_turn(req.session_id, question, text)
    log.info(
        "chat session=%s chunks=%d tokens=%s prompt=%s@%s latency=%.2fs%s",
        req.session_id, len(chunks), usage.get("total_tokens"), prompt.name, prompt.version, time.monotonic() - started,
        f" experiment={req.experiment.name} item={req.experiment.item_id}" if req.experiment else "",
    )
    return ChatResponse(
        message_id=message_id,
        trace_id=None,
        session_id=req.session_id,
        answer=text,
        model=model,
        sources=[
            Source(
                n=c.n, score=round(c.score, 4), source=c.source, title=c.title, section=c.section,
                chunk_index=c.chunk_index, snippet=" ".join(c.text.split())[:240],
            )
            for c in chunks
        ],
        usage=usage,
        top_k=top_k,
        prompt=PromptInfo(name=prompt.name, version=prompt.version, fallback=prompt.client is None),
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
