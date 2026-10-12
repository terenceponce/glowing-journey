"""Embed a question via the embedder service and print the top-k chunks from Qdrant.

Usage: python query.py "how do pods share storage?"
Config: EMBEDDER_URL, QDRANT_URL, QDRANT_API_KEY (optional), COLLECTION (default "docs"), TOP_K (default 5).
"""

import os
import sys

import httpx
from qdrant_client import QdrantClient


def main() -> None:
    question = " ".join(sys.argv[1:]).strip()
    if not question:
        raise SystemExit('usage: query.py "<question>"')
    collection = os.environ.get("COLLECTION", "docs")
    top_k = int(os.environ.get("TOP_K", "5"))

    resp = httpx.post(f"{os.environ['EMBEDDER_URL']}/embed", json={"texts": [question], "kind": "query"}, timeout=60)
    resp.raise_for_status()
    vector = resp.json()["vectors"][0]

    qdrant = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.environ.get("QDRANT_API_KEY") or None)
    hits = qdrant.query_points(collection, query=vector, limit=top_k, with_payload=True).points
    print(f"Q: {question}\n")
    for rank, hit in enumerate(hits, 1):
        p = hit.payload
        snippet = " ".join(p["text"].split())[:200]
        print(f"{rank}. score={hit.score:.3f}  {p['source']}  [{p['title']} > {p['section'] or '-'}] #{p['chunk_index']}")
        print(f"   {snippet}...\n")


if __name__ == "__main__":
    main()
