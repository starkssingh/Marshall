# syntax=docker/dockerfile:1
# Development / research image for xq (ARCH-008). Production images arrive with DEPLOY-001.
FROM python:3.12-slim

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
USER xq

CMD ["xq", "--help"]
