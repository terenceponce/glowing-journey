# Ingestion job. Build for linux/arm64 (see Makefile: make ingest-image).
# The corpus is fetched at build time and pinned to a git ref, so the Job runs offline and reproducibly.

FROM alpine/git:2.49.1 AS corpus
ARG CORPUS_REPO=https://github.com/kubernetes/website.git
ARG CORPUS_REF
ARG CORPUS_PATH=content/en/docs/concepts
RUN test -n "$CORPUS_REF" || (echo "CORPUS_REF build arg is required" && exit 1)
WORKDIR /src
RUN git init -q . \
 && git remote add origin "$CORPUS_REPO" \
 && git sparse-checkout set --no-cone "/$CORPUS_PATH/" \
 && git fetch -q --depth 1 --filter=blob:none origin "$CORPUS_REF" \
 && git checkout -q FETCH_HEAD \
 && mkdir -p /corpus && cp -r "$CORPUS_PATH"/. /corpus/

FROM python:3.12-slim
ARG CORPUS_REF
ARG CORPUS_PATH=content/en/docs/concepts
ENV CORPUS_DIR=/corpus \
    CORPUS_PREFIX=${CORPUS_PATH} \
    CORPUS_REF=${CORPUS_REF} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /srv
COPY pyproject.toml chunking.py ingest.py query.py ./
RUN pip install --quiet .
COPY --from=corpus /corpus /corpus

RUN useradd --uid 10001 --no-create-home app
USER 10001

CMD ["python", "ingest.py"]
