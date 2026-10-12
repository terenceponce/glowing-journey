"""Embedding service: POST /embed turns texts into vectors with fastembed.

Config (environment variables):
  EMBED_MODEL     fastembed model name (baked into the image at build time)
  MODEL_CACHE_DIR directory holding the downloaded model
  EMBED_THREADS   ONNX Runtime threads (default: let ONNX Runtime decide)
  MAX_BATCH       max texts per request (default 256)
  ONNX_BATCH_SIZE texts per ONNX Runtime call; bounds peak memory (default 16)
  QUERY_PREFIX    prepended to each text when "kind" is "query" (default: none)
"""

import os
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastembed import TextEmbedding
from pydantic import BaseModel

MODEL_NAME = os.environ["EMBED_MODEL"]
CACHE_DIR = os.environ["MODEL_CACHE_DIR"]
THREADS = int(os.environ["EMBED_THREADS"]) if os.environ.get("EMBED_THREADS") else None
MAX_BATCH = int(os.environ.get("MAX_BATCH", "256"))
# fastembed's default runs up to 256 texts as one ONNX batch padded to 512 tokens, which
# needs well over 1 GiB. Smaller batches keep memory flat for the same throughput on CPU.
ONNX_BATCH_SIZE = int(os.environ.get("ONNX_BATCH_SIZE", "16"))
QUERY_PREFIX = os.environ.get("QUERY_PREFIX", "")

# Loads from CACHE_DIR; the image sets HF_HUB_OFFLINE=1 so this never touches the network.
model = TextEmbedding(model_name=MODEL_NAME, cache_dir=CACHE_DIR, threads=THREADS)
DIM = len(next(iter(model.embed(["warmup"]))))

app = FastAPI(title="embedder")


class EmbedRequest(BaseModel):
    texts: list[str]
    # "query" applies QUERY_PREFIX (BGE's retrieval instruction); "passage" embeds as-is.
    kind: Literal["passage", "query"] = "passage"


class EmbedResponse(BaseModel):
    model: str
    dim: int
    vectors: list[list[float]]


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "model": MODEL_NAME, "dim": DIM}


# Plain `def`: FastAPI runs it in a worker thread, so the CPU-bound embedding doesn't block the event loop.
@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest) -> EmbedResponse:
    if len(req.texts) > MAX_BATCH:
        raise HTTPException(status_code=413, detail=f"at most {MAX_BATCH} texts per request")
    texts = [QUERY_PREFIX + t for t in req.texts] if req.kind == "query" else req.texts
    vectors = [v.tolist() for v in model.embed(texts, batch_size=ONNX_BATCH_SIZE)] if texts else []
    return EmbedResponse(model=MODEL_NAME, dim=DIM, vectors=vectors)
