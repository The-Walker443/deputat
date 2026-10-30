# syntax=docker/dockerfile:1
# Deputat – single container: FastAPI + SQLite in /data (Konzept §14.1, DEPLOY.md)
# Runs with a read-only root filesystem: everything written at runtime goes to /data or /tmp.

# ---------------------------------------------------------------- build stage: resolve deps with uv
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------- runtime stage
FROM python:3.12-slim

ARG APP_UID=10001
ARG APP_GID=10001
ARG GIT_SHA=unknown

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Europe/Berlin \
    GIT_SHA=${GIT_SHA}

LABEL org.opencontainers.image.title="deputat" \
      org.opencontainers.image.description="Arbeitszeitkonto für Lehrkräfte (self-hosted)" \
      org.opencontainers.image.source="https://github.com/The-Walker443/deputat" \
      org.opencontainers.image.revision="${GIT_SHA}"

# The named volume inherits ownership from the image, so /data and its subfolders are created here.
RUN groupadd --system --gid ${APP_GID} app \
    && useradd --system --uid ${APP_UID} --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data/backups /data/reports /data/exports /data/imports \
    && chown -R app:app /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic
COPY config ./config
COPY app ./app
# Bytecode is compiled at build time because the root filesystem is read-only at runtime.
RUN python -m compileall -q /app/app /app/alembic

USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# Migrations, seed and the backup scheduler run in the app lifespan on startup.
# No --proxy-headers: NPM forwards scheme=http for tunnel traffic; the app builds absolute URLs from BASE_URL
# and resolves the client IP for logging itself (DEPLOY.md).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
