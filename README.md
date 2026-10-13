# glowing-journey

A hands-on lab for practicing **AI observability**: a RAG chatbot running on a local Kubernetes cluster, traced and evaluated with self-hosted [Langfuse](https://langfuse.com).

## Stack

| Layer | Choice |
|---|---|
| Cluster | [kind](https://kind.sigs.k8s.io/) + [Helm](https://helm.sh/) |
| Observability | Langfuse (self-hosted, official Helm chart) |
| Vector database | [Qdrant](https://qdrant.tech/) |
| Embeddings | [fastembed](https://github.com/qdrant/fastembed) with `BAAI/bge-small-en-v1.5`, as its own service |
| Chatbot | Python + FastAPI |
| LLM | Z.AI GLM (Flash tier) via its OpenAI-compatible API |

## Roadmap

Work is tracked in [GitHub Issues](https://github.com/terenceponce/glowing-journey/issues):

1. Local Kubernetes cluster (#1)
2. Deploy Langfuse (#2)
3. Qdrant, embedding service and document ingestion (#3)
4. RAG chatbot (#4)
5. Instrument the chatbot with Langfuse (#5)
6. Evals and failure drills (#6)

## Getting started

Prerequisites: Docker Desktop (10 GB+ memory), `kubectl`, and `brew install kind helm`.

```sh
make cluster-up      # create the kind cluster and namespaces
make cluster-status  # nodes, namespaces, pods
make cluster-down    # delete the cluster
```

Then Langfuse:

```sh
make langfuse-up     # cert-manager, ClickHouse operator, secrets, Langfuse
make langfuse-ui     # http://localhost:3000 (login in secrets/langfuse.env)
make langfuse-test   # send a test trace
make langfuse-down   # uninstall (keeps data)
```

Then the vector store:

```sh
make qdrant-up       # Qdrant (persistent) in the qdrant namespace
make embedder-up     # embedding service in the app namespace
make ingest          # chunk + embed the Kubernetes concepts docs into the `docs` collection
make query Q="How do containers in the same Pod share storage?"
make qdrant-ui       # http://localhost:6333/dashboard
```

Then the chatbot (needs a Z.AI key in `secrets/zai.env` as `ZAI_API_KEY=...`):

```sh
make chatbot-up      # Secret from secrets/zai.env, build, deploy to the app namespace
make chatbot-test    # one question through POST /chat
make chatbot-ui      # chat page at http://localhost:8080
make chatbot-trace TRACE=<trace_id>  # the turn's Langfuse trace (also in the Langfuse UI)
```

See [docs/cluster.md](docs/cluster.md), [docs/langfuse.md](docs/langfuse.md), [docs/vector-store.md](docs/vector-store.md), [docs/chatbot.md](docs/chatbot.md) and [docs/observability.md](docs/observability.md) for details.
