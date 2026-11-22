FROM ghcr.io/astral-sh/uv:0.13.9-python3.15-trixie-slim AS build
ENV UV_NO_DEV=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked

FROM python:3.15-slim-trixie
# the official MCP Registry checks this label to confirm the image belongs to server.json's name
LABEL io.modelcontextprotocol.server.name="io.github.BukeLy/peerbox"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY server.py instance.py accounts.py mailbox.py bucket.py panel.py icon.png schema.sql ./
COPY panel ./panel
ENV PATH="/app/.venv/bin:$PATH"
CMD exec granian --interface asgi --loop uvloop --http 1 --no-ws --host 0.0.0.0 --port "${PORT:-8000}" server:app
