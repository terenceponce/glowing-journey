# Experiments: dataset runs

A fixed Langfuse dataset, `rag-eval`, goes through the chatbot under a few named configs. Each config becomes a Langfuse **experiment** (what v3 called a dataset run). Every answer is scored by the LLM judge from [evals.md](evals.md) plus a retrieval check. The point is to see how top-k, chunk size and the prompt change answer quality, latency and tokens.

Everything stays on the free model, **`glm-4.7-flash`**, for both the chatbot and the judge. The runner and the chatbot refuse any other model.

## Quick start

```sh
make experiment-setup                              # the rag-eval dataset and the candidate prompt (label "concise")
make experiment EXPERIMENT_CONFIG=baseline         # one config as one experiment; resumable, 35-125 min on the free model
make experiment EXPERIMENT_CONFIG=top-k-3
make experiment EXPERIMENT_CONFIG=top-k-8
make experiment EXPERIMENT_CONFIG=prompt-concise

make ingest CHUNK_SIZE=600 CHUNK_OVERLAP=100        # chunks-600 needs docs re-ingested (~3.5 min)
make experiment EXPERIMENT_CONFIG=chunks-600
make ingest                                         # back to 1200 / 200

make experiment-check                               # defaults are back, and the chatbot answers
```

Re-running `make experiment` with the same config continues the same experiment. Answered items are skipped, failed ones are asked again, and missing scores are filled in. `EXPERIMENT_ITEMS=rag-eval-02,rag-eval-16` limits a run to some items, and `EXPERIMENT_RUN=<name>` starts a fresh experiment under another name.

| Target | What |
|---|---|
| `experiment-config` | Writes the `experiment-config` ConfigMap (the `EXPERIMENT_*` variables) and the `experiment-scripts` ConfigMap (`scripts/experiment*.py`, `run_experiment.py`, and the judge scripts through `judge-config`) |
| `experiment-setup` | Runs `scripts/experiment_setup.py` as a Job (`deploy/experiments/setup-job.yaml`). It upserts the dataset and its 20 items by stable id and creates the candidate prompt version under the label `EXPERIMENT_PROMPT_LABEL` (`concise`). It never moves `production`, and it fails if `production` changed. Idempotent: a re-run reports "20 unchanged" and "up to date". |
| `experiment` | Runs `scripts/run_experiment.py` as a Job (`deploy/experiments/experiment-job.yaml`, deadline 4 h) and follows its log, which ends with the summary and a `RESULT {json}` line |
| `experiment-check` | Pipes `scripts/experiment_check.py` into `python -` in the chatbot pod. It checks `TOP_K`, `LANGFUSE_PROMPT_LABEL`, `LLM_MODEL`, `LLM_ALLOWED_MODELS`, the chunking `docs` was last ingested with (ingest-config) and that `production` holds the content of `prompts.py`. It prints the `docs` point count, then asks one question with `make chatbot-test`. |

| Variable | Default | What |
|---|---|---|
| `EXPERIMENT_CONFIG` | `baseline` | Config name, from `CONFIGS` in `scripts/run_experiment.py` |
| `EXPERIMENT_RUN` | the config name | Experiment (run) name; the experiment id is derived from the dataset id and this name |
| `EXPERIMENT_ITEMS` | all | Comma-separated dataset item ids |
| `EXPERIMENT_PROMPT_LABEL` | `concise` | Label of the candidate prompt version (`experiment-setup`) |
| `EXPERIMENT_MAX_ATTEMPTS` | 3 | Chat requests per item before it's recorded as failed |
| `EXPERIMENT_BACKOFF_BASE` / `_MAX` | 30 / 180 s | Backoff between chat requests: `base * 2^n`, capped, times 0.5–1.5 |
| `EXPERIMENT_PAUSE` | 3 s | Pause after every chat request (the judge has its own `JUDGE_PAUSE`) |
| `EXPERIMENT_TIMEOUT` | 900 s | HTTP timeout of one chat request; covers the chatbot's whole retry budget |
| `EXPERIMENT_LLM_MAX_RETRIES` | 7 | LLM retries the chatbot makes for an experiment request (its own default is 3) |
| `EXPERIMENT_LLM_BACKOFF_BASE` / `_MAX` | 2 / 60 s | The chatbot's backoff for those retries |

## The dataset

`scripts/experiment_dataset.py` holds 20 questions about the corpus, the Kubernetes concepts docs ([vector-store.md](vector-store.md)). Each item has a stable id (`rag-eval-01` … `rag-eval-20`), an expected answer written from the docs and the expected source pages:

