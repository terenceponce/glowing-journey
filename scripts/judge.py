"""Script judge: score recent chat turns for groundedness and answer relevance, and post the results to Langfuse.

Run it with `make judge` (a Job using the chatbot image). For each of the JUDGE_LIMIT most recent `chat` traces
with a successful `chat-completion` generation, it asks JUDGE_MODEL each evaluator in judge_prompts.py and
records a NUMERIC score (0-1) with the reason as its comment, on that generation and its trace. It's built for
the free model, which often answers 429:

  - one LLM request at a time, with a JUDGE_PAUSE pause after each one
  - up to JUDGE_MAX_ATTEMPTS attempts per request, with exponential backoff and jitter
    (JUDGE_BACKOFF_BASE * 2^n seconds, capped at JUDGE_BACKOFF_MAX) on 429, 5xx, timeouts, connection errors
    and replies without a usable score
  - resumable: a trace that already has a score of that name (from this script or Langfuse's own evaluator)
    is skipped, so a re-run only retries what failed (JUDGE_FORCE=true re-judges them, e.g. after a prompt change)
  - a turn that fails every attempt is reported as failed and the run moves on; it never crashes the run
  - the summary at the end has the success, skipped and failure counts

Env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL, LANGFUSE_TRACING_ENVIRONMENT (the chatbot's
environment, used to find its traces and for the scores), ZAI_API_KEY, JUDGE_BASE_URL, JUDGE_MODEL,
JUDGE_LIMIT, JUDGE_TRACES (comma-separated trace ids instead of the most recent ones), JUDGE_FORCE (true:
re-judge turns that already have scores, replacing them), JUDGE_MAX_ATTEMPTS,
JUDGE_BACKOFF_BASE, JUDGE_BACKOFF_MAX, JUDGE_PAUSE, JUDGE_TIMEOUT. Needs show_trace.py from the chatbot image
on PYTHONPATH (to read scores).
"""

import json
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass

import openai
from langfuse import get_client

import judge_prompts
import show_trace

MODEL = os.environ["JUDGE_MODEL"]
ENVIRONMENT = os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "lab")
LIMIT = int(os.environ.get("JUDGE_LIMIT", "10"))
TRACES = [t.strip() for t in os.environ.get("JUDGE_TRACES", "").split(",") if t.strip()]
FORCE = os.environ.get("JUDGE_FORCE", "false").lower() == "true"  # re-judge scored turns (after a prompt change)
MAX_ATTEMPTS = int(os.environ.get("JUDGE_MAX_ATTEMPTS", "8"))
BACKOFF_BASE = float(os.environ.get("JUDGE_BACKOFF_BASE", "2"))
BACKOFF_MAX = float(os.environ.get("JUDGE_BACKOFF_MAX", "60"))
PAUSE = float(os.environ.get("JUDGE_PAUSE", "2"))
TIMEOUT = float(os.environ.get("JUDGE_TIMEOUT", "120"))

# APITimeoutError is a subclass of APIConnectionError.
RETRYABLE_ERRORS = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)

llm = openai.OpenAI(base_url=os.environ["JUDGE_BASE_URL"], api_key=os.environ["ZAI_API_KEY"], timeout=TIMEOUT, max_retries=0)
langfuse = get_client()

# The judge answers through a forced tool call: glm-4.7-flash ignores response_format json_schema (see docs/evals.md).
SCORE_TOOL = {
    "type": "function",
    "function": {
        "name": "record_score",
        "description": "Record the evaluation result.",
        "parameters": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string", "description": "Short reason for the score."},
                "score": {"type": "number", "minimum": 0, "maximum": 1, "description": "Score from 0 to 1."},
            },
            "required": ["reasoning", "score"],
        },
    },
}


class BadReply(Exception):
    """The model answered, but without a usable score. Retried like a 429."""


@dataclass
class Turn:
    trace_id: str
    observation_id: str
    request: str
    answer: str


@dataclass
class Result:
    score: float
    reasoning: str
    attempts: int
    errors: list[str]
    seconds: float


def as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def recent_turns() -> list[Turn]:
    """The latest successful chat-completion generation of each of the most recent chat traces."""
    turns: dict[str, Turn] = {}
    params = {
        "name": "chat-completion", "type": "GENERATION", "level": "DEFAULT", "environment": ENVIRONMENT,
        "fields": "core,basic,io", "limit": 100,
    }
    with show_trace.client() as api:
        if TRACES:
            for trace_id in TRACES:
                resp = api.get("/api/public/v2/observations", params={**params, "traceId": trace_id})
                resp.raise_for_status()
                add_turns(turns, resp.json()["data"])
            return list(turns.values())
        cursor = None
        while len(turns) < LIMIT:
            resp = api.get("/api/public/v2/observations", params={**params, **({"cursor": cursor} if cursor else {})})
            resp.raise_for_status()
            body = resp.json()
            add_turns(turns, body["data"])
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor or not body["data"]:
                break
    return list(turns.values())[:LIMIT]


def add_turns(turns: dict[str, Turn], observations: list[dict]) -> None:
    # Newest first, so the first one per trace is the final, answered attempt.
    for o in sorted(observations, key=lambda o: o["startTime"], reverse=True):
        if o["traceId"] in turns or not o.get("output"):
            continue
        output = o["output"]
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                pass
        answer = output.get("content") if isinstance(output, dict) else output
        turns[o["traceId"]] = Turn(o["traceId"], o["id"], as_text(o["input"]), as_text(answer))


