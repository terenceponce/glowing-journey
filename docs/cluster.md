# Local Kubernetes cluster

A single-node [kind](https://kind.sigs.k8s.io/) cluster called `obs-lab`, running on Docker Desktop. It hosts Langfuse, Qdrant, the embedding service and the chatbot.

## Prerequisites

- Docker Desktop with at least 10 GB of memory (check with `docker info --format "{{.MemTotal}}"`)
- `kubectl` (ships with Docker Desktop)
- `kind` and `helm`: `brew install kind helm`

Tested with kind v0.33.0, Helm v4.3.0 and kubectl v1.36.1 on Apple Silicon.

## Usage

| Command | What it does |
|---|---|
| `make cluster-up` | Creates the cluster from `cluster/kind-config.yaml` (skipped if it already exists), applies the namespaces and waits for the node to be Ready. Safe to run again. |
| `make cluster-status` | Lists kind clusters, nodes, namespaces and all pods. |
| `make cluster-down` | Deletes the cluster and its Docker container. |

`make cluster-up` switches your current kubectl context to `kind-obs-lab`. The Makefile always passes `--context kind-obs-lab` explicitly, so it won't touch another cluster by accident.

## Layout

- `cluster/kind-config.yaml`: kind config, one control-plane node.
- `deploy/namespaces/namespaces.yaml`: the `langfuse`, `qdrant` and `app` namespaces.

## Notes

- **Node image is pinned** by tag and digest (`kindest/node:v1.36.4@sha256:...`) so the cluster is reproducible. kind v0.33.0 defaults to Kubernetes v1.37.0, but v1.36 matches the local kubectl client's minor version and is a little more conservative for Helm charts that haven't caught up yet. When upgrading kind, take the new digest from the [kind release notes](https://github.com/kubernetes-sigs/kind/releases); kind only supports the node images listed for that release.
- **No port mappings.** Services will be reached with `kubectl port-forward` for now. If an ingress is needed later, add `extraPortMappings` to the kind config; that requires recreating the cluster.
- **Storage:** kind ships the `local-path-provisioner` as the default StorageClass, so PVCs (e.g. for Langfuse's Postgres/ClickHouse and Qdrant) work out of the box. Data lives inside the node container and is lost on `make cluster-down`.
- Creating the cluster took about 20 seconds after the node image was cached.
