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

Not runnable yet. Setup instructions will land here as `make` targets once the cluster task (#1) is done.
