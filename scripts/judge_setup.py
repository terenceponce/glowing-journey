"""Set up Langfuse's own LLM-as-judge for the chatbot. Idempotent; run it with `make judge-setup`.

  1. Upserts the LLM connection JUDGE_PROVIDER: OpenAI adapter, base URL JUDGE_BASE_URL, custom model
     JUDGE_MODEL only (no OpenAI default models). The key is ZAI_API_KEY from the app/zai-api Secret, which
     `make chatbot-secret` creates from secrets/zai.env. It's never printed.
  2. Creates or updates the evaluators in judge_prompts.py (names = score names). Langfuse keeps a version
     per update, so an evaluator is only updated when its definition changed.
  3. Creates or updates the evaluation rule JUDGE_RULE_NAME. It runs both evaluators on successful
     `chat-completion` generations of `chat` traces in the chatbot's environment. JUDGE_RULE_ENABLED=false
     (the default) keeps it switched off; see docs/evals.md for why.

Env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL, LANGFUSE_TRACING_ENVIRONMENT (the
chatbot's, from chatbot-config), ZAI_API_KEY, JUDGE_PROVIDER, JUDGE_BASE_URL, JUDGE_MODEL,
JUDGE_RULE_NAME, JUDGE_RULE_ENABLED, JUDGE_SAMPLING.
"""

import json
import os
import random
import sys
import time

import httpx

import judge_prompts

PROVIDER = os.environ.get("JUDGE_PROVIDER", "zai")
BASE_URL = os.environ["JUDGE_BASE_URL"]
MODEL = os.environ["JUDGE_MODEL"]
RULE_NAME = os.environ.get("JUDGE_RULE_NAME", "chat-judge")
RULE_ENABLED = os.environ.get("JUDGE_RULE_ENABLED", "false").lower() == "true"
SAMPLING = float(os.environ.get("JUDGE_SAMPLING", "1"))
ENVIRONMENT = os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "lab")


