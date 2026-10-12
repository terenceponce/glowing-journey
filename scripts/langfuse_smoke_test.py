"""Send one trace to Langfuse with the Python SDK.

Reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL from the environment.
Run it with `make langfuse-test`, which runs it as a Job in the `app` namespace.
"""

import sys

from langfuse import get_client

langfuse = get_client()

if not langfuse.auth_check():
    sys.exit("Langfuse auth check failed: check the keys and LANGFUSE_BASE_URL")

with langfuse.start_as_current_observation(as_type="span", name="smoke-test") as span:
    span.update(input={"question": "Is Langfuse up?"})
    with langfuse.start_as_current_observation(
        as_type="generation", name="fake-llm-call", model="smoke-test-model"
    ) as generation:
        generation.update(output="Yes, Langfuse is up.")
    span.update(output={"answer": "Yes, Langfuse is up."})
    trace_id = langfuse.get_current_trace_id()

langfuse.flush()
print(f"Sent trace {trace_id}")
print(f"Open: http://localhost:3000/project/rag-chatbot/traces/{trace_id}")
