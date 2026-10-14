"""Experiment runner: run the eval dataset through the chatbot under a named config, link each answer to a Langfuse
dataset run, and score it with the script judge from #8 (docs/experiments.md).

Run it with `make experiment EXPERIMENT_CONFIG=<name>` (a Job using the chatbot image). For each dataset item it:
  1. asks the chatbot (POST /chat) with the config's overrides (top_k, prompt_label, model), for follow-ups the
     item's fixed history, an LLM retry budget, and the experiment context. The chatbot traces the turn as usual
     and puts the experiment attributes on every observation, which is how Langfuse v4 links a trace to an
     experiment (dataset run) item. The run is EXPERIMENT_RUN (default: the config name); its experiment id is
     derived from the dataset id and run name, so a re-run continues the same run.
  2. scores it: `groundedness` and `answer-relevance` with judge.judge() / judge.post_score() (the same prompts,
     forced tool call and backoff as `make judge`), and `retrieval-hit` (1 if an expected source page was
     retrieved, no LLM call; not for off-topic items)

Built for the free model, which often answers 429:
  - one request at a time: items run in sequence, with EXPERIMENT_PAUSE after each chat request (the judge has
    its own JUDGE_PAUSE)
  - the chatbot retries the LLM call up to EXPERIMENT_LLM_MAX_RETRIES times (instead of its default 3), with
    backoff EXPERIMENT_LLM_BACKOFF_BASE * 2^n seconds up to EXPERIMENT_LLM_BACKOFF_MAX, so one item is normally
    one trace
  - a chat request that still fails (502, 5xx, 429, timeout, connection error) is retried up to
    EXPERIMENT_MAX_ATTEMPTS times with exponential backoff and jitter: EXPERIMENT_BACKOFF_BASE * 2^n seconds,
    capped at EXPERIMENT_BACKOFF_MAX, times 0.5-1.5
  - resumable: an item that already has an answered (non-ERROR) item in this experiment (experiments API) isn't
    asked again, and only its missing scores are judged. Re-running the same config and run name retries only
    what failed.
  - an item that fails every attempt is recorded as failed and the run moves on; it never crashes the run
  - the summary has the success, skipped and failure counts, and the run's averages read back from Langfuse

Config is chosen by name from CONFIGS. Chunking can't be switched per request (it's how `docs` was ingested), so
a config names the chunking it expects and the run refuses to start if the ingest-config ConfigMap
(INGEST_CHUNK_SIZE / INGEST_CHUNK_OVERLAP) doesn't match.

Env: EXPERIMENT_CONFIG, EXPERIMENT_RUN, EXPERIMENT_DATASET, EXPERIMENT_ITEMS (comma-separated item ids, default
all), CHATBOT_URL, EXPERIMENT_MAX_ATTEMPTS, EXPERIMENT_BACKOFF_BASE, EXPERIMENT_BACKOFF_MAX, EXPERIMENT_PAUSE,
EXPERIMENT_TIMEOUT, EXPERIMENT_LLM_MAX_RETRIES, EXPERIMENT_LLM_BACKOFF_BASE, EXPERIMENT_LLM_BACKOFF_MAX, EXPERIMENT_ALLOWED_MODELS, INGEST_CHUNK_SIZE, INGEST_CHUNK_OVERLAP, plus everything judge.py
reads (JUDGE_*, ZAI_API_KEY, LANGFUSE_*). Needs show_trace.py from the chatbot image on PYTHONPATH.
"""

import json
import os
import random
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

import httpx

import experiment_dataset
import judge
import judge_prompts
import show_trace

# The free model only. A config naming any other model is refused (the chatbot's LLM_ALLOWED_MODELS refuses it too).
DEFAULTS = {"top_k": 5, "prompt_label": "production", "model": "glm-4.7-flash", "chunking": "1200/200"}
CONFIGS = {
    "baseline": {**DEFAULTS},
    "top-k-3": {**DEFAULTS, "top_k": 3},
    "top-k-8": {**DEFAULTS, "top_k": 8},
    "prompt-concise": {**DEFAULTS, "prompt_label": "concise"},
    "chunks-600": {**DEFAULTS, "chunking": "600/100"},  # needs `make ingest CHUNK_SIZE=600 CHUNK_OVERLAP=100` first
}

