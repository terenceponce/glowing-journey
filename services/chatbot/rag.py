"""Retrieval and generation for the RAG chatbot. Each step is its own Langfuse span (see docs/observability.md).

Config (environment variables):
  EMBEDDER_URL     embedding service base URL (POST /embed with kind="query")
  QDRANT_URL       Qdrant REST URL
  QDRANT_API_KEY   optional Qdrant API key
  COLLECTION       Qdrant collection (default "docs")
  TOP_K            chunks retrieved per question (default 5)
  LLM_BASE_URL     OpenAI-compatible base URL (Z.AI general API)
  LLM_MODEL        chat model, e.g. glm-4.7-flash
  LLM_TEMPERATURE  sampling temperature (default 0.2)
  LLM_MAX_TOKENS   max completion tokens (default 1024)
  LLM_THINKING     "enabled" or "disabled": GLM thinking mode (default "disabled")
  LLM_TIMEOUT      seconds per LLM request (default 60)
  LLM_MAX_RETRIES  retries on 429/5xx/connection errors/timeouts, with backoff (default 3)
  ZAI_API_KEY      API key, from the zai-api Secret
  LANGFUSE_PROMPT_LABEL      label of the Langfuse prompt to use (default "production")
  LANGFUSE_PROMPT_CACHE_TTL  seconds a fetched prompt is reused before a background refresh (default 60)
  LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL, LANGFUSE_TRACING_ENVIRONMENT: read by the Langfuse SDK
"""

import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass

import httpx
import openai
from langfuse import get_client
from langfuse.openai import OpenAI  # drop-in wrapper: every completion becomes a Langfuse generation
from qdrant_client import QdrantClient

import prompts

EMBEDDER_URL = os.environ["EMBEDDER_URL"]
COLLECTION = os.environ.get("COLLECTION", "docs")
TOP_K = int(os.environ.get("TOP_K", "5"))
LLM_MODEL = os.environ["LLM_MODEL"]
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1024"))
LLM_THINKING = os.environ.get("LLM_THINKING", "disabled")
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))
PROMPT_LABEL = os.environ.get("LANGFUSE_PROMPT_LABEL", "production")
PROMPT_CACHE_TTL = int(os.environ.get("LANGFUSE_PROMPT_CACHE_TTL", "60"))
# While Langfuse is unreachable, use the built-in prompt for this long before trying again,
# so requests don't each wait for a prompt fetch to time out.
PROMPT_FALLBACK_BACKOFF = 30

log = logging.getLogger("chatbot.rag")

langfuse = get_client()
embedder = httpx.Client(base_url=EMBEDDER_URL, timeout=30)
qdrant = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.environ.get("QDRANT_API_KEY") or None)
llm = OpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["ZAI_API_KEY"],
    timeout=float(os.environ.get("LLM_TIMEOUT", "60")),
    # Retries happen in generate() instead of inside the SDK, so each attempt shows up in the trace.
    max_retries=0,
)

# Z.AI's free models return 429 "service may be temporarily overloaded" now and then.
# APITimeoutError is a subclass of APIConnectionError.
RETRYABLE_ERRORS = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)


@dataclass
class Chunk:
    n: int  # citation number, 1-based
    id: str  # Qdrant point ID
    score: float
    source: str
    title: str
    section: str
    chunk_index: int
    text: str


@dataclass
class Prompt:
    messages: list[dict]  # compiled: system + user, history goes in between
    client: object | None  # Langfuse prompt, linked to the generation; None for the built-in fallback
    name: str
    version: int | None  # None for the built-in fallback


_fallback_until = 0.0
_fallback_lock = threading.Lock()


def get_prompt(question: str, chunks: list[Chunk]) -> Prompt:
    """Fetch the RAG prompt from Langfuse (cached for PROMPT_CACHE_TTL) and fill in the context and question.

    Falls back to prompts.RAG_CHAT_PROMPT when Langfuse is unreachable and nothing is cached yet.
    """
    global _fallback_until
    variables = {"context": format_context(chunks), "question": question}
    if time.monotonic() >= _fallback_until:
        try:
            client = langfuse.get_prompt(
                prompts.PROMPT_NAME, label=PROMPT_LABEL, type="chat", fallback=prompts.RAG_CHAT_PROMPT,
                cache_ttl_seconds=PROMPT_CACHE_TTL, max_retries=0, fetch_timeout_seconds=2,
            )
            if not client.is_fallback:
                return Prompt(client.compile(**variables), client, client.name, client.version)
        except Exception as exc:  # SDK not configured
            log.warning("prompt fetch failed: %s", type(exc).__name__)
        with _fallback_lock:
            _fallback_until = time.monotonic() + PROMPT_FALLBACK_BACKOFF
        log.warning("using the built-in prompt for %ss: Langfuse prompt '%s' unavailable", PROMPT_FALLBACK_BACKOFF, prompts.PROMPT_NAME)
    messages = [
        {**m, "content": re.sub(r"\{\{(\w+)\}\}", lambda v: variables[v.group(1)], m["content"])}
        for m in prompts.RAG_CHAT_PROMPT
    ]
    return Prompt(messages, None, prompts.PROMPT_NAME, None)


