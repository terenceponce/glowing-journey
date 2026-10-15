# Failure drills

Practice at diagnosing production-style failures from Langfuse traces (plus `kubectl`). Each drill breaks one thing, sends a few chat turns, reads the traces, then restores the system. Every fault has a `make` switch, so the drills can be repeated.

Everything stays on the free model, **`glm-4.7-flash`**. Nothing here calls a paid model, and `production` on the `rag-chat` prompt stays on version 2.

## How a drill runs

```sh
make drill-<name>-on                                   # break it
make drill-run DRILL=<name>                            # traffic: 3 dataset items through the experiment runner
make drill-report DRILL=<name>                         # read the traces back from Langfuse, next to the `baseline` run
make drill-<name>-off                                  # restore
make drill-check                                       # nothing left switched on, defaults back, the chatbot answers
make drill-restore                                     # every -off at once, if a drill was left half-way
```

| Target | What |
|---|---|
| `drill-run DRILL=<name>` | Runs `make experiment` with the config `as-deployed` as dataset run `drill-<name>` over `DRILL_ITEMS` (default `rag-eval-01,rag-eval-05,rag-eval-09`, all direct questions). `as-deployed` sends no `top_k` or `prompt_label`, so the chatbot uses whatever it is deployed with. The judge is off (`DRILL_JUDGE=true` turns it on), the runner makes one chat request per item (`EXPERIMENT_MAX_ATTEMPTS=1`) and the chatbot retries the LLM `DRILL_LLM_RETRIES` (2) times. Items run one at a time. Failed items are recorded as failed and the run moves on. Drill runs never touch the five experiments of [experiments.md](experiments.md). |
| `drill-report DRILL=<name> [COMPARE=baseline]` | Runs `scripts/drill_report.py` in the chatbot pod. It reads only Langfuse: the experiments API for the run's traces, the observations API for each trace. It prints every observation (latency, level, status message, retrieval collection and scores, LLM attempts and errors, prompt version, tokens), then a summary per run, with the reference run limited to the same items. |
| `drill-check` | Lists the chatbot's env overrides (only `ZAI_API_KEY` should be left), the embedder's latency, Qdrant's ready replicas, then runs `make experiment-check` (defaults, `production` is v2, `docs` has 3048 points) and asks one question. |

The drills never edit the chatbot's code paths for normal use. A fault is a cluster change (a scaled-down StatefulSet, an env var on a Deployment) or, for the embedder, a latency flag that is `0` by default. Every `-on`/`-off` waits for its rollout.

The reference for "normal" is the `baseline` run of [experiments.md](experiments.md) on the same items. Its latencies came from another time of night, so compare structure (which span, which level, which count) before comparing seconds.

## 1. Qdrant scaled to 0

**Trigger:** `make drill-qdrant-down-on` (`kubectl -n qdrant scale statefulset/qdrant --replicas=0`, waits for `qdrant-0` to go).

**Symptoms:** every chat turn fails at once, in about 0.03 s. The runner reports `HTTP 502 retrieval failed: ResponseHandlingException` for all three items. No LLM call is made, so there is no waiting and no cost.

**Langfuse evidence** (`make drill-report DRILL=qdrant-down`):

```
trace d44c3321…  item rag-eval-01  level ERROR
  - SPAN       chat         0.03s  ERROR: retrieval failed: ResponseHandlingException
  - EMBEDDING  embed-query  0.02s
  - RETRIEVER  retrieve     0.01s  ERROR: ResponseHandlingException: [Errno 111] Connection refused  collection=docs top_k=5 hits=None
run drill-qdrant-down: 3 traces, 0 answered, 3 failed (chat level ERROR)
reference baseline: 3 answered, chat latency median 6.78s, 5 chunks per turn, top-1 score 0.806
```

- `chat` is ERROR and its status message names the step: *retrieval failed*.
- `embed-query` succeeded (0.02 s), which clears the embedder.
- `retrieve` is the first ERROR, with the cause in its status message: `Connection refused` (not a timeout, not a 404 for a missing collection, not a 5xx from Qdrant). It has no `hits`, `scores` or `sources` metadata.
- There is no `generate` span at all: the turn never reached the LLM.

