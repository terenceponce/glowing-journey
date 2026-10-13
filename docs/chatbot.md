# RAG chatbot

A FastAPI app in the `app` namespace. It answers questions about Kubernetes from the Qdrant `docs` collection (see [vector-store.md](vector-store.md)) using Z.AI's GLM through the `openai` SDK. Every turn is traced in Langfuse; see [observability.md](observability.md).

```
browser ──port-forward :8080──▶ chatbot (app ns, :8000)
                                  │ 1. POST /embed {kind: "query"} ──▶ embedder (app ns)
                                  │ 2. query_points top-k ───────────▶ qdrant (qdrant ns), collection "docs"
                                  │ 3. chat.completions ─────────────▶ https://api.z.ai/api/paas/v4 (glm-4.7-flash)
                                  ◀ answer + cited sources
```

## Quick start

Prerequisites: the cluster, Qdrant, the embedder and an ingested `docs` collection ([cluster.md](cluster.md), [vector-store.md](vector-store.md)). You also need a Z.AI API key in `secrets/zai.env` (git-ignored):

```sh
umask 077; printf 'ZAI_API_KEY=%s\n' '<your key>' > secrets/zai.env
```

```sh
make chatbot-up       # zai-api Secret + build image (arm64) + kind load + chatbot-config + Langfuse setup + deploy, waits until Ready
make chatbot-test     # one question through POST /chat from inside the cluster (make chatbot-test Q="..." SESSION=...)
make chatbot-trace    # print a turn's Langfuse trace (make chatbot-trace TRACE=<trace_id>)
make chatbot-ui       # chat page at http://localhost:8080 (Ctrl-C to stop)
make chatbot-logs     # follow logs
make chatbot-status   # pod, service and the current chatbot-config
make chatbot-down     # remove Deployment + Service (keeps Secret and ConfigMap)
```

## Code: `services/chatbot/`

| File | What |
|---|---|
| `rag.py` | `retrieve(question)` embeds the question with `kind: "query"` and returns the top-k `Chunk`s from Qdrant. `get_prompt(...)` fetches the `rag-chat` prompt from Langfuse and fills in the context and question. `build_messages(...)` puts the history between its system and user messages. `generate(messages, prompt)` calls the LLM, with retries, and returns the answer and token usage. Each step is a Langfuse span ([observability.md](observability.md)). |
| `prompts.py` | The `rag-chat` prompt: the source for Langfuse and the fallback when Langfuse is unreachable |
| `langfuse_setup.py` | Model prices and the prompt in Langfuse (`make chatbot-langfuse-setup`) |
| `show_trace.py` | Prints a trace tree (`make chatbot-trace`) |
| `app.py` | HTTP layer: `POST /chat`, `GET /healthz`, `GET /` (static page) and the in-memory session history. |
| `static/index.html` | Minimal chat page. It keeps `user_id` in `localStorage` and `session_id` in `sessionStorage`, and "New conversation" starts a new session. Each answer has a collapsible source list and an empty `.feedback` slot for the thumbs up/down buttons that come later. |

### API

`POST /chat`

```json
{"session_id": "abc", "user_id": "user-1", "message": "How do containers in the same Pod share storage?"}
```

The response is below. `message_id` is there so feedback can be attached to an answer later.

```json
{
  "message_id": "uuid",
  "trace_id": "0b52d107871054814e88d8fdce37fac9",
  "session_id": "abc",
  "answer": "... [1].",
  "model": "glm-4.7-flash",
  "sources": [{"n": 1, "score": 0.842, "source": "content/en/docs/concepts/workloads/pods/_index.md",
               "title": "Pods", "section": "Storage in Pods", "chunk_index": 18, "snippet": "..."}],
  "usage": {"prompt_tokens": 943, "completion_tokens": 47, "total_tokens": 990, "...": "..."}
}
```

The service returns `502` with a short `detail` if retrieval (embedder/Qdrant) or the LLM call fails, and `422` for an invalid body.

### Prompt and history

- The prompt (`rag-chat` in Langfuse, source in `prompts.py`) says to answer only from the numbered passages, cite them as `[n]`, and say "I don't know from the documentation" when the passages don't cover the question.
- The current question is sent with its top-k passages. History holds only the earlier questions and answers, not their passages, to keep prompts small: about 900–1500 prompt tokens per turn.
- History is per `session_id`, held in memory. The last `MAX_HISTORY_TURNS` (6) pairs are kept, for at most `MAX_SESSIONS` (1000) sessions, with the oldest dropped first. A pod restart clears it. That's also why the Deployment runs one replica.
- `user_id` and `session_id` are set on the Langfuse trace.

## Config

Most settings come from the `chatbot-config` ConfigMap, which `make chatbot-up` writes from Makefile variables in the same way as `ingest-config`. Override them on the command line. The values stay in place until the next `make chatbot-up` without the override.

