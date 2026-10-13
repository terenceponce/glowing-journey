"""Set up Langfuse for the chatbot: custom model prices for the GLM models, and the RAG prompt.

Idempotent; run it with `make chatbot-langfuse-setup` (a Job using the chatbot image). It:
  1. Creates or replaces the custom model definitions in MODELS, so Langfuse can compute cost.
     Langfuse turns OpenAI's prompt_tokens_details.cached_tokens into the usage type `input_cached_tokens`
     (and subtracts it from `input`), so the cached-input price uses that key.
  2. Creates a new version of the prompt from prompts.py, labelled `production`, if it differs from the
     current production version.

Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL from the environment.
"""

import os
import sys

import httpx
from langfuse import get_client

import prompts

PER_MILLION = 1e-6

# Z.AI prices, USD per token: https://docs.z.ai/guides/overview/pricing
MODELS = [
    {
        "modelName": "glm-4.7-flash",
        "matchPattern": r"(?i)^(glm-4\.7-flash)$",
        "unit": "TOKENS",
        "prices": {"input": 0.0, "input_cached_tokens": 0.0, "output": 0.0},  # free
    },
    {
        "modelName": "glm-5.3-flash",
        "matchPattern": r"(?i)^(glm-5\.3-flash)$",
        "unit": "TOKENS",
        "prices": {
            "input": 0.15 * PER_MILLION,
            "input_cached_tokens": 0.03 * PER_MILLION,
            "output": 0.50 * PER_MILLION,
        },
    },
]


def model_body(model: dict) -> dict:
    return {
        "modelName": model["modelName"],
        "matchPattern": model["matchPattern"],
        "unit": model["unit"],
        "pricingTiers": [
            {"name": "Standard", "isDefault": True, "priority": 0, "conditions": [], "prices": model["prices"]}
        ],
    }


def current_prices(model: dict) -> dict:
    tiers = model.get("pricingTiers") or []
    default = next((t for t in tiers if t.get("isDefault")), None)
    return {k: float(v) for k, v in (default or {}).get("prices", {}).items()}


def same_prices(a: dict, b: dict) -> bool:
    return a.keys() == b.keys() and all(abs(a[k] - b[k]) < 1e-15 for k in a)


def setup_models(api: httpx.Client) -> None:
    existing, page = [], 1
    while True:
        resp = api.get("/api/public/models", params={"page": page, "limit": 100})
        resp.raise_for_status()
        body = resp.json()
        existing += body["data"]
        if page >= body["meta"]["totalPages"]:
            break
        page += 1

    for model in MODELS:
        custom = [m for m in existing if m["modelName"] == model["modelName"] and not m.get("isLangfuseManaged")]
        if (
            len(custom) == 1
            and custom[0]["matchPattern"] == model["matchPattern"]
            and custom[0]["unit"] == model["unit"]
            and same_prices(current_prices(custom[0]), model["prices"])
        ):
            print(f"model {model['modelName']}: up to date ({custom[0]['id']})")
            continue
        for old in custom:
            api.delete(f"/api/public/models/{old['id']}").raise_for_status()
            print(f"model {model['modelName']}: deleted outdated definition {old['id']}")
        resp = api.post("/api/public/models", json=model_body(model))
        resp.raise_for_status()
        print(f"model {model['modelName']}: created {resp.json()['id']} prices/token {model['prices']}")


def setup_prompt() -> None:
    langfuse = get_client()
    try:
        current = langfuse.get_prompt(prompts.PROMPT_NAME, label="production", type="chat", cache_ttl_seconds=0)
    except Exception:  # not created yet (404)
        current = None
    # Langfuse returns the messages with an extra "type": "message"; compare role and content only.
    stored = [{"role": m["role"], "content": m["content"]} for m in current.prompt] if current else None
    if stored == prompts.RAG_CHAT_PROMPT:
        print(f"prompt {prompts.PROMPT_NAME}: production is version {current.version}, up to date")
        return
    created = langfuse.create_prompt(
        name=prompts.PROMPT_NAME,
        type="chat",
        prompt=prompts.RAG_CHAT_PROMPT,
        labels=["production"],
        commit_message="Synced from services/chatbot/prompts.py",
    )
    print(f"prompt {prompts.PROMPT_NAME}: created version {created.version}, labelled production")


def main() -> None:
    base_url = os.environ["LANGFUSE_BASE_URL"]
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    with httpx.Client(base_url=base_url, auth=auth, timeout=30) as api:
        try:
            setup_models(api)
        except httpx.HTTPStatusError as exc:
            sys.exit(f"Langfuse models API: {exc.response.status_code} {exc.response.text[:300]}")
    setup_prompt()


if __name__ == "__main__":
    main()
