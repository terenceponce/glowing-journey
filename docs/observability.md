# Observability: tracing the chatbot with Langfuse

Every `POST /chat` produces one nested trace in the self-hosted Langfuse ([langfuse.md](langfuse.md)). It records latency per step, the retrieval query and results, the exact LLM input and output, tokens, cost and the prompt version. Turns of one conversation are grouped into a Langfuse session. Users can rate each answer, and the rating is stored as the score `user-feedback` on that turn's trace ([below](#user-feedback-score)).

## Quick start

```sh
make chatbot-up                                   # also runs chatbot-langfuse-setup (model prices + prompt)
make chatbot-test SESSION=demo Q="What is a Pod?" # prints trace_id at the end
make chatbot-test SESSION=demo Q="How do I scale it?"
make chatbot-trace TRACE=<trace_id>               # the trace as a tree plus its scores, read back from the Langfuse API
make chatbot-feedback TRACE=<trace_id> VOTE=down COMMENT="Missed the point"  # rate it like the chat page does
make langfuse-ui                                  # http://localhost:3000 → Tracing / Sessions
```

| Target | What |
|---|---|
| `chatbot-langfuse-setup` | Runs `services/chatbot/langfuse_setup.py` as a Job (chatbot image, `langfuse-api` Secret). Creates or updates the GLM model prices and the `rag-chat` prompt. Idempotent: it prints "up to date" when nothing changed. `chatbot-up` runs it. |
| `chatbot-config` | Writes the `chatbot-config` ConfigMap, now including the Langfuse settings below |
| `chatbot-trace TRACE=<id>` | Runs `show_trace.py` in the chatbot pod: observation tree with latency, level, model, usage, cost, prompt version, retry attempts and the retrieval query, then the trace's scores |
| `chatbot-feedback TRACE=<id> VOTE=up\|down [COMMENT=...]` | Calls `POST /feedback` from inside the chatbot pod (metadata `source: make`), then polls `/api/public/v3/scores` until the score shows that value and comment (fails after 30 s) |
| `chatbot-test SESSION=<id> Q=...` | Same `SESSION` = same conversation. Prints `trace_id` |

## Trace model

```
trace "chat"   user_id, session_id, tags [env:lab, version:0.4.0, model:glm-4.7-flash], version 0.4.0, environment lab
│                score user-feedback (NUMERIC, 1 = 👍, 0 = 👎) + optional comment, once the user votes
└─ SPAN chat                  input {message, history_turns}  output {answer, sources}  metadata {message_id}
   ├─ EMBEDDING embed-query   input {text, kind: query}  output {dim: 384}  model BAAI/bge-small-en-v1.5
   ├─ RETRIEVER retrieve      input {query, top_k, collection}
   │                          output [{n, chunk_id, score, source, title, section, chunk_index}, ...]
   │                          metadata {top_k, hits, chunk_ids, scores, sources}
   └─ SPAN generate           metadata {attempts, errors, prompt_name, prompt_version, prompt_fallback}
      └─ GENERATION chat-completion   one per attempt (from the langfuse.openai wrapper)
                                      model, model parameters, full messages in, answer out,
                                      usage {input, input_cached_tokens, output, ...}, cost,
                                      linked prompt rag-chat vN, metadata {attempt}
```

- **Root span `chat`** (`app.py`) wraps the request. Directly inside it, `propagate_attributes(user_id, session_id, tags, version, trace_name)` copies these trace attributes onto every child observation. That's how Langfuse v4 filters and aggregates by user and session.
- **`embed-query` and `retrieve`** are separate observations of types `embedding` and `retriever`, so the UI shows the right icons and the timings are split. `retrieve.input.query` is the exact text sent to the vector search. Today it's only the latest message, which makes follow-up questions retrieve poorly (see below).
- **`generate` is a span and the generations are its children.** The Z.AI free model often returns HTTP 429 (code 1305). The OpenAI SDK's own retries are turned off (`max_retries=0`), and `rag.generate()` retries up to `LLM_MAX_RETRIES` (3) times, with 1, 2, 4… s backoff and jitter. Each attempt is its own `chat-completion` generation with `metadata.attempt`. Failed attempts have level `ERROR` and the 429 body as their status message. When retries were needed, `generate` gets level `WARNING` ("succeeded after N attempts"). When all attempts fail, it gets `ERROR`, and so does `chat` (with the 502 detail).
- **Cost** is computed by Langfuse from the generation's model and usage, using the custom model definitions (below). The trace's total cost is the sum of its generations.
- **The prompt version** is on the generation (the "Prompt" link in the UI, `promptName`/`promptVersion` in the API). It's also in `generate.metadata`, so a trace still says when the built-in fallback was used (`prompt_fallback: true`, version `null`).

