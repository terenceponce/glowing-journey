# Vector store: Qdrant, embedder and ingestion

The RAG chatbot retrieves from a Qdrant collection called `docs`. It's filled by an ingestion Job that embeds chunks through an in-cluster embedding service.

```
services/ingest (Job) ──POST /embed──▶ embedder (app ns) ──fastembed / bge-small-en-v1.5
        │
        └──upsert──▶ qdrant (qdrant ns) ── collection "docs" (384-d, cosine)
```

| Component | Where | In-cluster URL |
|---|---|---|
| Qdrant v1.19.2 (chart `qdrant/qdrant` 1.19.2) | `qdrant` namespace, StatefulSet `qdrant`, PVC `qdrant-storage-qdrant-0` | `http://qdrant.qdrant.svc.cluster.local:6333` (REST), `:6334` (gRPC) |
| Embedder (`services/embedder`) | `app` namespace, Deployment + Service `embedder` | `http://embedder.app.svc.cluster.local:8000` |
| Ingest (`services/ingest`) | `app` namespace, Job `ingest` | – |

## Quick start

Prerequisite: the cluster from [cluster.md](cluster.md).

```sh
make qdrant-up      # helm install Qdrant (persistent), waits until Ready
make embedder-up    # build the embedder image (arm64), kind load, deploy, wait
make embedder-test  # /healthz + one /embed call from inside the cluster
make ingest         # build the ingest image with the corpus, run the Job, print its logs
make query Q="How do containers in the same Pod share storage?"   # top-5 chunks
make qdrant-ui      # http://localhost:6333/dashboard (Ctrl-C to stop)
```

## Qdrant

- Values: `deploy/qdrant/values.yaml`. Chart version is pinned in the `Makefile` (`QDRANT_CHART_VERSION`).
- Single replica. The chart's default cluster (Raft/p2p) mode is turned off, and so is telemetry.
- **Persistence:** the chart always creates a PVC from a StatefulSet `volumeClaimTemplate`. There's no `persistence.enabled` switch. We set it to 5Gi, though `local-path` doesn't enforce the size. `make qdrant-down` keeps the PVC, so collections survive a reinstall. `make qdrant-purge` deletes it.
- Resources: requests 100m / 256Mi, limit 1Gi. The `docs` collection uses roughly 100 MiB.
- **No API key.** Qdrant is only reachable inside the cluster or through `kubectl port-forward`. If one is added later, the chart takes `apiKey.valueFrom.secretKeyRef`. `ingest.py` and `query.py` already read an optional `QDRANT_API_KEY`.

## Embedding service

