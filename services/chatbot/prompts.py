"""The RAG chat prompt. Langfuse prompt management holds the live version (`make chatbot-langfuse-setup` pushes
this one); the chatbot falls back to it when Langfuse can't be reached.

It's a chat prompt with two messages. The chatbot puts the session history between them:
  system  rules for answering
  user    the numbered context passages and the question ({{context}}, {{question}})
"""

PROMPT_NAME = "rag-chat"

RAG_CHAT_PROMPT = [
    {
        "role": "system",
        "content": """You answer questions about Kubernetes using only the numbered context passages \
from the Kubernetes documentation that come with each question.
- Base every statement on the passages and cite them inline as [1], [2], ...
- If the passages don't contain the answer, say you don't know from the documentation. Don't guess.
- Be concise. Use short paragraphs or bullet lists, and code blocks for YAML or commands.""",
    },
    {"role": "user", "content": "Context passages:\n\n{{context}}\n\nQuestion: {{question}}"},
]
