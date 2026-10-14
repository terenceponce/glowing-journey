"""Set up Langfuse for the experiments (docs/experiments.md): the dataset and the candidate prompt version.

Idempotent; run it with `make experiment-setup` (a Job using the chatbot image). It:
  1. Creates the dataset experiment_dataset.DATASET if it's missing, and creates or updates its items with their
     stable ids. Unchanged items are left alone, so a re-run creates no new dataset versions.
  2. Creates a version of the `rag-chat` prompt with the candidate system message below, labelled
     EXPERIMENT_PROMPT_LABEL ("concise"), unless the version with that label already has this content. It never
     touches the `production` label: the script checks that `production` points at the same version afterwards.

Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL and EXPERIMENT_PROMPT_LABEL. Needs prompts.py
from the chatbot image on PYTHONPATH (the production prompt the candidate is derived from).
"""

import os
import sys

from langfuse import get_client

import experiment_dataset
import prompts

PROMPT_LABEL = os.environ.get("EXPERIMENT_PROMPT_LABEL", "concise")

# The candidate: the production prompt with a stricter length rule. Hypothesis: fewer output tokens and lower
# latency, possibly at the cost of answer relevance on multi-part questions.
CONCISE_SYSTEM = """You answer questions about Kubernetes using only the numbered context passages \
from the Kubernetes documentation that come with each question.
- Answer in at most three sentences. Use a code block only when the question asks for a command or YAML.
- Base every statement on the passages and cite them inline as [1], [2], ...
- If the passages don't contain the answer, say you don't know from the documentation. Don't guess."""

CANDIDATE_PROMPT = [{"role": "system", "content": CONCISE_SYSTEM}, prompts.RAG_CHAT_PROMPT[1]]

langfuse = get_client()


def item_body(item: dict) -> dict:
    return {
        "input": {"question": item["question"], **({"history": item["history"]} if item.get("history") else {})},
        "expected_output": {"answer": item["answer"], "sources": item["sources"]},
        "metadata": {"kind": item["kind"]},
    }


def setup_dataset() -> None:
    name = experiment_dataset.DATASET
    try:
        langfuse.api.datasets.get(name)
        print(f"dataset {name}: exists")
    except Exception:  # 404
        langfuse.api.datasets.create(
            name=name,
            description="RAG chatbot eval set: Kubernetes concepts questions (direct, follow-up, off-topic) with "
            "expected answers and source pages. Source: scripts/experiment_dataset.py",
            metadata={"source": "scripts/experiment_dataset.py"},
        )
        print(f"dataset {name}: created")

    existing, page = {}, 1
    while True:
        resp = langfuse.api.dataset_items.list(dataset_name=name, page=page, limit=100)
        existing |= {i.id: i for i in resp.data}
        if page >= resp.meta.total_pages:
            break
        page += 1

    created = updated = unchanged = 0
    for item in experiment_dataset.ITEMS:
        body = item_body(item)
        old = existing.get(item["id"])
        if old and (old.input, old.expected_output, old.metadata) == (body["input"], body["expected_output"], body["metadata"]) \
                and str(old.status).endswith("ACTIVE"):
            unchanged += 1
            continue
        langfuse.api.dataset_items.create(dataset_name=name, id=item["id"], status="ACTIVE", **body)
        created += old is None
        updated += old is not None
    stale = sorted(set(existing) - {i["id"] for i in experiment_dataset.ITEMS})
    print(f"dataset {name}: {len(experiment_dataset.ITEMS)} items ({created} created, {updated} updated, {unchanged} unchanged)")
    if stale:
        print(f"dataset {name}: items not in experiment_dataset.py (left alone): {stale}")


def production_version() -> int:
    return langfuse.get_prompt(prompts.PROMPT_NAME, label="production", type="chat", cache_ttl_seconds=0).version


def setup_prompt() -> None:
    before = production_version()
    try:
        current = langfuse.get_prompt(prompts.PROMPT_NAME, label=PROMPT_LABEL, type="chat", cache_ttl_seconds=0)
    except Exception:  # no version has the label yet (404)
        current = None
    # Langfuse returns the messages with an extra "type": "message"; compare role and content only.
    stored = [{"role": m["role"], "content": m["content"]} for m in current.prompt] if current else None
    if stored == CANDIDATE_PROMPT:
        print(f"prompt {prompts.PROMPT_NAME}: label {PROMPT_LABEL} is version {current.version}, up to date")
    else:
        created = langfuse.create_prompt(
            name=prompts.PROMPT_NAME,
            type="chat",
            prompt=CANDIDATE_PROMPT,
            labels=[PROMPT_LABEL],
            commit_message="Experiment candidate from scripts/experiment_setup.py: at most three sentences",
        )
        print(f"prompt {prompts.PROMPT_NAME}: created version {created.version}, labelled {PROMPT_LABEL}")
    after = production_version()
    print(f"prompt {prompts.PROMPT_NAME}: production is version {after}")
    if after != before:
        sys.exit(f"production moved from version {before} to {after}; move it back in the Langfuse UI")


def main() -> None:
    setup_dataset()
    setup_prompt()
    langfuse.flush()


if __name__ == "__main__":
    main()
