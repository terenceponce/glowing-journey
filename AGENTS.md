# AGENTS.md

Guidance for AI coding agents working in this repository. See `README.md` for what the project is.

## Workflow

- Every piece of work maps to a GitHub issue. Read the issue (`gh issue view <N>`) before starting, and stay within its scope. If something outside the issue needs doing, note it rather than doing it.
- Reference the issue in every commit message: `Refs #N`. The commit that completes the issue uses `Closes #N`.
- Make small, focused commits on `main` and push them.
- When a step needs a human (changing Docker Desktop settings, creating accounts or API keys in a UI, confirming a choice), stop and ask instead of guessing.

## This repo is public

- Never commit secrets: API keys, passwords, Langfuse salts or encryption keys, kubeconfigs. Keep them in git-ignored files (e.g. `.env`, `secrets/`) and load them into Kubernetes Secrets from there.
- Don't put personal or company-confidential information in code, commits, issues or docs.
- Before committing, check `git diff --staged` for anything that looks like a credential.

## Environment

- macOS on Apple Silicon (arm64). Build every container image for `linux/arm64`.
- Local cluster: kind, cluster name `obs-lab`. Load images with `kind load docker-image` instead of pushing to a registry.
- Docker Desktop with 10–12 GB of memory allocated. Keep resource requests laptop-sized.
- The host's Python is old. Run services in containers based on Python 3.12+; don't depend on the host interpreter.

## Conventions

- **Reproducible:** everything runs through `Makefile` targets. No undocumented one-off commands.
- **Kubernetes:** Helm values live in `deploy/<component>/values.yaml`. Plain manifests live in `deploy/<component>/`. Namespaces are `langfuse`, `qdrant` and `app`.
- **Layout:**
  - `cluster/`: kind configuration
  - `deploy/`: Helm values and manifests
  - `services/<name>/`: one folder per service, each with its own `pyproject.toml` and `Dockerfile`
  - `scripts/`: one-off tooling such as experiments
  - `docs/`: write-ups
- **Config:** read config from environment variables and keep secrets in Kubernetes Secrets. Don't hardcode URLs, model IDs or keys.
- **Docs:** each task records what was done, how to run it, and anything surprising in `docs/`.
- **Check current docs:** use the official documentation for Langfuse, Qdrant, kind, Helm and Z.AI rather than memory. These tools change quickly.