CONFIG_NAME = os.environ["EXPERIMENT_CONFIG"]
RUN = os.environ.get("EXPERIMENT_RUN") or CONFIG_NAME
DATASET = os.environ.get("EXPERIMENT_DATASET") or experiment_dataset.DATASET
ONLY_ITEMS = [i.strip() for i in os.environ.get("EXPERIMENT_ITEMS", "").split(",") if i.strip()]
CHATBOT_URL = os.environ.get("CHATBOT_URL", "http://chatbot.app.svc.cluster.local:8000")
MAX_ATTEMPTS = int(os.environ.get("EXPERIMENT_MAX_ATTEMPTS", "3"))
BACKOFF_BASE = float(os.environ.get("EXPERIMENT_BACKOFF_BASE", "30"))
BACKOFF_MAX = float(os.environ.get("EXPERIMENT_BACKOFF_MAX", "180"))
PAUSE = float(os.environ.get("EXPERIMENT_PAUSE", "3"))
TIMEOUT = float(os.environ.get("EXPERIMENT_TIMEOUT", "900"))  # covers the chatbot's whole retry budget
LLM_RETRY = {
    "max_retries": int(os.environ.get("EXPERIMENT_LLM_MAX_RETRIES", "7")),
    "backoff_base": float(os.environ.get("EXPERIMENT_LLM_BACKOFF_BASE", "2")),
    "backoff_max": float(os.environ.get("EXPERIMENT_LLM_BACKOFF_MAX", "60")),
}
ALLOWED_MODELS = {m.strip() for m in os.environ.get("EXPERIMENT_ALLOWED_MODELS", "glm-4.7-flash").split(",") if m.strip()}
HIT_SCORE = "retrieval-hit"
REFUSAL = "don't know from the documentation"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ChatFailed(Exception):
    pass


@dataclass
class Record:
    """One dataset item in this run, read back from Langfuse."""
    item_id: str
    kind: str
    trace_id: str | None = None
    status: str = "failed"  # ok (asked now), skipped (already answered in the run), failed
    error: str = ""
    latency: float | None = None  # the chat span: retrieval + prompt + every LLM attempt and backoff
    gen_latency: float | None = None  # the final, successful chat-completion
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    attempts: int | None = None  # LLM attempts inside the chatbot
    hit: float | None = None
    refused: bool | None = None
    scores: dict = field(default_factory=dict)


def check_config(config: dict) -> None:
    if config["model"] not in ALLOWED_MODELS or judge.MODEL not in ALLOWED_MODELS:
        sys.exit(f"refusing to run: models must be in {sorted(ALLOWED_MODELS)} (free model only)")
    ingested = f"{os.environ.get('INGEST_CHUNK_SIZE', '?')}/{os.environ.get('INGEST_CHUNK_OVERLAP', '?')}"
    if ingested != config["chunking"]:
        sys.exit(f"refusing to run: config {CONFIG_NAME} expects chunking {config['chunking']}, but `docs` was last "
                 f"ingested with {ingested} (ingest-config). Run `make ingest CHUNK_SIZE=.. CHUNK_OVERLAP=..` first.")


def ask(api: httpx.Client, item, config: dict, experiment: dict, stats: Counter) -> dict:
    """POST /chat with the config's overrides and the experiment context, retrying with backoff. Returns the
    response body."""
    body = {
        "session_id": f"experiment-{RUN}-{item.id}",
        "user_id": "experiment",
        "message": item.input["question"],
        "history": item.input.get("history", []),  # fixed; empty for standalone questions
        "top_k": config["top_k"],
        "prompt_label": config["prompt_label"],
        "model": config["model"],
        "retry": LLM_RETRY,
        "experiment": {
            **experiment,
            "item_id": item.id,
            "item_metadata": {k: str(v) for k, v in (item.metadata or {}).items()},
            "expected_output": item.expected_output,
        },
    }
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        stats["chat_requests"] += 1
        try:
            resp = api.post("/chat", json=body)
            if resp.status_code in RETRYABLE_STATUS:
                last = f"HTTP {resp.status_code} {resp.json().get('detail', '') if resp.headers.get('content-type', '').startswith('application/json') else ''}".strip()
            elif resp.status_code != 200:
                raise ChatFailed(f"HTTP {resp.status_code} {resp.text[:200]}")  # a config error: don't retry
            else:
                data = resp.json()
                if data["prompt"]["fallback"]:
                    last = "the chatbot used its built-in fallback prompt (Langfuse unreachable)"
                elif (data["top_k"], data["model"]) != (config["top_k"], config["model"]):
                    raise ChatFailed(f"chatbot ignored the overrides: top_k={data['top_k']} model={data['model']}")
                else:
                    data["chat_attempts"] = attempt
                    return data
        except httpx.TransportError as exc:  # includes timeouts
            last = type(exc).__name__
        finally:
            time.sleep(PAUSE)
        stats[f"chat_error {last.split(':')[0][:60]}"] += 1
        if attempt == MAX_ATTEMPTS:
            break
        delay = min(BACKOFF_BASE * 2 ** (attempt - 1), BACKOFF_MAX) * random.uniform(0.5, 1.5)
        print(f"    chat attempt {attempt}: {last}, retrying in {delay:.0f}s", flush=True)
        time.sleep(delay)
    raise ChatFailed(f"{MAX_ATTEMPTS} attempts, last: {last}")


