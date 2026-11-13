# Application container. MCP Toolbox runs as a separate sidecar container (see deploy/service.yaml).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY retail_agent ./retail_agent
COPY app ./app

ENV PATH="/app/.venv/bin:$PATH" PORT=8080
# Non-root user
RUN useradd --uid 10001 --no-create-home appuser
USER appuser

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