`services/embedder/app.py`, FastAPI + [fastembed](https://github.com/qdrant/fastembed) 0.9.0, model `BAAI/bge-small-en-v1.5` (384 dimensions, ONNX, quantized, 67 MB).

| Endpoint | Request | Response |
|---|---|---|
| `GET /healthz` | – | `{"status": "ok", "model": "...", "dim": 384}` |
| `POST /embed` | `{"texts": ["..."], "kind": "passage" \| "query"}` | `{"model": "...", "dim": 384, "vectors": [[...], ...]}` |

`kind: "query"` prepends `QUERY_PREFIX`, which the Deployment sets to BGE's retrieval instruction (`Represent this sentence for searching relevant passages: `). Use it for user questions. Use `passage` (the default) for documents.

- **Model baked into the image.** The Dockerfile downloads the model into `/models` at build time and sets `HF_HUB_OFFLINE=1`, so the pod starts with no network access. This was verified with `docker run --network none`. To change the model, pass `--build-arg EMBED_MODEL=...` and re-ingest, since the dimension may change.
- Env: `EMBED_THREADS` (2), `MAX_BATCH` (256 texts per request), `ONNX_BATCH_SIZE` (16 texts per ONNX call), `QUERY_PREFIX`.
- Resources: requests 100m / 256Mi, limits 2 CPU / 1Gi. It idles at about 165 MiB and peaks around 360 MiB.
- Image `embedder:0.1.1`, about 630 MB. It's built for `linux/arm64` and loaded with `kind load docker-image`. Bump `EMBEDDER_IMAGE` in the Makefile when the code changes.

## Corpus

The Kubernetes **concepts** docs: `content/en/docs/concepts` from [kubernetes/website](https://github.com/kubernetes/website), pinned to commit `cb2e97a15992f47dba976a47ccca071e4d167464` (main on 2026-10-12). That's 187 markdown files, about 2.9 MB.

The ingest Dockerfile fetches only that directory at build time (shallow, blob-less fetch with sparse checkout) and copies it to `/corpus`. The Job therefore needs no network or GitHub access, and the same image always ingests the same text. To change the corpus, set `CORPUS_REPO`, `CORPUS_REF` or `CORPUS_PATH` (Makefile variables, passed as build args).

## Chunking

`services/ingest/chunking.py`, for each file:

1. Parse the YAML front matter and use its `title`.
2. Clean the Hugo markup: drop HTML comments, replace `{{< glossary_tooltip text="X" ... >}}` with `X`, remove other shortcode tags (`{{< note >}}`, `{{% code_sample %}}`, …) but keep their inner text, turn `[text](url)` into `text`, and drop images.
3. Split into sections on markdown headings, ignoring `#` lines inside code fences such as shell comments. Each chunk records its section heading.
4. Within a section, pack paragraphs into chunks of at most `CHUNK_SIZE` characters. Each new chunk starts with the last `CHUNK_OVERLAP` characters of the previous one, cut at a word boundary. A paragraph longer than `CHUNK_SIZE` is cut into overlapping windows. Chunks under 50 characters are dropped.

| Setting | Default | Notes |
|---|---|---|
| `CHUNK_SIZE` | 1200 characters | About 300 tokens, well under bge-small's 512-token limit (longer input is truncated) |
| `CHUNK_OVERLAP` | 200 characters | Must be smaller than `CHUNK_SIZE` |

With the defaults, 187 files yield **3048 chunks** (median 685 characters).

The **vector** is computed from `title + section + chunk text`, so short chunks keep their context. The payload stores the raw chunk text only.

## Collection `docs`

- Vectors: 384 dimensions, cosine distance. The size is taken from the embedder's response, not hardcoded.
- Point ID: UUIDv5 of `"<source>#<chunk_index>"`, so it's deterministic.
- Payload:

| Field | Example |
|---|---|
| `source` | `content/en/docs/concepts/workloads/pods/_index.md` (path in the corpus repo) |
| `title` | `Pods` |
| `section` | `Storage in Pods` (nearest heading, `""` before the first one) |
| `chunk_index` | `18` (position within the file) |
| `text` | the cleaned chunk text |
| `corpus_ref` | the corpus commit |

## Re-ingesting

```sh
make ingest                                      # same settings
make ingest CHUNK_SIZE=800 CHUNK_OVERLAP=100     # different chunking
make ingest CORPUS_REF=<newer commit>            # newer docs (rebuilds the image)
```

`make ingest` rebuilds and loads the ingest image, writes the `ingest-config` ConfigMap from the Makefile variables (`COLLECTION`, `CHUNK_SIZE`, `CHUNK_OVERLAP`, `EMBEDDER_URL`, `QDRANT_URL`), replaces the `ingest` Job, waits for it to finish and prints its logs. The Job **drops and recreates the collection** (`RECREATE_COLLECTION=true`), so changing the chunk settings leaves no stale points behind. During the roughly 3.5-minute run, queries see a partial collection. A full ingest of 3048 chunks takes about 3.5 minutes, and nearly all of that time is embedding on the CPU.

## Example query

```
$ make query Q="How do containers in the same Pod share storage?"
1. score=0.842  content/en/docs/concepts/workloads/pods/_index.md  [Pods > Storage in Pods] #18
   A Pod can specify a set of shared storage volumes. All containers in the Pod can access the shared volumes, ...
2. score=0.814  content/en/docs/concepts/workloads/pods/_index.md  [Pods > Pods with multiple containers] #24
3. score=0.805  content/en/docs/concepts/workloads/pods/_index.md  [Pods > What is a Pod?] #2
4. score=0.804  content/en/docs/concepts/workloads/pods/_index.md  [Pods > What is a Pod?] #1
5. score=0.795  content/en/docs/concepts/workloads/pods/_index.md  [Pods > Pods with multiple containers] #25
```

Other checks: "What is the difference between a Deployment and a StatefulSet?" returns `statefulset.md` and `controllers/_index.md` first. "How do I limit how much CPU a namespace can use?" returns `limit-range.md` and `resource-quotas.md`.

`make query` runs `services/ingest/query.py` in a throwaway pod (ingest image). It embeds the question with `kind: "query"` and calls Qdrant `query_points`, which is the same path the chatbot will use.

## Surprises

- **The embedder was OOM-killed on the first ingest.** fastembed's `embed()` defaults to `batch_size=256` and pads each batch to its longest text (up to 512 tokens). One `/embed` call with 64 chunks of about 1200 characters went over the 1Gi limit. The fix is passing `batch_size=ONNX_BATCH_SIZE` (16). Peak memory is now about 360 MiB with no loss of throughput on CPU.
- **`kubectl wait --for=condition=Complete` hangs on a failed Job** until it times out, because it can't wait for "Complete OR Failed". `make ingest` polls the Job conditions instead and prints the logs on failure.
- **The Qdrant chart has no `persistence.enabled`.** Storage is always a PVC. It also enables cluster/p2p mode by default, even with one replica.
- **The Qdrant image has no `curl` or `wget`**, so you can't inspect it with `kubectl exec ... curl`. Use `make qdrant-ui` (port-forward) or `make query`.
- **bge-small's query prefix** is described as "not so necessary" in fastembed's model list. We apply BGE's retrieval instruction for queries anyway, because the model card recommends it for short questions against passages.
- The ONNX Runtime prints a harmless `Failed to persist telemetry device ID` warning at startup, because the non-root user has no home directory.
- Docker memory: the kind node went from about 4.7 GB to about 6.6 GB with Qdrant, the embedder and the page cache from the ingest run.
