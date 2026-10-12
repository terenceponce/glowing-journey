# Embedding service. Build for linux/arm64 (see Makefile: make embedder-image).
FROM python:3.12-slim

ARG EMBED_MODEL=BAAI/bge-small-en-v1.5
ENV EMBED_MODEL=${EMBED_MODEL} \
    MODEL_CACHE_DIR=/models \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /srv
COPY pyproject.toml app.py ./
RUN pip install --quiet .

# Bake the model into the image so pods start without network access.
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['EMBED_MODEL'], cache_dir=os.environ['MODEL_CACHE_DIR'])"
ENV HF_HUB_OFFLINE=1

RUN useradd --uid 10001 --no-create-home app && chmod -R a+rX /models
USER 10001

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