### Example: one turn

`make chatbot-test Q="What is the difference between a Deployment and a StatefulSet?"` then `make chatbot-trace`:

```
trace b771407c91beb058c3076fbb8b27b5cc
  userId: make-test
  sessionId: session-demo-1791869725
  environment: lab
  version: 0.2.0
  - SPAN       chat  5.36s
    - EMBEDDING  embed-query  0.02s  model=BAAI/bge-small-en-v1.5
    - RETRIEVER  retrieve  0.01s  chunk_ids=["96f2f3e8-…", …]  scores=[0.8553,0.8326,0.8305,0.8119,0.7904]
      query: {"query": "What is the difference between a Deployment and a StatefulSet?", "top_k": 5, "collection": "docs"}
    - SPAN       generate  5.33s  level=WARNING (succeeded after 2 attempts)  attempts=2  errors=["attempt 1: RateLimitError 429"]
      - GENERATION chat-completion  1.16s  level=ERROR (Error code: 429 - {'error': {'code': '1305', …}})  model=glm-4.7-flash  prompt=rag-chat v2  attempt=1
      - GENERATION chat-completion  3.24s  model=glm-4.7-flash  usage={"input":700,"output":153,"total":944,"input_cached_tokens":91,"output_reasoning_tokens":0}
                                           cost={"input":0,"output":0,"input_cached_tokens":0,"total":0}  prompt=rag-chat v2  attempt=2
```

In the UI (Tracing → the trace), the same tree appears in the left pane with latency bars. The header shows the session, user, tags, environment `lab`, release version and total cost ($0.00 on the free model). Clicking `chat-completion` shows the full message list, the answer, token counts, cost and a link to `rag-chat` version 2.

### Example: a conversation (session)

Three questions with the same `SESSION`. The third question failed once with a 502 (four 429s in a row) and was asked again, so the session has four traces:

| Trace | Input | Level | Notes |
|---|---|---|---|
| `b771407c…` | What is the difference between a Deployment and a StatefulSet? | DEFAULT | 1 retry, top score 0.855 |
| `eeb7ef71…` | Which one should I use for a database? (`history_turns: 1`) | DEFAULT | 1 retry, top score 0.687: weak retrieval |
| `171a452f…` | How do I scale it? | ERROR | "LLM call failed: RateLimitError 429" after 4 attempts |
| `e6ca9ce3…` | How do I scale it? (asked again) | DEFAULT | 3 retries. Retrieved only Horizontal Pod Autoscaling chunks (scores about 0.65) |

In the UI, **Sessions → `session-demo-…`** shows these traces in order as one conversation, with each input and answer, plus the session's total cost and duration. The follow-up weakness from #4 is easy to see: `retrieve.input.query` is just "How do I scale it?". The history isn't used, so retrieval can't know that "it" means a StatefulSet. Retrieved chunk IDs and scores are recorded per turn, which makes it easy to compare against a future query-rewriting step.

## User feedback score

Each answer on the chat page has 👍 / 👎 buttons. After a vote, a comment box appears ("Send comment" or Enter). The page sends `POST /feedback`:

```json
{"trace_id": "<ChatResponse.trace_id>", "value": 1, "comment": "optional, ≤ 1000 chars", "user_id": "user-…"}
```

`value` must be `1` (up) or `0` (down), and `trace_id` must be 32 hex characters (otherwise 422). The chatbot records the Langfuse score:

| Field | Value |
|---|---|
| name | `user-feedback` |
| id | `user-feedback-<trace_id>`: one feedback score per turn |
| value / data type | `1` = up, `0` = down, `NUMERIC` (so averages and `user-feedback < 1` filters work) |
| target | the trace (not an observation) |
| comment | the user's comment, or empty |
| metadata | `{user_id, source}`: `source` is `chat-page` or `make` |
| environment | `lab`, the SDK's tracing environment, same as the trace |

The score goes through the SDK's background queue like the spans (`langfuse.create_score`), so `POST /feedback` returns at once, and a dead Langfuse doesn't break the page.

**Changing a vote updates the score.** Langfuse overwrites a score only when the **id, name and date of the timestamp** all match. In ClickHouse, `scores` is a `ReplacingMergeTree` ordered by `(project_id, toDate(timestamp), name, id)`. So `/feedback` first looks the score up with `GET /api/public/v3/scores?id=…` and reuses its timestamp, and it only uses "now" for the first vote. It also always sends the whole score, including an empty comment (see Surprises).

### Reading scores back