**Root cause:** nothing is listening where `QDRANT_URL` points. `kubectl -n qdrant get pods` shows no pod and the StatefulSet at `0/0`.

**Restore:** `make drill-qdrant-down-off` scales back to 1 and waits for the rollout. The PVC kept the data: `drill-check` shows `docs` with 3048 points, and the chatbot answers.

## 2. Embedding service slowed down

**Trigger:** `make drill-embedder-slow-on` adds `DRILL_EMBED_LATENCY_MS` (default 4000) to every `POST /embed`. It sets `EMBED_LATENCY_MS` on the embedder Deployment, a flag that `embedder:0.1.2` reads (default `0`, so normal runs are unaffected). The target refuses to run if the embedder isn't on that image: run `make embedder-up` once first.

**Symptoms:** answers still arrive and are correct, only slower. Nothing is an error: no 5xx, no WARNING, the retrieval scores and sources are the same as normal.

**Langfuse evidence** (`make drill-report DRILL=embedder-slow`):

| Trace | `chat` | `embed-query` | `retrieve` | `generate` |
|---|---|---|---|---|
| rag-eval-01 | 37.91 s | **4.02 s** | 0.02 s | 33.87 s |
| rag-eval-05 | 7.88 s | **4.03 s** | 0.01 s | 3.84 s |
| rag-eval-09 | 29.93 s | **4.03 s** | 0.01 s | 25.89 s (one 429, retried) |

- Baseline `embed-query` median is 0.03 s; here it's 4.02–4.03 s in all three traces, with almost no spread. A constant is the signature of an injected or timeout-like delay, not load.
- `retrieve` is unchanged (0.01 s) and the top-1 scores are the baseline's (mean 0.806), so Qdrant and the vectors are fine.
- **The total `chat` latency alone points at the wrong span.** Two of the three turns were slow because of the LLM (`generate` 34 s and 26 s, the free model on a bad minute), which hides a 4 s embedder delay. Only the per-observation view separates them: look for the span that is the same size every time, and compare each span with the same span in a normal trace.
- Item 05 is the clean case: `chat` 7.88 s of which `embed-query` is 4.03 s (half), `generate` 3.84 s.

**Root cause:** the embedder takes 4 s per request. `kubectl -n app get deploy embedder -o yaml` shows `EMBED_LATENCY_MS=4000`; the embedder pod itself is Ready and healthy, so only the trace gives it away. In production the same trace shape would mean a CPU-starved or throttled embedder, or a network problem on the way to it.

**Restore:** `make drill-embedder-slow-off` sets `EMBED_LATENCY_MS=0` and waits for the rollout. `drill-check` shows `embedder EMBED_LATENCY_MS: 0`.

## 3. Retrieval returning irrelevant chunks (wrong collection)

**Trigger:** `make drill-wrong-collection-on` seeds a small collection `recipes` (`scripts/drill_collection.py`: 15 passages about cooking, gardening, travel and music, embedded by the real embedder, with the same payload fields as `docs`) and sets `COLLECTION=recipes` on the chatbot Deployment. `docs` isn't touched. It stands for a deployment pointed at a stale or foreign collection: a valid collection with the right vector size, which answers every query with something.

**Symptoms:** no errors. Every answer to a Kubernetes question is "I don't know from the documentation" (the prompt's grounding rule works, so the model doesn't invent anything), after a fast turn: about 4 s, 416 prompt tokens. A user would report "the bot suddenly knows nothing". Two of the three first requests also failed with a 502 (RateLimitError 429), see drill 4; the run was resumed with a larger retry budget (`DRILL_LLM_RETRIES=7`) and only the failed items were asked again.

**Langfuse evidence** (`make drill-report DRILL=wrong-collection`), the `retrieve` observation of the rag-eval-09 trace `31106c76…`:

```
RETRIEVER retrieve  0.00s  collection=recipes top_k=5 hits=5 scores=[0.4454, 0.4287, 0.4182, 0.4078, 0.4005]
    sources=['fitness/marathon.md', 'garden/compost.md', 'music/guitar.md', 'recipes/sourdough.md', 'travel/packing.md']
```

