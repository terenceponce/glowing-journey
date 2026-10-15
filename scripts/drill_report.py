"""Read a drill's traces back from Langfuse and print what they show (docs/failure-drills.md).

Run it with `make drill-report DRILL=<name> [COMPARE=<run>]`, which pipes this file into `python -` in the chatbot
pod (it has the Langfuse keys, and show_trace.py in its working directory):
  python - <run name> [<reference run name>]
The run is a dataset run of the experiment runner, e.g. `drill-qdrant-down`; the reference is an earlier run over
the same items, e.g. `baseline`. Everything comes from Langfuse's API (the experiments API for the run's traces, the
observations API for each trace), nothing from the cluster, which is the point of the drills: diagnose from traces.

For each trace of the run: the chat span's level and status, and every observation with its latency, level and
status message, plus the fields that matter: the retrieval collection, scores and sources, the LLM attempts and
errors, the prompt version and the token usage. Then one summary row per run, with the reference's rows limited to
the items the run asked. The cost columns are an estimate at glm-5.3-flash prices ($0.15 per 1M input, $0.50 per
1M output tokens, no cached-input discount): the model really used is the free glm-4.7-flash, so Langfuse shows $0.
"""

import json
import statistics
import sys
from datetime import timedelta

from langfuse import get_client

import show_trace

DATASET = "rag-eval"
PRICE_IN, PRICE_OUT = 0.15 / 1e6, 0.50 / 1e6  # glm-5.3-flash, USD per token

langfuse = get_client()


def run_traces(run: str) -> list[dict]:
    """The traces of an experiment (run): item id, trace id, level."""
    dataset = langfuse.get_dataset(DATASET)
    since = min(i.created_at for i in dataset.items) - timedelta(days=1)
    experiment_id = langfuse._create_observation_id(seed=f"{dataset.id}:{run}")
    out, cursor = [], None
    while True:
        resp = langfuse.api.experiments.list_items(
            from_start_time=since, experiment_id=experiment_id, limit=100, **({"cursor": cursor} if cursor else {}),
        )
        out += [{"item": x.experiment_item_id, "trace": x.trace_id, "level": str(x.level).split(".")[-1]} for x in resp.data]
        cursor = resp.meta.cursor if resp.meta else None
        if not cursor or not resp.data:
            return sorted(out, key=lambda t: t["item"])


def parsed(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def facts(trace: dict) -> dict:
    """The numbers one trace contributes to the summary."""
    obs = trace["obs"]
    by = lambda name: [o for o in obs if o["name"] == name]  # noqa: E731
    chat, embed, retrieve, generate = (by(n) for n in ("chat", "embed-query", "retrieve", "generate"))
    gens = [o for o in by("chat-completion") if o.get("usageDetails")]
    usage = [g["usageDetails"] for g in gens]
    out_tokens = sum(u.get("output", 0) for u in usage)
    in_tokens = sum(u.get("total", 0) - u.get("output", 0) for u in usage)
    meta = (retrieve[0].get("metadata") or {}) if retrieve else {}
    scores = meta.get("scores") or []
    return {
        "failed": bool(chat) and chat[0].get("level") == "ERROR",
        "chat_s": show_trace.seconds(chat[0]) if chat else None,
        "embed_s": show_trace.seconds(embed[0]) if embed else None,
        "retrieve_s": show_trace.seconds(retrieve[0]) if retrieve else None,
        "generate_s": show_trace.seconds(generate[0]) if generate else None,
        "top1": scores[0] if scores else None,
        "chunks": len(scores),
        "in": in_tokens, "out": out_tokens,
        "attempts": ((generate[0].get("metadata") or {}).get("attempts") if generate else None),
        "collection": (parsed(retrieve[0].get("input")) or {}).get("collection") if retrieve else None,
        "prompt": sorted({f"v{g.get('promptVersion')}" for g in gens if g.get("promptName")}),
    }


def show(trace: dict) -> None:
    obs = sorted(trace["obs"], key=lambda o: o["startTime"])
    chat = next((o for o in obs if o["name"] == "chat"), {})
    print(f"\ntrace {trace['trace']}  item {trace['item']}  level {trace['level']}")
    print(f"  Q: {(parsed(chat.get('input')) or {}).get('message')}")
    for o in obs:
        line = f"  - {o['type']:<10} {o['name']:<16} {show_trace.seconds(o) or 0:6.2f}s"
        if o.get("level") not in (None, "DEFAULT"):
            line += f"  {o['level']}: {(o.get('statusMessage') or '')[:110]}"
        meta = o.get("metadata") or {}
        if o["name"] == "retrieve":
            inp = parsed(o.get("input")) or {}
            line += f"  collection={inp.get('collection')} top_k={inp.get('top_k')} hits={meta.get('hits')} scores={meta.get('scores')}"
            line += f"\n      sources={[s.removeprefix('content/en/docs/concepts/') for s in meta.get('sources', [])]}"
        elif o["name"] == "generate":
            line += f"  attempts={meta.get('attempts')} errors={meta.get('errors')} prompt={meta.get('prompt_name')} v{meta.get('prompt_version')}"
        elif o["name"] == "chat-completion":
            u = o.get("usageDetails") or {}
            line += f"  attempt={meta.get('attempt')} usage in/out={u.get('total', 0) - u.get('output', 0)}/{u.get('output')}" if u else f"  attempt={meta.get('attempt')}"
            if o.get("promptName"):
                line += f" prompt={o['promptName']} v{o.get('promptVersion')}"
        print(line)
    if chat.get("output"):
        out = parsed(chat["output"]) or {}
        print(f"  A: {' '.join(str(out.get('answer', '')).split())[:300]}")


def median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 3) if values else None