| Kind | Items | What a good answer does |
|---|---|---|
| direct | 01–14 | Answers a standalone question: workloads, Pods, scheduling, storage, networking, security |
| follow-up | 15–17 | Answers a question that only makes sense after a fixed earlier turn ("How do I roll **it** back?"). The earlier turn is sent as fixed `history`, so only the follow-up changes between configs. |
| off-topic | 18–20 | France, sourdough, GKE pricing: says "I don't know from the documentation" |

In Langfuse the item input is `{question, history?}`, the expected output is `{answer, sources}` and the metadata is `{kind}`.

## The configs

| Config | top-k | Prompt | Chunking | Hypothesis |
|---|---|---|---|---|
| `baseline` | 5 | `production` (v2) | 1200 / 200 | The defaults |
| `top-k-3` | 3 | `production` | 1200 / 200 | Fewer tokens and faster, more retrieval misses |
| `top-k-8` | 8 | `production` | 1200 / 200 | More hits, more tokens, maybe more distraction |
| `prompt-concise` | 5 | `concise` (v3) | 1200 / 200 | Shorter answers and less latency, maybe less relevant on multi-part questions |
| `chunks-600` | 5 | `production` | 600 / 100 | Smaller, more focused chunks but less context per chunk |

The model is `glm-4.7-flash` in every config. The `concise` prompt (v3) is the production prompt with the length rule changed to "Answer in at most three sentences" (`scripts/experiment_setup.py`). The prompt comparison uses labels: v3 has the label `concise`, and `production` stays on v2 the whole time.

Chunking can't be changed per request, because it's how `docs` was ingested. A config names the chunking it expects, and the runner refuses to start if the `ingest-config` ConfigMap says otherwise.

## How it works

### Chatbot 0.4.0: per-request overrides

The runner talks to the real chatbot over `POST /chat`, so every answer is traced exactly as a user's would be. `chatbot:0.4.0` takes these optional request fields ([chatbot.md](chatbot.md#api)). Without them it behaves as before, and the defaults are unchanged.

| Field | What |
|---|---|
| `top_k` | 1–20, instead of `TOP_K` |
| `prompt_label` / `prompt_version` | The `rag-chat` version to fetch from Langfuse, mutually exclusive. There's no fallback to the built-in prompt: if it can't be fetched, the answer is 502 "prompt unavailable", so a run can't silently mix prompts. |
| `model` | Must be in `LLM_ALLOWED_MODELS` (default: `LLM_MODEL` only); anything else is 422 |
| `history` | Fixed earlier turns; the turn isn't saved to the session |
| `retry` | `{max_retries ≤ 12, backoff_base, backoff_max}` for this request's LLM calls |
| `experiment` | The experiment context (below) |

The response also returns `top_k` and `prompt {name, version, fallback}`, so the runner checks that the overrides were applied.

### Linking traces to an experiment in Langfuse v4

This took the most digging. Langfuse v4 (this deployment runs v4.50.0 in `events_only` mode) **doesn't use dataset run items any more**:

