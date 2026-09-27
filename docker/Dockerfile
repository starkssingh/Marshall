# syntax=docker/dockerfile:1
# Development / research image for xq (ARCH-008). Production images arrive with DEPLOY-001.
#
# Stages: `base` (locked runtime dependencies and the package), `test` (base plus the dev
# dependencies and the test suite; CI builds it and runs the suite inside it, ADR 0032) and
# `runtime` (the default, last stage: the image `docker compose` builds).
FROM python:3.12-slim AS base

# uv from PyPI (same version as the lockfile was produced with).
RUN pip install --no-cache-dir uv==0.8.17

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so source edits do not invalidate the dependency layer.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

RUN useradd --create-home --uid 1000 xq \
    && mkdir -p /app/data /app/logs /app/reports \
    && chown -R xq:xq /app/data /app/logs /app/reports


# The test suite inside the image: `docker build --target test -t xq:test -f docker/Dockerfile .`
# then `docker run --rm xq:test`. git is needed by the run-context tests and tzdata by the
# non-UTC local timezone the suite runs under.
FROM base AS test

RUN apt-get update \
    && apt-get install -y --no-install-recommends git tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

COPY tests ./tests
COPY experiments ./experiments
# The suite writes caches (hypothesis examples) next to the project.
RUN chown -R xq:xq /app
USER xq
ENV TZ=Asia/Tokyo
CMD ["pytest", "-m", "not slow and not research", "-p", "no:cacheprovider"]


FROM base AS runtime
USER xq

CMD ["xq", "--help"]