`/api/public/scores` and `/api/public/v2/scores` return 404 "not available … in Langfuse v4 events_only mode". **`GET /api/public/v3/scores`** works and filters by `traceId`, `id`, `name`, `sessionId`, `value`, timestamps and more. It returns only core fields unless asked: `fields=details` adds `comment` and `metadata`, and `fields=subject` adds what the score is attached to. `make chatbot-trace` and `make chatbot-feedback` use it.

Example: vote 👍, then change it to 👎 with a comment, then back to 👍 without one (`make chatbot-feedback`, trace `3f9ce76a…`):

```
  - user-feedback=1  NUMERIC  on trace  id=user-feedback-3f9ce76a…  metadata={"user_id":"make-test","source":"make"}  timestamp=2026-10-13T06:56:59.801Z
  - user-feedback=0  NUMERIC  on trace  id=user-feedback-3f9ce76a…  comment="Too long, missed the shared network part"  …  timestamp=2026-10-13T06:56:59.801Z
  - user-feedback=1  NUMERIC  on trace  id=user-feedback-3f9ce76a…  metadata={"user_id":"make-test","source":"make"}  timestamp=2026-10-13T06:56:59.801Z
```

Each read returned exactly one score with the first vote's timestamp. A read-only ClickHouse query showed two physical rows (`SELECT … FROM scores WHERE trace_id=…`) until a background merge, but one with `FINAL` (`value 1`, empty comment), which is what the API and UI show. Flipping a vote about 1 s after the first one (before the first was ingested) also ended in one score: the lookup found nothing and used "now", which falls on the same date.

In the Langfuse UI, the score belongs to the trace (Tracing → the trace). Per the Langfuse docs, the traces table can be filtered by `user-feedback < 1` to find disliked answers. The chat page's own requests (`POST /chat`, then `POST /feedback` with `source: chat-page`) were also replayed with curl through `make chatbot-ui`, with the same results.

**Click test in the browser** (`make chatbot-ui`, Chrome): asked "What is a Service in Kubernetes?" and clicked 👍. The page showed 👍 selected and "Thanks!", and `/api/public/v3/scores` had `user-feedback=1` on trace `71cbbadd…` (`metadata.source: chat-page`). Then clicked 👎 and sent the comment "No example of a Service manifest". That is three `POST /feedback` calls (up; down; down + comment, per the chatbot log). Afterwards the API, and ClickHouse with `FINAL`, showed one score: value `0`, that comment, and the timestamp of the first click.

## Prompt management

- The prompt is the chat prompt **`rag-chat`** with two messages. The `system` message holds the rules, and the `user` message has `{{context}}` and `{{question}}`. History is inserted between them. Source: `services/chatbot/prompts.py`.
- `make chatbot-langfuse-setup` pushes it. If it differs from the current `production` version, a new version is created and labelled `production`. To change the prompt, either edit `prompts.py` and run the target, or edit it in the Langfuse UI and move the `production` label. The chatbot follows the label (`LANGFUSE_PROMPT_LABEL`), so promoting a version in the UI takes effect without a redeploy.
- At runtime `rag.get_prompt()` calls `langfuse.get_prompt(..., cache_ttl_seconds=60, fallback=prompts.RAG_CHAT_PROMPT, max_retries=0, fetch_timeout_seconds=2)`. After the first fetch (done at startup), it's served from memory. Once it's stale, the SDK returns the cached version and refreshes it in the background. If Langfuse is unreachable and nothing is cached, it uses the built-in prompt. It then doesn't retry for 30 s, so requests don't each wait 2 s for a timeout.
- The prompt is linked to the generation through the wrapper's `langfuse_prompt=` argument. The SDK doesn't link fallback prompts.

## Cost: custom model definitions

