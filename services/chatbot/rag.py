"""Retrieval and generation for the RAG chatbot, kept as separate steps so each can be traced on its own.

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
  LLM_MAX_RETRIES  SDK retries on 429/5xx/timeouts, with backoff (default 3)
  ZAI_API_KEY      API key, from the zai-api Secret
"""

import os
from dataclasses import dataclass

import httpx
from openai import OpenAI
from qdrant_client import QdrantClient

EMBEDDER_URL = os.environ["EMBEDDER_URL"]
COLLECTION = os.environ.get("COLLECTION", "docs")
TOP_K = int(os.environ.get("TOP_K", "5"))
LLM_MODEL = os.environ["LLM_MODEL"]
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1024"))
LLM_THINKING = os.environ.get("LLM_THINKING", "disabled")

embedder = httpx.Client(base_url=EMBEDDER_URL, timeout=30)
qdrant = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.environ.get("QDRANT_API_KEY") or None)
llm = OpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["ZAI_API_KEY"],
    timeout=float(os.environ.get("LLM_TIMEOUT", "60")),
    # Z.AI's free models return 429 "service may be temporarily overloaded" now and then.
    max_retries=int(os.environ.get("LLM_MAX_RETRIES", "3")),
)

SYSTEM_PROMPT = """You answer questions about Kubernetes using only the numbered context passages \
from the Kubernetes documentation that come with each question.
- Base every statement on the passages and cite them inline as [1], [2], ...
- If the passages don't contain the answer, say you don't know from the documentation. Don't guess.
- Be concise. Use short paragraphs or bullet lists, and code blocks for YAML or commands."""


@dataclass
class Chunk:
    n: int  # citation number, 1-based
    score: float
    source: str
    title: str
    section: str
    chunk_index: int
    text: str


def retrieve(question: str, top_k: int = TOP_K) -> list[Chunk]:
    """Embed the question (kind=query) and return the top-k chunks from Qdrant."""
    resp = embedder.post("/embed", json={"texts": [question], "kind": "query"})
    resp.raise_for_status()
    vector = resp.json()["vectors"][0]
    hits = qdrant.query_points(COLLECTION, query=vector, limit=top_k, with_payload=True).points
    return [
        Chunk(
            n=i,
            score=hit.score,
            source=hit.payload["source"],
            title=hit.payload["title"],
            section=hit.payload["section"],
            chunk_index=hit.payload["chunk_index"],
            text=hit.payload["text"],
        )
        for i, hit in enumerate(hits, 1)
    ]


def build_messages(question: str, history: list[dict], chunks: list[Chunk]) -> list[dict]:
    """System prompt, earlier turns (questions and answers only), then the context and the new question."""
    context = "\n\n".join(
        f"[{c.n}] {c.title}" + (f" > {c.section}" if c.section else "") + f" ({c.source})\n{c.text}" for c in chunks
    )
    user = f"Context passages:\n\n{context}\n\nQuestion: {question}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": user}]


def generate(messages: list[dict]) -> tuple[str, dict]:
    """Call the LLM and return the answer text and token usage."""
    completion = llm.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
        # Z.AI extension: GLM models can think before answering; off by default for speed.
        extra_body={"thinking": {"type": LLM_THINKING}},
    )
    answer = (completion.choices[0].message.content or "").strip()
    usage = completion.usage.model_dump(exclude_none=True) if completion.usage else {}
    return answer, usage
