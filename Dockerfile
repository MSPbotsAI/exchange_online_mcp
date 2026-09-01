# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ARG VERSION="unknown"
ARG COMMIT_SHA="unknown"
ARG BUILD_DATE="unknown"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Dependencies first, so the layer is cached when only src changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# ── Production ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS production

# Re-declared: ARGs do not cross stage boundaries, and the LABELs below use them
ARG VERSION="unknown"
ARG COMMIT_SHA="unknown"
ARG BUILD_DATE="unknown"

RUN groupadd -g 1001 app && \
    useradd -u 1001 -g app -s /bin/sh -m app

# python:*-slim ships neither curl nor wget, and HEALTHCHECK needs one of them
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH"

ENV MCP_HTTP_PORT=8080
ENV MCP_HTTP_HOST=0.0.0.0
ENV EXO_BASE_URL=https://outlook.office365.com
ENV ENTRA_LOGIN_BASE_URL=https://login.microsoftonline.com

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["python", "-m", "exo_mcp"]

LABEL org.opencontainers.image.title="exchange-online-mcp"
LABEL org.opencontainers.image.description="Exchange Online MCP server"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.created="${BUILD_DATE}"
LABEL org.opencontainers.image.revision="${COMMIT_SHA}"
LABEL org.opencontainers.image.licenses="Apache-2.0"