- `GET /api/public/dataset-run-items` and `GET /api/public/datasets/{name}/runs` return **404**: "This endpoint is not available on deployments running in Langfuse v4 events_only mode".
- `POST /api/public/dataset-run-items` still exists, but it's deprecated and only writes a stale compatibility object. Runs created that way don't show up. It also needs a `traceId` or `observationId` (400 without one).
- A trace belongs to an experiment through **attributes on its observations**: `langfuse.experiment.id`, `.name`, `.description`, `.metadata`, `.dataset.id`, `.item.id`, `.item.metadata`, `.item.expected_output` and `.item.root_observation_id` ([Langfuse FAQ: experiment runs not visible in v4](https://langfuse.com/faq/all/experiment-runs-not-visible-v4), [v3 → v4 upgrade guide](https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4)). The SDK's `run_experiment()` sets them by propagating them to every span of the task.
- Reads go through the **experiments API**: `GET /api/public/experiments` and `/experiment-items` (SDK `api.experiments.list` / `list_items`, both need `from_start_time`).

`run_experiment()` can't be used as is, because the task here is an HTTP call to another process. So the runner sends the context in the request's `experiment` field, and the chatbot sets the attributes on its own spans the way the SDK's `_process_experiment_item` does: it propagates them to every observation of the turn with the `chat` span as `root_observation_id`, and puts the description and expected output on the root span. The experiment id is deterministic, `_create_observation_id(seed="<dataset id>:<run name>")` (the SDK's own fallback when there's no run id), so re-running a config writes into the same experiment. Each trace also gets the tag `experiment:<name>`, and the chat span's `metadata.overrides` records what was overridden.

The chatbot uses `langfuse._client.propagation._propagate_attributes(experiment=...)` for this, which is private SDK API. It could break on a Langfuse SDK upgrade. Sending only a `trace_context` without a parent span wasn't an option, because that creates an orphan parent observation.

### Built for an unreliable free model

`glm-4.7-flash` often answers 429 (code 1305), sometimes fails every retry (502), and sometimes takes ~50 s for a successful call. So:

- **One request at a time.** Items run in sequence, with `EXPERIMENT_PAUSE` after each chat request and `JUDGE_PAUSE` between judge calls.
- **Two retry layers.** For experiment requests the chatbot retries the LLM up to 7 times (2 s × 2^n, capped at 60 s, with jitter), so an item is normally one trace. If the request still fails (502, 5xx, 429, timeout), the runner retries it up to 3 times with 30 s × 2^n backoff, capped at 180 s and jittered by 0.5–1.5. The judge has its own retries ([evals.md](evals.md)).
- **Failures are recorded, never fatal.** An item that fails every attempt is printed as failed, counted in the summary, and the run moves on.
- **Resumable.** On start, the runner reads the experiment's items through the experiments API. Items that already have an answered (non-ERROR) trace are skipped, and only their missing scores are judged. Score ids are deterministic (`groundedness-<trace>`, `answer-relevance-<trace>`, `retrieval-hit-<trace>`), so re-scoring overwrites instead of duplicating.
- **Counts in every summary.** ok / skipped / failed for chat and for each score, the chat and judge request counts, the errors by type, and the averages read back from Langfuse.

### Scores

| Score | From | Notes |
|---|---|---|
| `groundedness` | Script judge ([evals.md](evals.md)) | 0–1, every item |
| `answer-relevance` | Script judge | 0–1, every item. For off-topic items a correct refusal scores low, because it doesn't "answer" the question. |
| `retrieval-hit` | Runner, no LLM | 1 if any expected source page is among the retrieved chunks. Direct and follow-up items only. |

The summary also reports refusals (answers containing "don't know from the documentation") on- and off-topic, chat latency (the whole `chat` span, including LLM retries and backoff), the final `chat-completion`'s latency, and prompt/output tokens of that generation.

## Results

All five configs ran on 14–15 October 2026 over the same 20 items, with `glm-4.7-flash` for both the chatbot and the judge. Every run ended with 20/20 items answered and all scores posted. Some runs needed a resume (below). The numbers are what the run's final summary read back from Langfuse.

### Quality, latency and tokens

| Config | Groundedness | Relevance (all / on-topic) | Retrieval hit | Refusals (on-topic / off-topic) | Latency median / mean | Final LLM call median | Tokens prompt / output (mean) |
|---|---|---|---|---|---|---|---|
| `baseline` (k 5, v2, 1200) | 1.00 | 0.87 / 0.965 | 0.941 (16/17) | 0 / 3 of 3 | 9.8 s / 21.8 s | 3.7 s | 1073 / 96 |
| `top-k-3` | 0.985 | 0.85 / 1.00 | **0.882** (15/17) | 0 / 3 of 3 | 15.1 s / 29.2 s | 4.4 s | **673** / 80 |
| `top-k-8` | 0.95 | 0.85 / 1.00 | **1.00** (17/17) | 0 / 3 of 3 | 38.1 s / 92.9 s | 19.0 s | **1613** / 105 |
| `prompt-concise` (v3) | 1.00 | 0.85 / 0.941 | 0.941 (16/17) | **1** / 3 of 3 | **3.9 s / 14.2 s** | **2.3 s** | 1077 / **50** |
| `chunks-600` (600 / 100) | 0.97 | 0.87 / 0.965 | 0.941 (16/17) | 0 / 3 of 3 | 41.2 s / 81.6 s | 33.2 s | 728 / 69 |

Groundedness and relevance are means of the judge's 0–1 scores over all 20 items. Retrieval hit is over the 17 on-topic items. Latency is the whole `chat` span, including the chatbot's LLM retries and backoff. "Final LLM call" is the successful `chat-completion` alone.

### Success and failure counts

Each `make experiment` pass prints its own counts. A resume skips what's done, so the passes add up per config:

| Config | Passes | Minutes | Chat ok / failed per pass | Judge scores failed per pass (groundedness + relevance) | Chat requests | ERROR traces | Judge LLM calls | Judge 429s | Other judge errors |
|---|---|---|---|---|---|---|---|---|---|
| `baseline` | 2 | 5.2 + 35.0 | 2/0, 18/0 (+2 skipped) | 0, 0 | 23 | 3 | 102 | 62 | 0 |
| `top-k-3` | 3 | 77.1 + 26.9 + 2.8 | 18/2, 1/1, 1/0 | 2, 2, 0 | 30 | 10 | 146 | 100 | 5 timeouts, 1 HTTP 500 |
| `top-k-8` | 2 | 123.4 + 1.5 | 19/1, 1/0 | 4, 0 | 29 | 9 | 132 | 86 | 5 timeouts, 1 connection error |
| `prompt-concise` | 2 | 43.2 + 1.2 | 19/1, 1/0 | 0, 0 | 24 | 4 | 85 | 42 | 2 connection errors, 1 HTTP 500 |
| `chunks-600` | 1 | 83.4 | 20/0 | 0 | 23 | 3 | 83 | 40 | 3 timeouts |

- Every failed chat was a 502 from the chatbot after its 8 LLM attempts (all 429s), repeated over the runner's 3 requests. Every failed score was a judge call that ran out of retries (429 code 1302/1305, a timeout or a 500). Each failure was printed, counted, and retried on the next pass.
- **ERROR traces** come from the experiments API (`list_items`). Each failed chat request leaves one trace at level ERROR with the experiment attributes, so it shows up as an experiment item. Every config has exactly 20 DEFAULT items, one per dataset item, plus its ERROR ones. A config's `itemCount` is 20 + its ERROR traces, which equals its chat requests: baseline 23, top-k-3 30, top-k-8 29, prompt-concise 24, chunks-600 23.
- About 4 of 10 judge calls hit a 429, and 12–14 of the 20 turns in each run needed at least one LLM retry inside the chatbot. Wall time per config ranged from 40 to 125 minutes, depending on Z.AI's load rather than the config.

### Where the configs differed

| Item | baseline | top-k-3 | top-k-8 | prompt-concise | chunks-600 |
|---|---|---|---|---|---|
| 02 DaemonSet | hit | **miss**: answered with nodeSelector, groundedness 0.7 | hit | hit | hit |
| 13 Service types | 1 / 1 | 1 / 1 | 1 / 1 | 1 / 1 | groundedness **0.4**, relevance **0.4**: listed NodePort and ExternalName, which weren't in its 600-char chunks |
| 16 StatefulSet names (follow-up) | **miss** (expected page at rank 8) | **miss** | hit, but groundedness **0**: cited a Nodes passage | **miss, refused** ("I don't know"), relevance 0 | **miss** |
| 18–20 off-topic | refused 3/3 | refused 3/3 | refused 3/3 | refused 3/3 | refused 3/3 |

All other on-topic items scored groundedness 1 and relevance 1 in every config.

### What we concluded

- **The judge scores barely separate the configs.** Groundedness is 0.95–1.00 and on-topic relevance 0.94–1.00 everywhere. On 20 items one bad answer moves the mean by 0.05, so these differences are single items (02, 13, 16), not trends. As #8 found, groundedness is lenient: a refusal scores 1 ("makes no claims"), and most real answers score 1. It did drop on three answers that went beyond their passages (top-k-3 item 02, chunks-600 item 13, top-k-8 item 16), which is the right call. **Don't read the 0.95 vs 1.00 differences as quality rankings.**
- **Overall relevance mostly measures the off-topic items.** A correct refusal "doesn't answer the question", so the judge usually gives it 0, sometimes 1. That's why every overall relevance is 0.85–0.87; use the on-topic column.
- **Retrieval hit and tokens are the signals that move.** top-k 3 cuts prompt tokens by 37 % (1073 → 673) but misses one more page (item 02, where the DaemonSet page sits at rank 4). top-k 8 finds every expected page but costs 50 % more prompt tokens (1613) and didn't produce better-scored answers. **top-k 5 is a reasonable default**; the miss it keeps (item 16) is a follow-up whose question ("its Pods") is embedded without the earlier turn, which a query rewrite would fix and a bigger k only papers over.
- **Chunks of 600 / 100 didn't help.** Hit rate is the same as 1200 / 200 (item 16 still misses). Prompts are 32 % shorter, but each chunk carries less context, and item 13 shows the downside: the model filled the gaps from its own knowledge. Keep 1200 / 200. The collection has 5159 points at 600 / 100 vs 3048 at 1200 / 200.
- **The concise prompt is the only change that clearly paid off on cost**: half the output tokens (96 → 50) and the lowest latency, with the same groundedness and the same answers on the direct questions. It refused item 16 rather than guessing from a passage that didn't answer it. That's arguably the more honest answer, but it cost relevance. It's a candidate for `production` once the follow-up retrieval is fixed. That decision is Terence's; `production` stays on v2.
- **Latency doesn't compare across these runs.** The runs took place at different times of night, and Z.AI's load dominated: the final LLM call alone had a median of 2–4 s in the evening runs and 19–33 s in the late ones (top-k-8, chunks-600), whatever the prompt size. One top-k-8 turn took 762 s of retries. Compare latency only from runs made back to back, or run each config more than once.

### Seeing the runs

The runs are Langfuse **experiments** on the `rag-eval` dataset: `baseline`, `top-k-3`, `top-k-8`, `prompt-concise` and `chunks-600`. They belong to the dataset in the UI. They were verified through the public API rather than the UI: `GET /api/public/experiments` lists all five with 20 DEFAULT items each, and the runner read every item's three scores back from Langfuse ("fully scored 20" in each final summary). A trace shows its scores with `make chatbot-trace TRACE=<id>`.

## Leaving it as it was

After the runs:

- `docs` was re-ingested with 1200 / 200 (`make ingest`), giving 3048 points again.
- `production` on `rag-chat` is still version 2, the content of `prompts.py`. Version 3 has only the label `concise`. It stays, because the prompt comparison can be re-run from it.
- The chatbot still runs with `TOP_K=5`, `LANGFUSE_PROMPT_LABEL=production`, `LLM_MODEL=glm-4.7-flash` and 1 replica. Overrides only apply to the requests that send them.
- `make experiment-check` passed, and the chatbot answered "What is a Pod?".

The dataset, the experiments and their traces and scores stay in Langfuse.

## Surprises

- **Langfuse v4 dropped dataset run items.** The run-item read endpoints return 404 in `events_only` mode, and the write endpoint creates runs nobody sees. Experiments are attributes on observations, read through the experiments API ([above](#linking-traces-to-an-experiment-in-langfuse-v4)). This isn't obvious from the v3-era docs or the SDK's `dataset_run_items` client, which still exist.
- **Failed attempts count as experiment items.** Langfuse's docs say there's at most one experiment item per dataset item. Here a chat request that fails leaves an ERROR trace carrying the experiment attributes, so `itemCount` is 20 + failures (up to 30). The runner ignores ERROR items when it resumes, and the summary only counts the DEFAULT ones. To avoid it, the chatbot could leave out the experiment attributes when it returns 502. That wasn't done here, so every config ran the same code.
- **Experiments can't be deleted.** There's no delete endpoint for them in v4. The early smoke tests left an experiment `smoke-v4` (1 item) and a legacy run `smoke` made through the deprecated endpoint (invisible in v4). They're harmless and distinguishable by name.
- **The free model's load varied a lot over the night.** The same config took 35 or 125 minutes, and the final LLM call's median went from 2 s to 33 s. Three of five configs needed a resume. Resuming worked as intended: each pass retried only the missing chats and scores, and the last passes took 1–3 minutes.
- **`make experiment-check` itself can hit a 429.** The first check after restoring the defaults passed every config check but got a 502 on its test question. The re-run 30 minutes later answered. A failed check means "look at why", not necessarily that something is still changed.
- **The v2 observations API returns some outputs as JSON strings.** The retrieve span's output came back as a string, so the runner parses it before reading the sources. Dataset items come back newest first, so the runner sorts them by id.
- **A missing prompt label is logged as a cache eviction.** `get_prompt(label=...)` for a label that doesn't exist logs "not found during refresh, evicting from cache" before raising. The chatbot turns it into a 502 "prompt unavailable" rather than falling back, so an experiment can't silently use the wrong prompt.
- **The follow-up item 16 fails retrieval in every config but top-k 8.** The chatbot embeds only the last user message, "Do its Pods keep the same network names…", without the earlier turn, so "its" (the StatefulSet) never reaches the search. This is the clearest quality problem the dataset found, and it's outside this issue's scope. A query-rewrite step using the history is the likely fix.
