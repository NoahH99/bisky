# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.12.7

# --from cannot interpolate variables, so the pinned uv image gets its own stage.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# Copy the pinned uv binary rather than building on uv's own image, so the
# Python base and the uv version are each pinned explicitly.
COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer stays cached until the lockfile changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --no-editable

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --no-editable copies the package into site-packages, so the runtime stage
# needs only the .venv and not ./src.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 1001 bisky \
    && useradd --system --uid 1001 --gid bisky --create-home bisky

WORKDIR /app

COPY --from=builder --chown=bisky:bisky /app/.venv /app/.venv
COPY --chown=bisky:bisky alembic.ini ./
COPY --chown=bisky:bisky migrations ./migrations

USER bisky

CMD ["bisky"]