- `retrieve.input.collection` is `recipes`, not `docs`. It's the first thing to compare.
- `hits=5` and level DEFAULT: retrieval "worked". That's why nothing is flagged.
- The scores give it away: top-1 **0.47** against **0.81** in the baseline for the same three questions (0.4–0.49 here, 0.71–0.86 there). A top-1 below about 0.6 means the question has no good match in the collection.
- The sources are `garden/…`, `recipes/…`, `travel/…`: not Kubernetes docs, and different pages from baseline (`workloads/controllers/replicaset.md` for item 01).
- Downstream effects, all consequences rather than causes: `chat-completion` input is 413 tokens against 1030 (the chunks are short), output is 9–16 tokens (the refusal), and the answer is the refusal sentence. The tokens look like "cheap and fast", which a dashboard would score as good.

**Root cause:** the chatbot searches the wrong collection. `kubectl -n app get deploy chatbot -o yaml` shows the `COLLECTION=recipes` override, which beats the `chatbot-config` ConfigMap (`docs`). A bad `filter` would show up in the same fields: right collection, but `sources` outside the expected pages. (The chatbot doesn't have a filter option, so only the collection was drilled.)

**Restore:** `make drill-wrong-collection-off` removes the override, waits for the rollout and deletes `recipes`. `drill-check` shows `docs` with 3048 points and a normal answer.

## 4. LLM timeouts and rate limits

Two faults in one drill, because the free model supplies the second one for free.

**Trigger:** `make drill-llm-timeout-on` sets `LLM_TIMEOUT=1` (`DRILL_LLM_TIMEOUT`) on the chatbot: the per-request timeout of the LLM call drops from 60 s to 1 s, shorter than any real GLM answer. The **429s** need no trigger. Z.AI's free `glm-4.7-flash` returns them all the time (code 1305 "service may be temporarily overloaded", sometimes 1302 "rate limit reached"), so they turned up in every drill's traffic and are used here as real evidence.

**Symptoms:** `POST /chat` returns **502** `LLM call failed: APITimeoutError` (or `RateLimitError 429`) after 7–10 s, from three LLM attempts (the drill's `DRILL_LLM_RETRIES=2`, so 3 attempts with 2 s and 4 s backoff). All three items failed. Without the timeout fault, a 429 is retried and usually the second attempt answers: 4 of the 6 answered turns of drills 2 and 3 needed at least one retry.

**Langfuse evidence** (`make drill-report DRILL=llm-timeout`), trace `22800823…` (rag-eval-01):

```
SPAN       chat             9.87s  ERROR: LLM call failed: APITimeoutError
RETRIEVER  retrieve         0.01s  hits=5 scores=[0.86, …]   <- retrieval is fine
SPAN       generate         9.85s  ERROR: gave up after 3 attempts
                                  errors=['attempt 1: RateLimitError 429', 'attempt 2: RateLimitError 429', 'attempt 3: APITimeoutError']
GENERATION chat-completion  0.95s  ERROR: Error code: 429 - {'error': {'code': '1305', 'message': 'The service may be temporarily overloaded, …
GENERATION chat-completion  0.65s  ERROR: Error code: 429 - …
GENERATION chat-completion  1.01s  ERROR: Request timed out.
```

- Everything before the LLM is healthy (`embed-query` 0.01 s, `retrieve` 0.01 s with normal scores). `generate` is the first ERROR, and it has `attempts` and `errors` in its metadata: one entry per try.
- **Each attempt is its own `chat-completion` generation**, and its status message tells the two failure kinds apart. A rate limit is an immediate `Error code: 429 - {'code': '1305' …}` (0.5–0.95 s), which is Z.AI answering. A timeout is `Request timed out.` after **exactly the timeout** (1.00 s, 0.99–1.01 s over the three traces), which is our client giving up. A timeout with a constant duration equal to a configured limit is a configuration fingerprint; real network timeouts vary.
- A 429 that is retried and wins is **not an ERROR**: the turn is level WARNING with "succeeded after 2 attempts" (see item 09 in drill 2, trace `9901c25c…`, and all three turns of drill 3). Filter `generate` by level WARNING to measure how often the model rate-limits. Here, 4 of the 6 answered turns of drills 2 and 3 did.
- Same fingerprint on the 429 side: trace `c0c6a126…` (drill 3) and `2a8f5d2f…` above show `gave up after 3 attempts` with three 429s and no timeout, i.e. a real Z.AI overload, nothing broken on our side. There is no usage on failed attempts, so they cost no tokens.
- `chat-completion` input `prompt=rag-chat v2` is recorded on each failed attempt too, so you can see which prompt was being tried.

**Root cause:** (a) timeouts: the client timeout (`LLM_TIMEOUT=1`) is below the model's latency. The answered turns elsewhere took 1–4 s for the final call and sometimes 20–50 s, so any timeout under about 60 s fails requests the provider would have answered. `kubectl -n app get deploy chatbot -o yaml` shows the override. (b) 429s: the provider's capacity on the free tier. The only levers are the retry count and backoff (`LLM_MAX_RETRIES`, `LLM_BACKOFF_BASE/MAX`; the experiment runner uses 7 retries up to 60 s), or a paid plan.

**Restore:** `make drill-llm-timeout-off` removes the override (back to the 60 s default) and waits for the rollout.

## 5. Token cost spike (huge context)

**Trigger:** `make drill-token-spike-on` sets `TOP_K=20` (`DRILL_TOP_K`) on the chatbot, four times the default 5: a config change that passes review because every answer still looks right. `-off` removes the override.

**Symptoms:** nothing visibly breaks. Answers are normal, retrieval scores are normal, and Langfuse shows **$0.00** for every turn because `glm-4.7-flash` is free. The signal is the token count, and latency when the model is slow (the final LLM call took 35 s on one turn, but 3–4.5 s on the other two, so latency is not a reliable signal here).

**Langfuse evidence** (`make drill-report DRILL=token-spike COMPARE=baseline`, the final `chat-completion` of each answered turn):

| Item | Prompt tokens, baseline (5 chunks) | Prompt tokens, drill (20 chunks) | Output tokens, baseline → drill |
|---|---|---|---|
| rag-eval-01 | 687 | **2930** | 57 → 84 |
| rag-eval-05 | 1072 | **3970** | 206 → 220 |
| rag-eval-09 | 1330 | **4791** | 164 → 209 |
| mean | 1030 | **3897** (3.8×) | 159 → 171 |

- `retrieve` shows `top_k=20 hits=20` and 20 scores. Against the baseline's `top_k=5`, that's the cause. The scores prove the extra chunks are mostly noise: the top-1 is the same (0.808 against 0.806), and ranks 6–20 fall from 0.77 to about 0.65.
- The generation's `usageDetails` carries `input`, `output` and `total`. Prompt tokens are the same four-fold step in every trace, while output tokens barely moved, so the growth is the context, not longer answers.
- The cost field is useless on the free model (`cost={"input":0,"output":0,"total":0}`), so the cost is estimated from the tokens at `glm-5.3-flash` prices ($0.15 per 1M input, $0.50 per 1M output; `drill-report` does this, and no paid call was made): **$0.00023 per answered turn in the baseline against $0.00067 in the drill, $0.23 against $0.67 per 1000 turns (2.9×)**. The cost rises less than the prompt (3.8×) because output tokens, priced at 3.3× the input, didn't grow and are a big share of the baseline bill. At 100,000 turns a month this fault is about $44 more. Cached input would lower both numbers a little (the price list has $0.03 per 1M for cached input; the drill ignores that discount).
- Failed attempts have no usage, so they didn't add to it: one request of the first pass (rag-eval-05, trace `bc958178…`, ERROR) was rate-limited three times and asked again, without any token cost.

**Root cause:** `TOP_K=20` on the chatbot Deployment (`kubectl -n app get deploy chatbot -o yaml`) overriding `TOP_K=5` in `chatbot-config`. The fix is the config, and the lesson is to alert on mean prompt tokens per turn, not on the dollar cost.

**Restore:** `make drill-token-spike-off` removes the override and waits for the rollout. `drill-check` shows `TOP_K: 5`.