def check(resp: httpx.Response) -> dict:
    if resp.is_error:
        sys.exit(f"{resp.request.method} {resp.request.url.path}: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def write(api: httpx.Client, method: str, path: str, body: dict, attempts: int = 5) -> dict:
    """POST/PATCH an evaluator or rule. Langfuse test-calls the judge model when an evaluator is saved; when the
    free model answers 429 there, Langfuse returns 500, so 5xx responses are retried with backoff."""
    for attempt in range(1, attempts + 1):
        resp = api.request(method, path, json=body)
        if resp.status_code < 500 or attempt == attempts:
            return check(resp)
        delay = min(5 * 2 ** (attempt - 1), 60) * random.uniform(0.75, 1.25)
        print(f"  {method} {path}: {resp.status_code}, retrying in {delay:.0f}s")
        time.sleep(delay)
    raise AssertionError("unreachable")


def list_all(api: httpx.Client, path: str) -> list[dict]:
    items, cursor = [], None
    while True:
        body = check(api.get(path, params={"limit": 100, **({"cursor": cursor} if cursor else {})}))
        items += body["data"]
        cursor = (body.get("meta") or {}).get("cursor") or (body.get("meta") or {}).get("nextCursor")
        if not cursor or not body["data"]:
            return items


def setup_connection(api: httpx.Client) -> None:
    body = {
        "provider": PROVIDER,
        "adapter": "openai",
        "secretKey": os.environ["ZAI_API_KEY"],
        "baseURL": BASE_URL,
        "customModels": [MODEL],
        "withDefaultModels": False,
    }
    conn = check(api.put("/api/public/llm-connections", json=body))
    print(f"llm connection {conn['provider']}: adapter={conn['adapter']} baseURL={conn['baseURL']} models={conn['customModels']}")


def evaluator_body(e: dict) -> dict:
    return {
        "type": "llm_as_judge",
        "name": e["name"],
        "description": e["description"],
        "prompt": e["prompt"],
        "modelConfig": {"provider": PROVIDER, "model": MODEL},
        "variableMapping": judge_prompts.VARIABLE_MAPPING,
        "outputDefinition": {
            "dataType": "NUMERIC",
            "minValue": 0,
            "maxValue": 1,
            "scoreValueInstructions": e["score"],
            "scoreReasoningInstructions": e["reasoning"],
        },
    }


def same_definition(current: dict, wanted: dict) -> bool:
    prompt = [{"role": m["role"], "content": m["content"]} for m in current.get("prompt") or []]
    mapping = [
        {"variable": m["variable"], "source": m["source"], **({"jsonPath": m["jsonPath"]} if m.get("jsonPath") else {})}
        for m in current.get("variableMapping") or []
    ]
    out = current.get("outputDefinition") or {}
    return (
        current.get("description") == wanted["description"]
        and prompt == wanted["prompt"]
        and mapping == wanted["variableMapping"]
        and {k: (current.get("modelConfig") or {}).get(k) for k in ("provider", "model")} == wanted["modelConfig"]
        and all(out.get(k) == v for k, v in wanted["outputDefinition"].items())
    )


def setup_evaluators(api: httpx.Client) -> list[str]:
    existing = list_all(api, "/api/public/v2/evaluators")
    ids = []
    for e in judge_prompts.EVALUATORS:
        wanted = evaluator_body(e)
        matches = [x for x in existing if x["name"] == e["name"] and x["type"] == "llm_as_judge"]
        if len(matches) > 1:
            sys.exit(f"evaluator {e['name']}: {len(matches)} evaluators with this name; delete the extra ones in the UI")
        if not matches:
            created = write(api, "POST", "/api/public/v2/evaluators", wanted)
            print(f"evaluator {e['name']}: created {created['id']} v{created['version']} status={created['status']}")
            ids.append(created["id"])
            continue
        current = matches[0]
        if same_definition(current, wanted):
            print(f"evaluator {e['name']}: up to date ({current['id']} v{current['version']}) status={current['status']}")
        else:
            updated = write(api, "PATCH", f"/api/public/v2/evaluators/{current['id']}", wanted)
            print(f"evaluator {e['name']}: updated {updated['id']} to v{updated['version']} status={updated['status']}")
        if current.get("status") == "paused":
            print(f"  paused: {current.get('pausedReason')} {current.get('pausedMessage')}")
        ids.append(current["id"])
    return ids


def setup_rule(api: httpx.Client, evaluator_ids: list[str]) -> None:
    body = {
        "name": RULE_NAME,
        "enabled": RULE_ENABLED,
        "sampling": SAMPLING,
        "filter": [
            {"type": "stringOptions", "column": "environment", "operator": "any of", "value": [ENVIRONMENT]},
            {"type": "stringOptions", "column": "traceName", "operator": "any of", "value": ["chat"]},
            {"type": "stringOptions", "column": "type", "operator": "any of", "value": ["GENERATION"]},
            {"type": "stringOptions", "column": "name", "operator": "any of", "value": ["chat-completion"]},
            # Failed attempts (429s) are ERROR generations without an answer.
            {"type": "stringOptions", "column": "level", "operator": "none of", "value": ["ERROR"]},
        ],
        "evaluatorAssignments": [{"evaluatorId": i, "variableMapping": None} for i in evaluator_ids],
    }
    rules = [r for r in list_all(api, "/api/public/v2/evaluation-rules") if r["name"] == RULE_NAME]
    if len(rules) > 1:
        sys.exit(f"rule {RULE_NAME}: {len(rules)} rules with this name; delete the extra ones in the UI")
    if rules:
        rule = write(api, "PATCH", f"/api/public/v2/evaluation-rules/{rules[0]['id']}", body)
        action = "updated"
    else:
        rule = write(api, "POST", "/api/public/v2/evaluation-rules", body)
        action = "created"
    print(f"rule {RULE_NAME}: {action} {rule['id']} enabled={rule['enabled']} sampling={rule['sampling']}")
    print("  filter: " + json.dumps(rule["filter"], separators=(",", ":")))


def main() -> None:
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    with httpx.Client(base_url=os.environ["LANGFUSE_BASE_URL"], auth=auth, timeout=60) as api:
        setup_connection(api)
        setup_rule(api, setup_evaluators(api))


if __name__ == "__main__":
    main()
