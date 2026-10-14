# Evals: LLM-as-judge

Chat turns are scored automatically by an LLM judge on two things. Each score is stored in Langfuse on the turn's trace, next to `user-feedback` ([observability.md](observability.md#user-feedback-score)):

| Score | Question it answers | Range |
|---|---|---|
| `groundedness` | Is the answer supported by the retrieved context passages? | 0–1, NUMERIC |
| `answer-relevance` | Does the answer address the user's question? | 0–1, NUMERIC |

The judge model is the same free model as the chatbot, **`glm-4.7-flash`** on Z.AI's general API. Don't switch it to a paid model.

Langfuse's own LLM-as-judge evaluators are set up, but **they can't run on this model**: Langfuse asks for structured output with `response_format: json_schema`, which glm-4.7-flash ignores, so Langfuse pauses the evaluators when they're saved ([below](#langfuses-own-evaluators)). The scores come from the **script judge** `scripts/judge.py` instead. It sends the same prompts one request at a time, with backoff, and posts the results to Langfuse as scores.

## Quick start

```sh
make judge-setup                 # Langfuse: Z.AI LLM connection, the two evaluators, the chat-judge rule (off)
make judge                       # score the 10 most recent chat turns that aren't scored yet
make judge JUDGE_LIMIT=50        # more turns; already-scored ones are skipped
make judge JUDGE_TRACES=<id>,<id> JUDGE_FORCE=true   # re-score given turns (e.g. after changing a prompt)
make chatbot-trace TRACE=<id>    # the trace, with groundedness / answer-relevance / user-feedback scores
```

| Target | What |
|---|---|
| `judge-config` | Writes the `judge-config` ConfigMap (the `JUDGE_*` variables) and the `judge-scripts` ConfigMap (`scripts/judge*.py`). The other two targets run it. |
| `judge-setup` | Runs `scripts/judge_setup.py` as a Job (`deploy/judge/setup-job.yaml`). It upserts the LLM connection and creates or updates the evaluators and the rule. Idempotent: a re-run prints "up to date" and doesn't create new evaluator versions. Also runs `chatbot-secret`, so the key comes from `secrets/zai.env`. |
| `judge` | Runs `scripts/judge.py` as a Job (`deploy/judge/judge-job.yaml`) and follows its log, which ends with the summary |

Both Jobs use the chatbot image (Python 3.12, `openai`, `langfuse`, `httpx`) with the scripts mounted from the ConfigMap. `judge.py` imports `show_trace.py` from the image to read scores, the same code `make chatbot-trace` uses. The chatbot itself is unchanged. Everything the judge needs is already in the trace: the `chat-completion` generation's input holds the full prompt, including the retrieved passages, and its output holds the answer.

## What the judge reads

Both evaluators read the **last successful `chat-completion` generation** of a `chat` trace (level `DEFAULT`; failed 429 attempts are level `ERROR` and have no output):

| Variable | Source | Contains |
|---|---|---|
| `{{request}}` | generation input | The messages sent to the chat model: the system prompt, earlier turns, then the user message with the numbered context passages and `Question: …` |
| `{{answer}}` | generation output | The assistant message with the answer |

The retrieve span's output only lists chunk IDs and scores, not the text. That's why the judge uses the generation rather than the retrieve span, and also why a Langfuse observation evaluator would have to target the generation: observation evaluators don't see sibling observations.

Each score is attached to that generation (and so to its trace):

