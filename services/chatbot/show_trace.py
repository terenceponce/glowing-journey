"""Print a Langfuse trace as a tree: observation type, name, latency, model, tokens, cost and prompt version.

Usage (inside the chatbot pod, which has the Langfuse keys):
  python show_trace.py <trace-id>                    observation tree, then the trace's scores
  python show_trace.py --scores <trace-id> [VALUE [COMMENT]]
      only the scores; with VALUE (and COMMENT), wait until user-feedback has them
Run it with `make chatbot-trace TRACE=<trace-id>` (or `make chatbot-feedback`). Langfuse v4 has no
/api/public/traces or /api/public/scores, so this reads /api/public/v2/observations?traceId=... and
/api/public/v3/scores?traceId=... .
"""

import json
import os
import sys
import time
from datetime import datetime

import httpx


def client() -> httpx.Client:
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    return httpx.Client(base_url=os.environ["LANGFUSE_BASE_URL"], auth=auth, timeout=30)


def fetch(trace_id: str) -> list[dict]:
    with client() as api:
        # Ingestion is asynchronous: give it a few seconds, until the observation count stops growing.
        observations: list[dict] = []
        for _ in range(10):
            resp = api.get(
                "/api/public/v2/observations",
                params={"traceId": trace_id, "limit": 100, "fields": "core,basic,usage,io,metadata,model,prompt"},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            if data and len(data) == len(observations):
                return data
            observations = data
            time.sleep(2)
        return observations


def fetch_scores(trace_id: str, expect: float | None = None, comment: str | None = None) -> list[dict]:
    """The trace's scores. With `expect`, poll (ingestion is asynchronous) until user-feedback has that value
    and comment."""
    wanted = lambda s: s["name"] == "user-feedback" and s.get("value") == expect and (s.get("comment") or None) == comment  # noqa: E731
    with client() as api:
        for _ in range(15 if expect is not None else 1):
            # Core fields only by default; `details` adds comment and metadata, `subject` what the score is on.
            resp = api.get("/api/public/v3/scores", params={"traceId": trace_id, "limit": 100, "fields": "details,subject"})
            resp.raise_for_status()
            scores = resp.json()["data"]
            if expect is None or any(wanted(s) for s in scores):
                return scores
            time.sleep(2)
        sys.exit(f"user-feedback={expect:g} not visible on trace {trace_id} after 30 s; got {[describe_score(s) for s in scores]}")


def describe_score(s: dict) -> str:
    subject = s.get("subject") or {}
    parts = [f"{s['name']}={s['value']}", s["dataType"], f"on {subject.get('kind', '?')}", f"id={s['id']}"]
    if s.get("comment"):
        parts.append(f"comment={json.dumps(s['comment'])}")
    if s.get("metadata"):
        parts.append(f"metadata={json.dumps(s['metadata'], separators=(',', ':'))}")
    parts.append(f"source={s.get('source')}  timestamp={s.get('timestamp')}")
    return "  ".join(p for p in parts if p)


def print_scores(scores: list[dict]) -> None:
    print(f"  scores ({len(scores)}):")
    for s in scores:
        print("  - " + describe_score(s))


def seconds(o: dict) -> float | None:
    if not o.get("endTime"):
        return None
    parse = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))  # noqa: E731
    return (parse(o["endTime"]) - parse(o["startTime"])).total_seconds()


def describe(o: dict) -> str:
    parts = [f"{o['type']:<10} {o['name']}"]
    if (s := seconds(o)) is not None:
        parts.append(f"{s:.2f}s")
    if o.get("level") and o["level"] != "DEFAULT":
        parts.append(f"level={o['level']}" + (f" ({o['statusMessage']})" if o.get("statusMessage") else ""))
    if o.get("model") or o.get("providedModelName"):
        parts.append(f"model={o.get('model') or o.get('providedModelName')}")
    if o.get("usageDetails"):
        parts.append(f"usage={json.dumps(o['usageDetails'], separators=(',', ':'))}")
    if o.get("costDetails"):
        parts.append(f"cost={json.dumps(o['costDetails'], separators=(',', ':'))}")
    elif o.get("totalCost") is not None:
        parts.append(f"cost={o['totalCost']}")
    if o.get("promptName"):
        parts.append(f"prompt={o['promptName']} v{o.get('promptVersion')}")
    meta = o.get("metadata") or {}
    for key in ("attempt", "attempts", "errors", "chunk_ids", "scores"):
        if key in meta:
            parts.append(f"{key}={json.dumps(meta[key], separators=(',', ':'))}")
    return "  ".join(parts)


def main() -> None:
    if sys.argv[1] == "--scores":
        trace_id = sys.argv[2]
        expect = float(sys.argv[3]) if len(sys.argv) > 3 else None
        comment = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
        print(f"trace {trace_id}")
        print_scores(fetch_scores(trace_id, expect, comment))
        return
    trace_id = sys.argv[1]
    observations = fetch(trace_id)
    if not observations:
        sys.exit(f"no observations for trace {trace_id} (yet)")
    ids = {o["id"] for o in observations}
    children: dict[str | None, list[dict]] = {}
    for o in sorted(observations, key=lambda o: o["startTime"]):
        parent = o.get("parentObservationId")
        children.setdefault(parent if parent in ids else None, []).append(o)

    root = children.get(None, [{}])[0]
    print(f"trace {trace_id}")
    for key in ("userId", "sessionId", "environment", "version"):
        if root.get(key):
            print(f"  {key}: {root[key]}")
    if root.get("input"):
        print(f"  input: {json.dumps(root['input'])[:200]}")

    def walk(parent: str | None, depth: int) -> None:
        for o in children.get(parent, []):
            print("  " * depth + "- " + describe(o))
            if o["name"] == "retrieve" and o.get("input"):
                print("  " * (depth + 1) + f"query: {json.dumps(o['input'])}")
            walk(o["id"], depth + 1)

    walk(None, 1)
    print_scores(fetch_scores(trace_id))
    print(f"Open: http://localhost:3000/project/rag-chatbot/traces/{trace_id}")


if __name__ == "__main__":
    main()