def summarize(label: str, traces: list[dict]) -> None:
    f = [facts(t) for t in traces]
    ok = [x for x in f if not x["failed"]]
    n_in, n_out = sum(x["in"] for x in f), sum(x["out"] for x in f)
    cost = n_in * PRICE_IN + n_out * PRICE_OUT
    print(f"\n{label}: {len(f)} traces, {len(ok)} answered, {len(f) - len(ok)} failed (chat level ERROR)")
    print(f"  chat latency median {median(x['chat_s'] for x in f)}s  embed-query median {median(x['embed_s'] for x in f)}s  "
          f"retrieve median {median(x['retrieve_s'] for x in f)}s  generate median {median(x['generate_s'] for x in f)}s")
    print(f"  top-1 score mean {mean(x['top1'] for x in f)}  chunks per turn {mean(x['chunks'] for x in f)}  "
          f"collections {sorted({x['collection'] for x in f if x['collection']})}  prompt {sorted({p for x in f for p in x['prompt']})}")
    print(f"  LLM attempts: mean {mean(x['attempts'] for x in f)}, turns needing a retry {sum((x['attempts'] or 1) > 1 for x in f)}")
    print(f"  tokens: input mean {mean(x['in'] for x in ok)} output mean {mean(x['out'] for x in ok)} per answered turn")
    if ok:
        per = sum(x["in"] * PRICE_IN + x["out"] * PRICE_OUT for x in ok) / len(ok)
        print(f"  estimated cost at glm-5.3-flash: ${per:.5f} per answered turn, ${per * 1000:.2f} per 1000 turns "
              f"(the batch: ${cost:.5f}; Langfuse shows $0 on glm-4.7-flash)")


def load(run: str, items: set[str] | None = None) -> list[dict]:
    traces = [t for t in run_traces(run) if items is None or t["item"] in items]
    for t in traces:
        t["obs"] = show_trace.fetch(t["trace"])
    return traces


def main() -> None:
    run = sys.argv[1]
    reference = sys.argv[2] if len(sys.argv) > 2 else None
    traces = load(run)
    if not traces:
        sys.exit(f"no traces for run {run!r} in dataset {DATASET}")
    for t in traces:
        show(t)
    summarize(f"run {run}", traces)
    if reference:
        ref = load(reference, {t["item"] for t in traces})
        # Failed attempts of the reference don't belong in its picture of normal.
        ref = [t for t in ref if t["level"] != "ERROR"]
        summarize(f"reference {reference} (same {len({t['item'] for t in ref})} items)", ref)


if __name__ == "__main__":
    main()