| Field | Value |
|---|---|
| id | `groundedness-<trace_id>` / `answer-relevance-<trace_id>`: one per evaluator and turn, so re-scoring replaces it (same id, name and date, see [observability.md](observability.md#user-feedback-score)) |
| value | 0–1 |
| comment | the judge's reason |
| metadata | `{judge: script, model, attempts, errors, seconds}`: how many tries it took and which errors it hit |
| environment | `lab`, the same as the trace |

## The prompts

The source is `scripts/judge_prompts.py`, shared by the script judge and the Langfuse evaluators. Both evaluators use the same system message:

> You are a strict evaluator of a retrieval-augmented chatbot that answers questions about Kubernetes. You are given the chatbot's request (its instructions, any earlier turns, then a final user message with numbered context passages and the question) and the chatbot's answer. Judge only what you are asked to judge.

**groundedness**

```
Rate the GROUNDEDNESS of the answer: are its statements supported by the numbered context passages in the last
user message of the request?

- 1.0: every factual statement is supported by the passages (wording may differ).
- 0.7: mostly supported; a minor detail goes beyond the passages.
- 0.4: a mix of supported and unsupported statements.
- 0.0: the main statements are not in the passages, or contradict them.
An answer that only says it doesn't know from the documentation makes no claims: rate it 1.0.
Check that citations like [2] point to a passage that says what is claimed. Don't judge whether the answer is
helpful or complete, only whether it is supported.

Request:
{{request}}

Answer:
{{answer}}
```

**answer-relevance**

```
Rate the RELEVANCE of the answer: does it address the user's question (the text after "Question:" in the last
user message of the request, read together with any earlier turns)?

- 1.0: directly and fully answers the question.
- 0.7: answers it, but misses part of it or adds a lot of unrelated material.
- 0.4: only partly related to the question.
- 0.0: doesn't answer the question: an answer about something else, or a refusal such as "I don't know from the
documentation".
A refusal scores 0.0 even when refusing was the right thing to do (for example an off-topic question): this score
measures whether the user got an answer, not whether the chatbot behaved well. Don't judge whether the answer is
correct or supported by the passages, only whether it answers the question.

Request:
{{request}}

Answer:
{{answer}}
```

Output: score 0–1 plus one or two sentences of reasoning. In Langfuse these are the evaluator's `outputDefinition` (`scoreValueInstructions`, `scoreReasoningInstructions`). The script judge appends the same two instructions to the prompt and gets the result from a forced tool call, `record_score(reasoning, score)`, with temperature 0 and thinking off.

So a refusal on an off-topic question comes out as **groundedness 1, relevance 0**: it was honest but didn't help. The two scores mean different things, so this is intended.

## Langfuse's own evaluators

`make judge-setup` (`scripts/judge_setup.py`) configures Langfuse's LLM-as-judge through the public API (Langfuse 4.50: `/api/public/llm-connections`, `/api/public/v2/evaluators`, `/api/public/v2/evaluation-rules`):

1. **LLM connection** `zai`: adapter `openai`, base URL `https://api.z.ai/api/paas/v4`, custom model `glm-4.7-flash`, no default OpenAI models. The key is read from the `app/zai-api` Secret, which `make chatbot-secret` creates from `secrets/zai.env`. It's sent only in the request body and never printed; Langfuse returns only a masked key.
2. **Evaluators** `groundedness` and `answer-relevance` (`llm_as_judge`, NUMERIC 0–1, model `zai`/`glm-4.7-flash`, the prompts above, mapping `request` ← input, `answer` ← output). Each update creates a new evaluator version, so the script only updates an evaluator when its definition changed.
3. **Evaluation rule** `chat-judge`: both evaluators on observations with `environment = lab`, `traceName = chat`, `type = GENERATION`, `name = chat-completion` and `level ≠ ERROR`, sampling 1. It's created **disabled** (`JUDGE_RULE_ENABLED=false`). Turning it on would do nothing while the evaluators are paused.

**What happens:** both evaluators are created with `status: paused`, `pausedReason: EVAL_MODEL_CONFIG_INVALID` ("no valid evaluation model is configured"). The UI shows the same paused banner.

**Why:** when an evaluator is saved, Langfuse makes one test call with structured output (`testModelCall` in the Langfuse source, through the Vercel AI SDK's `Output.object`). For an OpenAI-adapter connection, that is `response_format: {type: "json_schema", …}`. Called directly, glm-4.7-flash ignores it. With Langfuse's own test message, it answered "It appears you did not include the JSON schema in your message". With a scoring question, it answered just `0`. The schema never reaches the model, the reply isn't valid structured output, and Langfuse pauses the evaluator. Langfuse's logs say nothing about it, and the API only returns the generic paused message. `response_format: json_object` works but has no schema. **Forced tool calls work reliably**, so the script judge uses those. The public API's `modelConfig` only takes provider and model, so there's no way to switch Langfuse to tool calling.

**How Langfuse would cope with 429s (from the docs, not observable here):** a rate-limited execution is marked *Delayed* and retried with exponential backoff in the worker. Executions run in parallel (`LANGFUSE_EVAL_EXECUTION_WORKER_CONCURRENCY`, default 5), and each one creates a trace in environment `langfuse-llm-as-a-judge`, with statuses Completed / Error / Delayed / Pending on the evaluator page. A failed execution shows as *Error* there. On the trace itself, a missing score just looks like no score, with no marker. There's no setting for the retry limits, and five concurrent requests is exactly what the free model rejects.

What was observed: **a 429 at save time makes the API return HTTP 500.** Langfuse treats a retryable LLM error in the test call as an operational error and rethrows it, so `PATCH /api/public/v2/evaluators/{id}` returned `500 Internal Server Error` with no version created. `judge_setup.py` retries 5xx with backoff (5 s × 2ⁿ, up to 5 attempts); the next attempt went through.

## The script judge and the free model

`scripts/judge.py` follows the issue's rules for the free model:

- **One request at a time**, with `JUDGE_PAUSE` (2 s) after every request, successful or not. Turns and evaluators run in sequence.
- **Exponential backoff with jitter**: up to `JUDGE_MAX_ATTEMPTS` (8) attempts per judgement. The wait after attempt n is `JUDGE_BACKOFF_BASE · 2ⁿ⁻¹` (2, 4, 8, … s), capped at `JUDGE_BACKOFF_MAX` (60 s) and multiplied by a random 0.5–1.5. That's up to about 3–4 minutes of waiting per judgement, against about 8 s in the chatbot's 4 attempts. Retried: 429, 5xx, connection errors, timeouts (`JUDGE_TIMEOUT`, 120 s, enough for the occasional 54 s call), and replies without a usable score (no tool call or JSON, score outside 0–1, empty reason).
- **Resumable**: before judging a turn, it reads the trace's scores (`/api/public/v3/scores`, via `show_trace.fetch_scores`) and skips each evaluator whose score name is already there, from this script or from a Langfuse evaluator. A re-run only does what's missing. `JUDGE_FORCE=true` re-judges anyway and replaces the scores.
- **Failures don't stop the run.** A judgement that fails every attempt is printed as `FAILED` and listed in the summary. No score is posted, so the next `make judge` retries it. Nothing in Langfuse marks it as failed: a missing score is the signal, and the run log names the trace.
- **The summary** ends every run with `ok`, `skipped` and `failed` per evaluator, the number of LLM calls, how many judgements needed retries, and the errors by type.

| Env var | Makefile var | Default |
|---|---|---|
| `JUDGE_MODEL` | `JUDGE_MODEL` | `glm-4.7-flash` (free; keep it) |
| `JUDGE_BASE_URL` | `JUDGE_BASE_URL` | `$(LLM_BASE_URL)`, `https://api.z.ai/api/paas/v4` |
| `JUDGE_LIMIT` | `JUDGE_LIMIT` | `10` most recent answered chat turns |
| `JUDGE_TRACES` | `JUDGE_TRACES` | empty; comma-separated trace IDs instead of the most recent |
| `JUDGE_FORCE` | `JUDGE_FORCE` | `false` |
| `JUDGE_MAX_ATTEMPTS` | `JUDGE_MAX_ATTEMPTS` | `8` |
| `JUDGE_BACKOFF_BASE`, `JUDGE_BACKOFF_MAX` | same | `2`, `60` seconds |
| `JUDGE_PAUSE` | `JUDGE_PAUSE` | `2` seconds |
| `JUDGE_TIMEOUT` | `JUDGE_TIMEOUT` | `120` seconds per request |
| `JUDGE_PROVIDER`, `JUDGE_RULE_ENABLED`, `JUDGE_SAMPLING` | same | `zai`, `false`, `1` (Langfuse setup only) |
| `ZAI_API_KEY` | – | Secret `app/zai-api` |

The Job has a 2-hour deadline (`activeDeadlineSeconds`).

## Sample run (2026-10-14)

Four new turns were added first with `make chatbot-test`: two Kubernetes questions and two off-topic ones ("What is the capital of France?", "Who won the 2018 FIFA World Cup?"). The FIFA question got a 502 from the chatbot the first time (all four attempts were 429s) and was asked again.

| Run | Command | groundedness ok / skipped / failed | answer-relevance ok / skipped / failed | LLM calls | 429s | Judgements needing retries |
|---|---|---|---|---|---|---|
| 1 | `make judge` (latest 10) | 10 / 0 / 0 | 10 / 0 / 0 | 52 | 32 | 14 of 20 |
| 2 | `make judge JUDGE_FORCE=true JUDGE_TRACES=<the 2 off-topic turns>` (after the relevance-prompt fix below) | 2 / 0 / 0 | 2 / 0 / 0 | 15 | 11 | 4 of 4 |
| 3 | `make judge JUDGE_LIMIT=20` (all 18 answered turns) | 8 / 10 / 0 | 8 / 10 / 0 | 22 | 6 | 5 of 16 |

Every judgement eventually succeeded, and no run had a failure. The worst cases needed 6 attempts and took 85–93 s. About 60 % of all calls in runs 1 and 2 were 429s, and only one 429 type showed up (code 1305). Successful judge calls took 1.3–16 s. Run 3 shows the resume: the 10 turns scored in run 1 were skipped (20 judgements, 0 LLM calls).

Scores for all 18 answered chat turns in the project:

| Question | groundedness | answer-relevance |
|---|---|---|
| What is a Pod? (5 turns) | 1 | 1 |
| What is a Service? / What is a Service in Kubernetes? / What does a Service do in Kubernetes? | 1 | 1 |
| What is a Namespace? | 1 | 1 |
| How do containers in the same Pod share storage? (2 turns) | 1 | 1 |
| How does a ConfigMap get into a Pod? | 1 | 1 |
| What is a Kubernetes Secret and how is it different from a ConfigMap? | 1 | 1 |
| What is the difference between a Deployment and a StatefulSet? | 1 | 1 |
| Which one should I use for a database? (follow-up) | 1 | 1 |
| How do I scale it? (follow-up) | 1 | **0.4**: "lists multiple scaling methods … but does not directly address how to scale a specific workload" |
| What is the capital of France? → "I don't know from the documentation." | 1 | **0** |
| Who won the 2018 FIFA World Cup? → "I don't know from the documentation." | 1 | **0** |

**Do the scores make sense?** Mostly yes:

- The off-topic refusals get relevance 0 and groundedness 1, as intended.
- "How do I scale it?" gets 0.4 relevance. That matches the weakness noted in [observability.md](observability.md#example-a-conversation-session): retrieval ignored the history, so the answer was about HPA metrics in general rather than scaling the StatefulSet.
- **Negative control** (run in the chatbot pod with `judge.judge()`, not posted to Langfuse): the real request of the ConfigMap turn with a made-up answer ("a gRPC sidecar called cm-injector encrypts it with AES-256 … limited to 10 per namespace") got **groundedness 0.0**. A half-true version (env vars [1] plus the made-up sidecar) got **0.4**. So groundedness does go down when claims aren't in the passages. Every real answer scored 1, which is plausible for a model told to answer only from the passages, but this judge is lenient on small slips. The follow-up "Which one should I use for a database?" also got 1, although #5 noticed it citing a passage for a claim from the earlier turn.

## Surprises

- **The first relevance prompt scored "I don't know" at 0.7.** The judge reasoned that refusing was "the appropriate response" for an off-topic question. The original rubric listed "I don't know" under 0.0, but that wasn't enough. The prompt now says explicitly that a refusal scores 0 even when it was the right thing to do, and the two turns were re-scored with `JUDGE_FORCE=true` (0.7 → 0). Re-scoring replaced the scores rather than adding new ones: same id, name and date.
- **Relevance still leaks correctness.** In the negative control, the made-up but on-topic answer got relevance 0 "because it's not in the passages", despite the instruction not to judge correctness. Read relevance together with groundedness.
- **glm-4.7-flash ignores `response_format: json_schema` and thinks by default.** Without `thinking: {type: disabled}`, the replies had long `reasoning_content` and took much longer. Langfuse's evaluator calls don't send that flag, so they'd also be slow even if structured output worked.
- **Langfuse's evaluator "paused" state hides the cause.** The API's `pausedMessage` says "no valid evaluation model is configured" even though the connection and model exist. Only the source (`getBlockReasonForInvalidModelConfig`) shows that any failed test call that isn't an auth error ends up as `EVAL_MODEL_CONFIG_INVALID`.
- **Metadata-only evaluator updates skip the model check.** Changing just the description doesn't re-run the test call. Changing the prompt does, and it creates a new version. The setup script compares the stored definition first, so a re-run creates no new versions.
- **Langfuse 4.50 has a full public API for evals**: LLM connections, evaluators with versions, evaluation rules with filters on observation columns such as `name`, `level`, `traceName` and `environment`. Trace-level evaluators are deprecated in v4 (and inactive in `events_only` mode), so the rule targets the generation.
