"""Check that the chatbot is back on its defaults after an experiment (docs/experiments.md).

Run with `make experiment-check`, which pipes this file into `python -` in the chatbot pod (so it sees the pod's
real env) with the expected values as arguments:
  python - <top_k> <prompt_label> <model> <ingested chunking> <expected chunking>
It checks the running chatbot's TOP_K, LANGFUSE_PROMPT_LABEL and LLM_MODEL, that the `production` label of the
rag-chat prompt points at a version with the content of prompts.py (the original), and the chunking `docs` was
last ingested with; it prints the `docs` point count. Exits 1 if anything differs. `make experiment-check` then
asks one question with `make chatbot-test` to show the chatbot still answers.
"""

import os
import sys

from langfuse import get_client
from qdrant_client import QdrantClient

import prompts

top_k, label, model, ingested, chunking = sys.argv[1:6]
problems = []


def check(name: str, actual, expected) -> None:
    ok = str(actual) == str(expected)
    print(f"  {'ok ' if ok else 'BAD'} {name}: {actual}" + ("" if ok else f" (expected {expected})"))
    if not ok:
        problems.append(name)


print("chatbot defaults")
check("TOP_K", os.environ.get("TOP_K"), top_k)
check("LANGFUSE_PROMPT_LABEL", os.environ.get("LANGFUSE_PROMPT_LABEL"), label)
check("LLM_MODEL", os.environ.get("LLM_MODEL"), model)
check("LLM_ALLOWED_MODELS", os.environ.get("LLM_ALLOWED_MODELS", os.environ.get("LLM_MODEL")), model)
check("docs chunking (ingest-config)", ingested, chunking)

prompt = get_client().get_prompt(prompts.PROMPT_NAME, label="production", type="chat", cache_ttl_seconds=0)
stored = [{"role": m["role"], "content": m["content"]} for m in prompt.prompt]
check(f"{prompts.PROMPT_NAME} production (version {prompt.version}) matches prompts.py", stored == prompts.RAG_CHAT_PROMPT, True)

qdrant = QdrantClient(url=os.environ["QDRANT_URL"])
collection = os.environ.get("COLLECTION", "docs")
print(f"  --  {collection} points: {qdrant.count(collection, exact=True).count} (3048 with chunking 1200/200)")
sys.exit(1 if problems else 0)
