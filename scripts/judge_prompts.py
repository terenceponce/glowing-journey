"""The LLM-as-judge evaluators: their names (also the Langfuse score names), prompts and output instructions.

Shared by scripts/judge_setup.py (Langfuse evaluators) and scripts/judge.py (the script judge), so both
judges ask the model exactly the same thing.

Both judges read the chatbot's `chat-completion` generation (see docs/observability.md):
  {{request}}  its input: the messages sent to the chat model. The last user message holds the numbered
               context passages and, after "Question:", the user's question.
  {{answer}}   its output: the assistant message with the chatbot's answer.
"""

SYSTEM = (
    "You are a strict evaluator of a retrieval-augmented chatbot that answers questions about Kubernetes. "
    "You are given the chatbot's request (its instructions, any earlier turns, then a final user message with "
    "numbered context passages and the question) and the chatbot's answer. Judge only what you are asked to judge."
)

GROUNDEDNESS = {
    "name": "groundedness",
    "description": "Is the answer supported by the retrieved context passages? 1 = every claim is supported.",
    "prompt": [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": """Rate the GROUNDEDNESS of the answer: are its statements supported by the numbered context \
passages in the last user message of the request?

- 1.0: every factual statement is supported by the passages (wording may differ).
- 0.7: mostly supported; a minor detail goes beyond the passages.
- 0.4: a mix of supported and unsupported statements.
- 0.0: the main statements are not in the passages, or contradict them.
An answer that only says it doesn't know from the documentation makes no claims: rate it 1.0.
Check that citations like [2] point to a passage that says what is claimed. Don't judge whether the answer is
helpful or complete, only whether it is supported.

Request:
{{request}}

Answer:
{{answer}}""",
        },
    ],
    "score": "A number from 0 to 1. 1 means every statement in the answer is supported by the context passages.",
    "reasoning": "One or two sentences naming any unsupported statement, or saying that all statements are supported.",
}

ANSWER_RELEVANCE = {
    "name": "answer-relevance",
    "description": "Does the answer address the user's question? 1 = directly and fully answers it.",
    "prompt": [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": """Rate the RELEVANCE of the answer: does it address the user's question (the text after \
"Question:" in the last user message of the request, read together with any earlier turns)?

- 1.0: directly and fully answers the question.
- 0.7: answers it, but misses part of it or adds a lot of unrelated material.
- 0.4: only partly related to the question.
- 0.0: doesn't answer the question: an answer about something else, or a refusal such as "I don't know from \
the documentation".
A refusal scores 0.0 even when refusing was the right thing to do (for example an off-topic question): this score \
measures whether the user got an answer, not whether the chatbot behaved well. Don't judge whether the answer is \
correct or supported by the passages, only whether it answers the question.

Request:
{{request}}

Answer:
{{answer}}""",
        },
    ],
    "score": "A number from 0 to 1. 1 means the answer directly and fully answers the question.",
    "reasoning": "One or two sentences on what the answer covers or misses of the question.",
}

EVALUATORS = [GROUNDEDNESS, ANSWER_RELEVANCE]

# Where the prompt variables come from on the chat-completion generation.
VARIABLE_MAPPING = [
    {"variable": "request", "source": "input"},
    {"variable": "answer", "source": "output"},
]
