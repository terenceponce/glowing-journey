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