def answered_items(experiment_id: str, since) -> tuple[dict[str, str], int]:
    """dataset item id -> trace id of the items already answered in this experiment, and how many items failed.

    Langfuse v4 has no dataset-run-items read API (404 in events_only mode); the experiments API reads the
    experiment attributes on the traces instead. A failed chat request leaves a trace at level ERROR."""
    answered, errors, cursor = {}, 0, None
    while True:
        resp = judge.langfuse.api.experiments.list_items(
            from_start_time=since, experiment_id=experiment_id, limit=100, **({"cursor": cursor} if cursor else {}),
        )
        for x in resp.data:
            if str(x.level).endswith("ERROR"):
                errors += 1
            else:
                answered.setdefault(x.experiment_item_id, x.trace_id)
        cursor = resp.meta.cursor if resp.meta else None
        if not cursor or not resp.data:
            return answered, errors


def observations(trace_id: str) -> list[dict]:
    """The trace's observations, once the answered chat-completion and the finished chat span have arrived."""
    for _ in range(30):
        obs = show_trace.fetch(trace_id)
        names = {(o["name"], o.get("level")) for o in obs if o.get("endTime")}
        if ("chat", "DEFAULT") in names and ("chat-completion", "DEFAULT") in names:
            return obs
        time.sleep(3)
    raise RuntimeError(f"trace {trace_id} incomplete in Langfuse after waiting")


def parsed(value):
    """Observation input/output as stored: the v2 API returns JSON values either decoded or as a string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def describe(record: Record, obs: list[dict], expected_sources: list[str]) -> judge.Turn:
    """Fill the record's metrics from the trace and return the turn the judge reads."""
    by_name = lambda n: [o for o in obs if o["name"] == n]  # noqa: E731
    chat = by_name("chat")[0]
    generations = [o for o in by_name("chat-completion") if o.get("level") == "DEFAULT" and o.get("output")]
    turns: dict[str, judge.Turn] = {}
    judge.add_turns(turns, generations)  # picks the latest answered attempt, like `make judge`
    turn = turns[record.trace_id]
    final = next(o for o in generations if o["id"] == turn.observation_id)
    usage = final.get("usageDetails") or {}
    record.gen_latency = show_trace.seconds(final)
    record.output_tokens = usage.get("output")
    record.prompt_tokens = usage["total"] - usage.get("output", 0) if "total" in usage else None
    record.attempts = ((by_name("generate") or [{}])[0].get("metadata") or {}).get("attempts")
    record.refused = REFUSAL in turn.answer.lower().replace("’", "'")
    if expected_sources:
        hits = parsed(by_name("retrieve")[0].get("output")) or []
        retrieved = [h["source"].removeprefix(experiment_dataset.SOURCE_PREFIX) for h in hits]
        record.hit = float(any(s in expected_sources for s in retrieved))
        record.scores["_hit_comment"] = "retrieved: " + ", ".join(dict.fromkeys(retrieved))
    record.latency = show_trace.seconds(chat)  # last: a record with a latency is fully described
    return turn


def score_item(record: Record, turn: judge.Turn, stats: Counter, judge_stats: Counter, failed: list[str]) -> None:
    existing = {s["name"]: s["value"] for s in show_trace.fetch_scores(record.trace_id)}
    comment = record.scores.pop("_hit_comment", "")
    if record.hit is not None:
        if HIT_SCORE in existing:
            stats[f"{HIT_SCORE}:skipped"] += 1
        else:
            judge.langfuse.create_score(
                score_id=f"{HIT_SCORE}-{record.trace_id}", name=HIT_SCORE, value=record.hit, data_type="NUMERIC",
                trace_id=record.trace_id, comment=comment, metadata={"run": RUN, "item": record.item_id},
            )
            stats[f"{HIT_SCORE}:ok"] += 1
        record.scores[HIT_SCORE] = record.hit
    for evaluator in judge_prompts.EVALUATORS:
        name = evaluator["name"]
        if name in existing:
            stats[f"{name}:skipped"] += 1
            record.scores[name] = existing[name]
            print(f"  {name}: already scored ({existing[name]:g}), skipped", flush=True)
            continue
        try:
            result = judge.judge(evaluator, turn, judge_stats)
            judge.post_score(evaluator, turn, result)
        except Exception as exc:  # recorded as failed; the next run retries it
            stats[f"{name}:failed"] += 1
            failed.append(f"{record.item_id} {name}: {type(exc).__name__} {str(exc)[:150]}")
            print(f"  {name}: FAILED {type(exc).__name__}", flush=True)
            continue
        stats[f"{name}:ok"] += 1
        judge_stats["retried"] += result.attempts > 1
        record.scores[name] = result.score
        print(f"  {name}={result.score:g}  ({result.attempts} attempt(s), {result.seconds:.1f}s)  {result.reasoning}", flush=True)


def mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 3) if values else None


def median(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def summarize(records: list[Record], config: dict, stats: Counter, judge_stats: Counter, failed: list[str], seconds: float) -> None:
    done = [r for r in records if r.trace_id and r.latency is not None]
    on_topic = [r for r in done if r.kind != "off-topic"]
    off_topic = [r for r in done if r.kind == "off-topic"]
    result = {
        "run": RUN, "config": CONFIG_NAME, **config, "dataset": DATASET,
        "items": len(records), "answered": len(done),
        "chat_ok": stats["chat:ok"], "chat_skipped": stats["chat:skipped"], "chat_failed": stats["chat:failed"],
        **{f"{e['name']}_{k}": stats[f"{e['name']}:{k}"] for e in judge_prompts.EVALUATORS for k in ("ok", "skipped", "failed")},
        "complete": sum(all(e["name"] in r.scores for e in judge_prompts.EVALUATORS) for r in done),
        "groundedness": mean(r.scores.get("groundedness") for r in done),
        "relevance": mean(r.scores.get("answer-relevance") for r in done),
        "relevance_on_topic": mean(r.scores.get("answer-relevance") for r in on_topic),
        "retrieval_hit": mean(r.hit for r in on_topic),
        "on_topic_refusals": sum(bool(r.refused) for r in on_topic),
        "off_topic_refusals": f"{sum(bool(r.refused) for r in off_topic)}/{len(off_topic)}",
        "latency_median": median(r.latency for r in done), "latency_mean": mean(r.latency for r in done),
        "gen_latency_median": median(r.gen_latency for r in done),
        "prompt_tokens_mean": mean(r.prompt_tokens for r in done), "output_tokens_mean": mean(r.output_tokens for r in done),
        "turns_with_llm_retries": sum((r.attempts or 1) > 1 for r in done),
        "chat_requests": stats["chat_requests"], "judge_llm_calls": judge_stats["llm_calls"],
        "minutes": round(seconds / 60, 1),
    }
    print("\nsummary")
    print(f"  run {RUN} (config {CONFIG_NAME}: top_k={config['top_k']} prompt={config['prompt_label']} "
          f"model={config['model']} chunking={config['chunking']}), dataset {DATASET}, {result['minutes']} min")
    print(f"  chat              ok={stats['chat:ok']}  skipped={stats['chat:skipped']}  failed={stats['chat:failed']}"
          f"  (requests={stats['chat_requests']})")
    for name in [HIT_SCORE, *(e["name"] for e in judge_prompts.EVALUATORS)]:
        print(f"  {name:<17} ok={stats[f'{name}:ok']}  skipped={stats[f'{name}:skipped']}  failed={stats[f'{name}:failed']}")
    errors = {k.removeprefix("error_"): v for k, v in sorted(judge_stats.items()) if k.startswith("error_")}
    chat_errors = {k.removeprefix("chat_error "): v for k, v in sorted(stats.items()) if k.startswith("chat_error ")}
    print(f"  judge llm calls={judge_stats['llm_calls']}  judgements needing retries={judge_stats['retried']}  errors={errors or 'none'}")
    print(f"  chat errors={chat_errors or 'none'}")
    print(f"  answered {len(done)}/{len(records)}, fully scored {result['complete']}")
    print(f"  groundedness={result['groundedness']}  relevance={result['relevance']} (on-topic {result['relevance_on_topic']})"
          f"  retrieval-hit={result['retrieval_hit']}  refusals on-topic={result['on_topic_refusals']} off-topic={result['off_topic_refusals']}")
    print(f"  latency median={result['latency_median']}s mean={result['latency_mean']}s (final LLM call median "
          f"{result['gen_latency_median']}s)  tokens prompt={result['prompt_tokens_mean']} output={result['output_tokens_mean']}"
          f"  turns with LLM retries={result['turns_with_llm_retries']}")
    for f in failed:
        print(f"  failed: {f}")
    if failed:
        print(f"Re-run `make experiment EXPERIMENT_CONFIG={CONFIG_NAME}{'' if RUN == CONFIG_NAME else f' EXPERIMENT_RUN={RUN}'}` to retry the failed ones.")
    print("RESULT " + json.dumps(result))


def main() -> None:
    if CONFIG_NAME not in CONFIGS:
        sys.exit(f"unknown EXPERIMENT_CONFIG {CONFIG_NAME!r}; one of {sorted(CONFIGS)}")
    config = CONFIGS[CONFIG_NAME]
    check_config(config)
    started = time.monotonic()
    langfuse = judge.langfuse
    dataset = langfuse.get_dataset(DATASET)
    expected = {i["id"]: i for i in experiment_dataset.ITEMS}
    items = sorted((i for i in dataset.items if not ONLY_ITEMS or i.id in ONLY_ITEMS), key=lambda i: i.id)
    since = min(i.created_at for i in dataset.items) - timedelta(days=1)  # experiments can't predate the dataset
    experiment_id = langfuse._create_observation_id(seed=f"{dataset.id}:{RUN}")  # stable, so re-runs continue it
    linked, earlier_errors = answered_items(experiment_id, since)
    description = (f"{CONFIG_NAME}: top_k={config['top_k']}, prompt label {config['prompt_label']}, "
                   f"model {config['model']}, chunking {config['chunking']}")
    experiment = {
        "id": experiment_id, "name": RUN, "description": description, "dataset_id": dataset.id,
        "metadata": {"config": CONFIG_NAME, **{k: str(v) for k, v in config.items()}},
    }
    print(f"run {RUN} (experiment {experiment_id}) on dataset {DATASET}: {len(items)} items, {len(linked)} already "
          f"answered{f', {earlier_errors} failed traces from earlier runs' if earlier_errors else ''}. {description}. "
          f"LLM retries in the chatbot: {LLM_RETRY['max_retries']} (backoff {LLM_RETRY['backoff_base']:g}s x2^n up to "
          f"{LLM_RETRY['backoff_max']:g}s); chat requests: max {MAX_ATTEMPTS} attempts, backoff {BACKOFF_BASE:g}s x2^n "
          f"up to {BACKOFF_MAX:g}s, pause {PAUSE:g}s", flush=True)

    stats, judge_stats = Counter(), Counter()
    failed: list[str] = []
    records: list[Record] = []
    with httpx.Client(base_url=CHATBOT_URL, timeout=TIMEOUT) as api:
        for n, item in enumerate(items, 1):
            record = Record(item.id, (item.metadata or {}).get("kind", "?"))
            records.append(record)
            print(f"[{n}/{len(items)}] {item.id} ({record.kind})  Q: {item.input['question']}", flush=True)
            try:
                if item.id in linked:
                    record.trace_id, record.status = linked[item.id], "skipped"
                    stats["chat:skipped"] += 1
                    print(f"  chat: already answered in the run (trace {record.trace_id}), skipped", flush=True)
                else:
                    data = ask(api, item, config, experiment, stats)
                    record.trace_id, record.status = data["trace_id"], "ok"
                    stats["chat:ok"] += 1
                    print(f"  chat: ok trace {data['trace_id']} prompt v{data['prompt']['version']} "
                          f"({data['chat_attempts']} request(s))  A: {' '.join(data['answer'].split())[:120]}", flush=True)
                turn = describe(record, observations(record.trace_id), (item.expected_output or {}).get("sources", []))
            except Exception as exc:  # ChatFailed, or Langfuse trouble: record it and move on
                if record.status == "failed":  # not answered: the next run asks again
                    stats["chat:failed"] += 1
                record.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                failed.append(f"{item.id} chat: {record.error}")
                print(f"  FAILED {record.error}", flush=True)
                continue
            if item.id not in expected:
                print(f"  note: {item.id} isn't in experiment_dataset.py", flush=True)
            score_item(record, turn, stats, judge_stats, failed)
            if record.hit is not None:
                print(f"  {HIT_SCORE}={record.hit:g}  latency {record.latency:.1f}s  tokens {record.prompt_tokens}+{record.output_tokens}", flush=True)
    langfuse.flush()
    summarize(records, config, stats, judge_stats, failed, time.monotonic() - started)


if __name__ == "__main__":
    sys.exit(main())