`langfuse_setup.py` creates these through `POST /api/public/models` (one default pricing tier each). Prices are from [Z.AI pricing](https://docs.z.ai/guides/overview/pricing):

| Model | Match pattern | input | input_cached_tokens | output |
|---|---|---|---|---|
| `glm-4.7-flash` | `(?i)^(glm-4\.7-flash)$` | $0 | $0 | $0 |
| `glm-5.3-flash` | `(?i)^(glm-5\.3-flash)$` | $0.15 / 1M | $0.03 / 1M | $0.50 / 1M |

The script compares the existing custom definitions with these. If they differ, it deletes them and creates new ones. Otherwise it leaves them alone.

Z.AI returns OpenAI-style usage (`prompt_tokens_details.cached_tokens`). Langfuse maps that to `input_cached_tokens` and subtracts it from `input`, so the cached-input price needs exactly that key. Checked without spending money: a synthetic `glm-5.3-flash` generation with 1M tokens of each usage type was costed at input $0.15, cached $0.03, output $0.50, total $0.68. `glm-4.7-flash` turns show $0 with the custom model matched (`internalModelId` set).

## Configuration

| Env var | Makefile var | Default | Source |
|---|---|---|---|
| `LANGFUSE_BASE_URL` | `LANGFUSE_URL` | `http://langfuse-web.langfuse.svc.cluster.local:3000` | `chatbot-config` |
| `LANGFUSE_TRACING_ENVIRONMENT` | `LANGFUSE_ENVIRONMENT` | `lab` | `chatbot-config` (also the `env:` tag) |
| `LANGFUSE_PROMPT_LABEL` | `LANGFUSE_PROMPT_LABEL` | `production` | `chatbot-config` |
| `LANGFUSE_PROMPT_CACHE_TTL` | – | `60` | Code default |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | – | – | Secret `app/langfuse-api` (`envFrom`) |

The app version (the `version:` tag and the trace's version) comes from `services/chatbot/pyproject.toml` (`0.4.0`). The examples above were recorded with 0.2.0. Bump it together with `CHATBOT_IMAGE`.

## Resilience

- **Langfuse down.** Tracing is exported in the background by the OpenTelemetry batch processor, so a dead Langfuse only causes `Transient error … exporting spans` warnings in the log, and the spans are dropped. Prompts fall back as described above. Tested by pointing `LANGFUSE_BASE_URL` at a non-existent host (`kubectl set env`, reverted afterwards): chats returned 200 with `prompt=rag-chat@None` in the log, and latency didn't change.
- **Shutdown.** The FastAPI lifespan calls `langfuse.shutdown()` after uvicorn stops on SIGTERM, which flushes queued spans. Tested with `LANGFUSE_FLUSH_INTERVAL=600` (so nothing is sent on the normal schedule): one chat, then `kubectl delete pod`. The log showed `flushing Langfuse`, and the full trace was in Langfuse afterwards.

## Surprises

- **FastAPI 0.143 has built-in OpenTelemetry.** Once the Langfuse SDK registers a global tracer provider, FastAPI starts creating `POST` / `fastapi.endpoint` spans. Langfuse's export filter drops them because they aren't from an LLM library, but `chat` still pointed at them as its parent, so the root had a `parentObservationId` that doesn't exist. It's turned off with `FastAPI(telemetry={"tracing": False, "metrics": False, "logs": False})`.
- **The OpenAI SDK's own retries are invisible to Langfuse.** The wrapper wraps `create()`, and the SDK retries inside it, so a 429-then-success looks like one slow generation. That's why the retry loop is in our code now.
- **429s are frequent on glm-4.7-flash.** Many test turns needed 1–3 retries, and one failed all 4 attempts (about 8 s of backoff in total). Once, a single successful call took 53 s. These are now plain to see in the traces (level `WARNING`/`ERROR`, latency per attempt). Tuning retries is left for later.
- **Langfuse v4 events_only mode limits the read API.** `/api/public/traces`, `/api/public/sessions/{id}` and `/api/public/metrics/daily` all return "not available … in Langfuse v4 events_only mode". `GET /api/public/v2/observations` works with `traceId=` or `sessionId=`, but it doesn't return tags or the trace name. Those are stored, though: a read-only ClickHouse query on `events_core` showed `trace_name: chat` and the three tags on every observation. Use the UI to see them.
- **Stored chat prompts come back with an extra `"type": "message"`** on each message, so the setup script compares only role and content. Before that fix, a re-run created a needless version 2.
- **The SDK's `get_prompt` docstring says `fetch_timeout_seconds` is in milliseconds.** The code passes it as seconds.
- **A score update needs the same id, name *and* date.** The Langfuse docs say a matching id alone isn't enough, and the ClickHouse sort key shows why. A vote changed on a later day would create a second score unless the original timestamp is reused, which is why `/feedback` looks it up first.
- **`create_score(comment=None)` doesn't clear a comment.** The SDK leaves `None` fields out of the ingestion event, and Langfuse merges the missing fields from the stored score (a "partial update", which the docs call deprecated). Changing 👎 + comment to 👍 without a comment kept the old comment until `/feedback` started sending `comment: ""`.
- **The v3 scores API returns core fields only.** Without `fields=details`, `comment` and `metadata` are missing from the response, which looks as if they weren't stored.
- **The OpenAI wrapper works with openai 3.29**, although the Langfuse docs only mention 1.x. `chat.completions.create` has no version gate; only `parse` (≥ 1.50 / 1.92) and the Responses API (≥ 1.66) do. `name`, `metadata` and `langfuse_prompt` are stripped before the request goes to Z.AI.
