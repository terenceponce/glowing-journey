#!/usr/bin/env bash
# Create (or update) the app/zai-api Secret from secrets/zai.env (git-ignored).
# The file holds one line: ZAI_API_KEY=...  The key is never printed.
set -euo pipefail

KUBECTL=${KUBECTL:-kubectl --context kind-obs-lab}
ENV_FILE=${ENV_FILE:-secrets/zai.env}

if ! grep -q '^ZAI_API_KEY=.' "$ENV_FILE" 2>/dev/null; then
  echo "$ENV_FILE is missing or has no ZAI_API_KEY=... line. Create it first (see docs/chatbot.md)." >&2
  exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
grep '^ZAI_API_KEY=' "$ENV_FILE" > "$tmp/zai.env"

$KUBECTL -n app create secret generic zai-api --from-env-file="$tmp/zai.env" --dry-run=client -o yaml | $KUBECTL apply -f -
