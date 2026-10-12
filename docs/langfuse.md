# Langfuse

Self-hosted [Langfuse](https://langfuse.com) v4 in the `langfuse` namespace, installed with the official Helm chart ([langfuse-k8s](https://github.com/langfuse/langfuse-k8s)). The chart bundles all the datastores:

| Component | What runs | Notes |
|---|---|---|
| `langfuse-web` | UI + public API (port 3000) | Runs headless init on startup |
| `langfuse-worker` | Async ingestion worker | |
| Postgres | `groundhog2k/postgres` sub-chart | Transactional data |
| ClickHouse + Keeper | `ClickHouseCluster` / `KeeperCluster` CRs | Traces and observations; managed by the ClickHouse operator |
| Valkey | `valkey-io/valkey` sub-chart | Queue / cache (Redis-compatible) |
| SeaweedFS | `seaweedfs` sub-chart (all-in-one) | S3-compatible blob storage (replaces MinIO in chart v2) |

Versions: chart `2.1.4` (Langfuse `4.50.0`), cert-manager `v1.20.2`, clickhouse-operator `0.0.8`. They're pinned at the top of the `Makefile`.

## Install

Prerequisite: the cluster from [cluster.md](cluster.md) (`make cluster-up`).

```sh
make langfuse-up      # prereqs + secrets + helm upgrade --install, waits until Ready
make langfuse-status  # pods, services, PVCs
```

`make langfuse-up` is idempotent and runs, in order:

1. `langfuse-prereqs`: installs **cert-manager** and the **ClickHouse operator**, once per cluster. Chart v2 requires them because ClickHouse is deployed through operator CRs. The chart aborts if the CRDs are missing.
2. `langfuse-secrets`: generates credentials and creates the Kubernetes Secrets (see below).
3. `helm upgrade --install langfuse langfuse/langfuse -f deploy/langfuse/values.yaml`.

A cold install (all images pulled) takes several minutes. `langfuse-web` and `langfuse-worker` restart a few times while the databases come up; that's expected.

## Access

```sh
make langfuse-ui   # port-forward svc/langfuse-web → http://localhost:3000 (Ctrl-C to stop)
```

Log in with `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD` from `secrets/langfuse.env`. The org `obs-lab` and project `rag-chatbot` are already there.

In-cluster base URL for apps: `http://langfuse-web.langfuse.svc.cluster.local:3000`.

## Secrets

Nothing secret is committed. `scripts/langfuse-secrets.sh` (`make langfuse-secrets`):

1. On first run, generates `secrets/langfuse.env` (git-ignored, mode 600): salt, encryption key, NextAuth secret, Postgres/ClickHouse/Valkey/S3 passwords, the project API key pair and the first user's password.
2. Creates or updates these Secrets from it:

| Secret | Namespace | Contents | Used by |
|---|---|---|---|
| `langfuse` | `langfuse` | App secrets + datastore credentials, in the key layout of the chart's `examples/minimal-installation/secret.yaml` | The chart (`secretKeyRef` / `existingSecret` in `values.yaml`) |
| `langfuse-init` | `langfuse` | `LANGFUSE_INIT_*` vars | `langfuse-web` via `envFrom` |
| `langfuse-api` | `app` | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | The chatbot (and the smoke test) |

Re-running keeps the existing file, so the keys stay stable. **Don't regenerate the salt or encryption key on a live install:** a new salt invalidates every API key, and a new encryption key makes encrypted data unreadable. To rotate everything, run `make langfuse-purge`, delete `secrets/langfuse.env`, then run `make langfuse-up`.

### Headless initialization (no UI clicks needed)

Langfuse supports [headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization). When the `LANGFUSE_INIT_*` env vars are set, `langfuse-web` creates the org, project, API keys and first user on startup. That means nobody has to create keys in the UI, and the keys are known before Langfuse starts. Public sign-up is disabled (`signUpDisabled: true`) because the user already exists.

## Smoke test

```sh
make langfuse-test
```

This runs `scripts/langfuse_smoke_test.py` as a Job in the `app` namespace (`python:3.12-slim`, `langfuse==4.17.0` installed at start). It reads the `langfuse-api` Secret and the in-cluster URL, the same way the chatbot will. It calls `auth_check()`, sends a trace with one span and a nested generation, then prints the trace ID and a UI link. Open the link with `make langfuse-ui` running.

To check from the command line instead (with the port-forward running):

```sh
source secrets/langfuse.env
curl -s -u "$LANGFUSE_INIT_PROJECT_PUBLIC_KEY:$LANGFUSE_INIT_PROJECT_SECRET_KEY" \
  "http://localhost:3000/api/public/v2/observations?traceId=<trace-id>"
```

## Resource trimming

Chart defaults are sized for production. For the laptop:

| Component | Chart default | Here |
|---|---|---|
| ClickHouse | req 1 CPU / 2Gi, limit 4Gi, 100Gi PVC | req 250m / 1Gi, limit 2Gi, 10Gi PVC, logger `information` (CRD default is `trace`) |
| Keeper | **3 replicas**, req 250m / 256Mi, limit 1Gi, 20Gi PVC each | **1 replica**, req 50m / 128Mi, limit 512Mi, 1Gi PVC, logger `information` |
| SeaweedFS | req 500m / 1Gi, limit 2 CPU / 2Gi, 50Gi PVC | req 100m / 256Mi, limit 1 CPU / 512Mi, 10Gi PVC |
| Postgres | none, 20Gi PVC | req 100m / 256Mi, limit 512Mi, 5Gi PVC |
| Valkey | none, 8Gi PVC | req 50m / 64Mi, limit 256Mi, 1Gi PVC |
| langfuse-web | none | req 250m / 768Mi, limit 1.5Gi |
| langfuse-worker | none | req 250m / 512Mi, limit 1Gi |

Memory requests add up to about 3 GiB, plus cert-manager and the operator. kind's `local-path` provisioner doesn't enforce PVC sizes, so the smaller sizes are informational. Telemetry is off.

## Teardown

```sh
make langfuse-down    # helm uninstall; PVCs and Secrets kept, so `make langfuse-up` brings the data back
make langfuse-purge   # also deletes the PVCs and the three Secrets (secrets/langfuse.env is kept)
make cluster-down     # deletes everything, including cert-manager and the operator
```

cert-manager and the ClickHouse operator aren't removed by `langfuse-down` / `langfuse-purge`. They're cluster-wide and cheap.

## Surprises

- **Chart v2 needs cluster-wide operators.** It dropped the Bitnami sub-charts. ClickHouse now runs through the upstream ClickHouse operator, which needs cert-manager. Neither is installed by the Langfuse chart.
- **MinIO is gone.** S3 storage is SeaweedFS in chart v2, not MinIO as older docs say.
- **Keeper defaults to 3 replicas** even with a single ClickHouse replica, and it can't be disabled while the chart deploys ClickHouse.
- **The chart can auto-generate every credential**, persisted via Helm `lookup`. We pin them explicitly anyway so the API keys are known up front for headless init and survive a purge-and-reinstall.
- **Langfuse v4 is "events_only".** The v1 read endpoints such as `GET /api/public/traces` return *"This endpoint is not available on deployments running in Langfuse v4 events_only mode"*. Read data back through `GET /api/public/v2/observations?traceId=<id>` instead. Ingestion through the SDK works as usual.
- **Startup restarts are normal.** On a fresh install `langfuse-web` restarted 4 times and ClickHouse 3 times (ClickHouse comes up before Keeper and fails with a `Coordination::Exception`). The worker logs Prisma errors such as `table public.batch_actions does not exist` until web has run the migrations. Everything settled in about 2 minutes once images were cached.
- **The Python SDK uses `LANGFUSE_BASE_URL`** in the current docs (older examples use `LANGFUSE_HOST`).