def embed_query(question: str) -> list[float]:
    """Embed the question with kind=query (the embedder adds the BGE query instruction)."""
    with langfuse.start_as_current_observation(
        as_type="embedding", name="embed-query", input={"text": question, "kind": "query"},
    ) as span:
        resp = embedder.post("/embed", json={"texts": [question], "kind": "query"})
        resp.raise_for_status()
        body = resp.json()
        span.update(model=body["model"], output={"dim": body["dim"]}, metadata={"embedder_url": EMBEDDER_URL})
        return body["vectors"][0]


def retrieve(question: str, top_k: int = TOP_K) -> list[Chunk]:
    """Embed the question and return the top-k chunks from Qdrant. Spans: embed-query, then retrieve."""
    vector = embed_query(question)
    with langfuse.start_as_current_observation(
        as_type="retriever", name="retrieve",
        # The exact text used for retrieval: today only the latest message, not the history.
        input={"query": question, "top_k": top_k, "collection": COLLECTION},
    ) as span:
        hits = qdrant.query_points(COLLECTION, query=vector, limit=top_k, with_payload=True).points
        chunks = [
            Chunk(
                n=i,
                id=str(hit.id),
                score=hit.score,
                source=hit.payload["source"],
                title=hit.payload["title"],
                section=hit.payload["section"],
                chunk_index=hit.payload["chunk_index"],
                text=hit.payload["text"],
            )
            for i, hit in enumerate(hits, 1)
        ]
        span.update(
            output=[
                {"n": c.n, "chunk_id": c.id, "score": round(c.score, 4), "source": c.source,
                 "title": c.title, "section": c.section, "chunk_index": c.chunk_index}
                for c in chunks
            ],
            metadata={
                "top_k": top_k,
                "hits": len(chunks),
                "chunk_ids": [c.id for c in chunks],
                "scores": [round(c.score, 4) for c in chunks],
                "sources": sorted({c.source for c in chunks}),
            },
        )
        return chunks


def format_context(chunks: list[Chunk]) -> str:
    return "\n\n".join(
        f"[{c.n}] {c.title}" + (f" > {c.section}" if c.section else "") + f" ({c.source})\n{c.text}" for c in chunks
    )


def build_messages(prompt: Prompt, history: list[dict]) -> list[dict]:
    """System prompt, earlier turns (questions and answers only), then the context and the new question."""
    system, user = prompt.messages
    return [system, *history, user]


def generate(messages: list[dict], prompt: Prompt) -> tuple[str, dict]:
    """Call the LLM and return the answer text and token usage.

    Runs in a `generate` span. Each attempt is its own generation (from the OpenAI wrapper) with
    metadata.attempt; failed attempts are level ERROR. The span records how many attempts it took.
    """
    prompt_info = {"prompt_name": prompt.name, "prompt_version": prompt.version, "prompt_fallback": prompt.client is None}
    with langfuse.start_as_current_observation(
        as_type="span", name="generate", input={"model": LLM_MODEL, "messages": len(messages)},
    ) as span:
        errors: list[str] = []
        for attempt in range(1, LLM_MAX_RETRIES + 2):
            try:
                completion = llm.chat.completions.create(
                    model=LLM_MODEL,
                    messages=messages,
                    temperature=LLM_TEMPERATURE,
                    max_tokens=LLM_MAX_TOKENS,
                    # Z.AI extension: GLM models can think before answering; off by default for speed.
                    extra_body={"thinking": {"type": LLM_THINKING}},
                    # Langfuse wrapper arguments, not sent to the API:
                    name="chat-completion",
                    metadata={"attempt": attempt, **prompt_info},
                    langfuse_prompt=prompt.client,
                )
                break
            except RETRYABLE_ERRORS as exc:
                errors.append(f"attempt {attempt}: {type(exc).__name__} {getattr(exc, 'status_code', '') or ''}".strip())
                if attempt > LLM_MAX_RETRIES:
                    span.update(
                        level="ERROR", status_message=f"gave up after {attempt} attempts",
                        metadata={"attempts": attempt, "errors": errors, **prompt_info},
                    )
                    raise
                delay = min(2 ** (attempt - 1), 8) * random.uniform(0.75, 1.25)
                log.warning("LLM %s, retrying in %.1fs", errors[-1], delay)
                time.sleep(delay)

        answer = (completion.choices[0].message.content or "").strip()
        usage = completion.usage.model_dump(exclude_none=True) if completion.usage else {}
        span.update(
            output=answer,
            metadata={"attempts": attempt, "errors": errors, **prompt_info},
            **({"level": "WARNING", "status_message": f"succeeded after {attempt} attempts"} if errors else {}),
        )
        return answer, usage