| Env var | Makefile var | Default | Notes |
|---|---|---|---|
| `LLM_BASE_URL` | `LLM_BASE_URL` | `https://api.z.ai/api/paas/v4` | Z.AI's general API. **Not** the coding-plan endpoint `/api/coding/paas/v4`, whose terms limit it to coding tools. |
| `LLM_MODEL` | `LLM_MODEL` | `glm-4.7-flash` | Free on Z.AI. Switch with `make chatbot-up LLM_MODEL=glm-5.3-flash`. **glm-5.3-flash is paid**; see Surprises. |
| `LLM_TEMPERATURE` | `LLM_TEMPERATURE` | `0.2` | |
| `LLM_MAX_TOKENS` | `LLM_MAX_TOKENS` | `1024` | |
| `LLM_THINKING` | `LLM_THINKING` | `disabled` | Z.AI's `thinking.type`, sent via `extra_body`. `enabled` adds reasoning tokens and latency. |
| `TOP_K` | `TOP_K` | `5` | Chunks retrieved per question |
| `COLLECTION` | `COLLECTION` | `docs` | |
| `EMBEDDER_URL` | `EMBEDDER_URL` | `http://embedder.app.svc.cluster.local:8000` | |
| `QDRANT_URL` | `QDRANT_URL` | `http://qdrant.qdrant.svc.cluster.local:6333` | `QDRANT_API_KEY` is optional and not set |
| `LLM_TIMEOUT`, `LLM_MAX_RETRIES` | – | `60`, `3` | Code defaults. `rag.generate()` retries 429/5xx/connection errors with backoff, and each attempt shows up in the trace. |
| `LANGFUSE_*` | `LANGFUSE_URL`, `LANGFUSE_ENVIRONMENT`, `LANGFUSE_PROMPT_LABEL` | | See [observability.md](observability.md). Keys come from Secret `app/langfuse-api`. |
| `MAX_HISTORY_TURNS`, `MAX_SESSIONS` | – | `6`, `1000` | Code defaults |
| `ZAI_API_KEY` | – | – | From Secret `app/zai-api` |

**The API key:** `make chatbot-secret`, which `chatbot-up` also runs, calls `scripts/zai-secret.sh`. The script copies only the `ZAI_API_KEY=` line from `secrets/zai.env` into the Secret `app/zai-api`, the same way `scripts/langfuse-secrets.sh` works. It never prints the key, and the app never logs it. To rotate the key, edit the file and run `make chatbot-up`. The rollout restart picks up the new value.

## Redeploying

```sh
make chatbot-up                              # after code changes: rebuilds, reloads, restarts
make chatbot-up LLM_MODEL=glm-5.3-flash      # config change only (still rebuilds; it's cached and quick)
```

`chatbot-up` always runs `rollout restart`, because the image tag (`CHATBOT_IMAGE`, `chatbot:0.2.0`) stays the same between builds and `kind load` replaces the image on the node in place. Bump the tag in the `Makefile` when you release a meaningful change, as with the other services.

Footprint: the image is about 390 MB. The pod uses about 100 MiB (requests 50m / 128Mi, limit 512Mi). The kind node stayed at about 6.7 GB.

## Example

```
$ make chatbot-test
Q: How do containers in the same Pod share storage?
A: Containers in the same Pod share storage by using volumes specified in the Pod. All containers within
   the Pod can access these shared volumes, which allows them to share data and ensures that persistent
   data survives if a container is restarted [1].

[1] 0.842 content/en/docs/concepts/workloads/pods/_index.md [Pods > Storage in Pods] #18
[2] 0.814 content/en/docs/concepts/workloads/pods/_index.md [Pods > Pods with multiple containers] #24
[3] 0.805 content/en/docs/concepts/workloads/pods/_index.md [Pods > What is a Pod?] #2
...
model glm-4.7-flash usage {'completion_tokens': 47, 'prompt_tokens': 943, 'total_tokens': 990, ...}
```

Latency is about 3–4 s per turn, almost all of it the LLM call. Embedding and Qdrant take about 15 ms. Asking "What is the capital of France?" returns "I don't know from the documentation."

## Surprises

- **Z.AI returns HTTP 429 with code `1305` ("The service may be temporarily overloaded")** on the free model now and then, even for the very first request. The key itself was fine, and the retry succeeded. The chatbot retries (`LLM_MAX_RETRIES=3`, exponential backoff). Since #5 the retry loop is in `rag.generate()` instead of the SDK, so every attempt is visible in Langfuse. If it keeps happening, the user sees a 502 `LLM call failed: RateLimitError 429`.
- **`glm-5.3-flash` is not free.** Z.AI's pricing page lists it at $0.15 per 1M input tokens and $0.50 per 1M output tokens. Only `glm-4.7-flash` is listed as free. Switching is just config, but it costs money.
- **Thinking mode is off explicitly** (`thinking: {type: disabled}`). The test calls showed `reasoning_tokens: 0` with it off. It's a Z.AI extension to the OpenAI schema, so it goes through `extra_body`.
- **Follow-up questions retrieve on their own words only.** "Which one should I use for a database?" after a Deployment-vs-StatefulSet question finds weaker chunks, such as the common-labels WordPress/MySQL example. The model still answers sensibly from history, but sometimes cites a passage for a claim that came from the earlier turn. Query rewriting (condensing history plus the question before retrieval) would fix this and would be a good thing to observe in Langfuse.
- **openai SDK 3.x** (3.29.0) still has `client.chat.completions.create` with `base_url`. Its HTTP log lines show up under the `httpx2` logger and include only the URL.
- The answer is markdown, such as `**bold**` and bullets, but the page shows it as plain pre-wrapped text. That's readable, and it avoids adding a markdown renderer or an HTML-injection risk to a minimal page.
