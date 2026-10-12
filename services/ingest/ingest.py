"""Ingest a markdown corpus into a Qdrant collection.

Walks CORPUS_DIR for *.md files, chunks them, embeds each chunk through the
embedder service and upserts the vectors into COLLECTION.

Config (environment variables):
  CORPUS_DIR          root of the corpus inside the image (required)
  CORPUS_PREFIX       prefix for the `source` payload, i.e. the corpus path in its repo (default "")
  CORPUS_REF          corpus git ref, stored in the payload for provenance (default "")
  EMBEDDER_URL        base URL of the embedding service (required)
  QDRANT_URL          Qdrant REST URL (required)
  QDRANT_API_KEY      optional
  COLLECTION          collection name (default "docs")
  CHUNK_SIZE          max characters per chunk (default 1200)
  CHUNK_OVERLAP       characters carried over between consecutive chunks (default 200)
  EMBED_BATCH_SIZE    chunks per /embed call and per upsert (default 64)
  RECREATE_COLLECTION "true" drops and recreates the collection first (default "true")
"""

import os
import time
import uuid
from pathlib import Path

import httpx
from qdrant_client import QdrantClient, models

from chunking import chunk_document

ID_NAMESPACE = uuid.UUID("6f1c2a0e-5d8b-4c1e-9a43-0b7d2e8f3c51")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing required environment variable {name}")
    return value


def embed(client: httpx.Client, texts: list[str]) -> tuple[int, list[list[float]]]:
    resp = client.post("/embed", json={"texts": texts, "kind": "passage"})
    resp.raise_for_status()
    data = resp.json()
    return data["dim"], data["vectors"]


def main() -> None:
    corpus_dir = Path(env("CORPUS_DIR"))
    prefix = env("CORPUS_PREFIX", "")
    corpus_ref = env("CORPUS_REF", "")
    collection = env("COLLECTION", "docs")
    size = int(env("CHUNK_SIZE", "1200"))
    overlap = int(env("CHUNK_OVERLAP", "200"))
    batch_size = int(env("EMBED_BATCH_SIZE", "64"))
    recreate = env("RECREATE_COLLECTION", "true").lower() == "true"
    if not 0 <= overlap < size:
        raise SystemExit("CHUNK_OVERLAP must be >= 0 and smaller than CHUNK_SIZE")

    started = time.monotonic()
    points: list[dict] = []
    files = sorted(corpus_dir.rglob("*.md"))
    for path in files:
        source = str(Path(prefix) / path.relative_to(corpus_dir))
        for i, chunk in enumerate(chunk_document(path.read_text(encoding="utf-8"), size, overlap)):
            points.append({
                "id": str(uuid.uuid5(ID_NAMESPACE, f"{source}#{i}")),
                # Title and section heading give short chunks some context in the vector.
                "embed_text": "\n".join(filter(None, [chunk.title, chunk.section, chunk.text])),
                "payload": {
                    "source": source,
                    "title": chunk.title,
                    "section": chunk.section,
                    "chunk_index": i,
                    "text": chunk.text,
                    "corpus_ref": corpus_ref,
                },
            })
    print(f"{len(files)} files -> {len(points)} chunks (size={size}, overlap={overlap})", flush=True)
    if not points:
        raise SystemExit("no chunks produced")

    embedder = httpx.Client(base_url=env("EMBEDDER_URL"), timeout=120, transport=httpx.HTTPTransport(retries=5))
    qdrant = QdrantClient(url=env("QDRANT_URL"), api_key=os.environ.get("QDRANT_API_KEY") or None, timeout=60)

    for start in range(0, len(points), batch_size):
        batch = points[start:start + batch_size]
        dim, vectors = embed(embedder, [p["embed_text"] for p in batch])
        if start == 0:
            exists = qdrant.collection_exists(collection)
            if exists and recreate:
                qdrant.delete_collection(collection)
                exists = False
            if not exists:
                qdrant.create_collection(
                    collection,
                    vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
                )
                print(f"created collection {collection!r} (dim={dim}, cosine)", flush=True)
        qdrant.upsert(
            collection,
            points=[models.PointStruct(id=p["id"], vector=v, payload=p["payload"]) for p, v in zip(batch, vectors)],
            wait=True,
        )
        done = start + len(batch)
        if done % (batch_size * 10) == 0 or done == len(points):
            print(f"  upserted {done}/{len(points)}", flush=True)

    count = qdrant.count(collection, exact=True).count
    print(f"done: collection {collection!r} has {count} points ({time.monotonic() - started:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