def fill(template: str, turn: Turn) -> str:
    values = {"request": turn.request, "answer": turn.answer}
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)


def parse_reply(message) -> tuple[float, str]:
    """Score and reasoning from the forced tool call, or from JSON in the text as a fallback."""
    raw = message.tool_calls[0].function.arguments if message.tool_calls else (message.content or "")
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(match.group(0) if match else raw)
        score = float(data["score"])
        reasoning = str(data.get("reasoning") or data.get("reason") or "").strip()
    except (ValueError, TypeError, KeyError) as exc:
        raise BadReply(f"no score in reply: {raw[:120]!r}") from exc
    if not 0 <= score <= 1:
        raise BadReply(f"score {score} outside 0-1")
    if not reasoning:
        raise BadReply("empty reasoning")
    return score, reasoning


def judge(evaluator: dict, turn: Turn, stats: Counter) -> Result:
    """Ask the model, one request at a time, retrying with backoff. Raises the last error after MAX_ATTEMPTS."""
    messages = [{"role": m["role"], "content": fill(m["content"], turn)} for m in evaluator["prompt"]]
    messages[-1]["content"] += f"\n\nCall record_score. score: {evaluator['score']} reasoning: {evaluator['reasoning']}"
    errors: list[str] = []
    started = time.monotonic()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            stats["llm_calls"] += 1
            completion = llm.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0,
                max_tokens=512,
                tools=[SCORE_TOOL],
                tool_choice={"type": "function", "function": {"name": "record_score"}},
                extra_body={"thinking": {"type": "disabled"}},
            )
            score, reasoning = parse_reply(completion.choices[0].message)
            return Result(score, reasoning, attempt, errors, time.monotonic() - started)
        except (*RETRYABLE_ERRORS, BadReply) as exc:
            status = getattr(exc, "status_code", None)
            errors.append(f"attempt {attempt}: {type(exc).__name__}{f' {status}' if status else ''}")
            stats[f"error_{type(exc).__name__}{f'_{status}' if status else ''}"] += 1
            if attempt == MAX_ATTEMPTS:
                raise
            delay = min(BACKOFF_BASE * 2 ** (attempt - 1), BACKOFF_MAX) * random.uniform(0.5, 1.5)
            print(f"    {errors[-1]}, retrying in {delay:.1f}s", flush=True)
            time.sleep(delay)
        finally:
            time.sleep(PAUSE)
    raise AssertionError("unreachable")


def post_score(evaluator: dict, turn: Turn, result: Result) -> None:
    langfuse.create_score(
        score_id=f"{evaluator['name']}-{turn.trace_id}",  # one per evaluator and turn; a re-run replaces it
        name=evaluator["name"],
        value=result.score,
        data_type="NUMERIC",
        trace_id=turn.trace_id,
        observation_id=turn.observation_id,
        comment=result.reasoning,
        metadata={"judge": "script", "model": MODEL, "attempts": result.attempts, "errors": result.errors,
                  "seconds": round(result.seconds, 1)},
    )


def main() -> None:
    turns = recent_turns()
    print(f"judging {len(turns)} chat turns ({'JUDGE_TRACES' if TRACES else f'latest {LIMIT}'}) with {MODEL}: "
          f"max {MAX_ATTEMPTS} attempts, backoff {BACKOFF_BASE:g}s x2^n up to {BACKOFF_MAX:g}s, pause {PAUSE:g}s", flush=True)
    stats: Counter = Counter()
    failed: list[str] = []
    for i, turn in enumerate(turns, 1):
        existing = {s["name"] for s in show_trace.fetch_scores(turn.trace_id)}
        question = turn.request.rsplit("Question:", 1)[-1].strip().rstrip('"}]').strip()[:80]
        print(f"[{i}/{len(turns)}] trace {turn.trace_id}  Q: {question}", flush=True)
        for evaluator in judge_prompts.EVALUATORS:
            name = evaluator["name"]
            if name in existing and not FORCE:
                stats[f"{name}:skipped"] += 1
                print(f"  {name}: already scored, skipped")
                continue
            try:
                result = judge(evaluator, turn, stats)
            except Exception as exc:  # recorded as failed; the next run retries it
                stats[f"{name}:failed"] += 1
                failed.append(f"{turn.trace_id} {name}: {type(exc).__name__} {str(exc)[:150]}")
                print(f"  {name}: FAILED {type(exc).__name__}", flush=True)
                continue
            try:
                post_score(evaluator, turn, result)
            except Exception as exc:
                stats[f"{name}:failed"] += 1
                failed.append(f"{turn.trace_id} {name}: posting the score failed: {type(exc).__name__}")
                continue
            stats[f"{name}:ok"] += 1
            stats["retried"] += result.attempts > 1
            print(f"  {name}={result.score:g}  ({result.attempts} attempt(s), {result.seconds:.1f}s)  {result.reasoning}", flush=True)
    langfuse.flush()

    print("\nsummary")
    for e in judge_prompts.EVALUATORS:
        n = e["name"]
        print(f"  {n:<17} ok={stats[f'{n}:ok']}  skipped={stats[f'{n}:skipped']}  failed={stats[f'{n}:failed']}")
    errors = {k.removeprefix("error_"): v for k, v in sorted(stats.items()) if k.startswith("error_")}
    print(f"  llm calls={stats['llm_calls']}  judgements needing retries={stats['retried']}  errors={errors or 'none'}")
    for f in failed:
        print(f"  failed: {f}")
    if failed:
        print("Re-run `make judge` to retry the failed ones.")


if __name__ == "__main__":
    sys.exit(main())
