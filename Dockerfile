# Korpha container image.
#
# Two-stage build: builder pulls deps via uv (Astral's fast Python toolchain),
# runtime stage ships a minimal Debian-slim with the venv copied in.
#
# Build:
#   docker build -t korpha:latest .
#
# Run (mounts ~/.korpha as a volume so config + DB persist across restarts):
#   docker run --rm -p 8765:8765 \
#     -v $HOME/.korpha:/home/korpha/.korpha \
#     korpha:latest
#
# Or use docker-compose: `docker compose up`.

# ---------- Build stage ----------
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv (the package manager). Pinned for reproducibility.
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install deps first for layer caching: copy lock files only, then sync.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy the package source + install
COPY korpha/ ./korpha/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY install.sh ./install.sh
RUN uv sync --frozen --no-dev


# ---------- Runtime stage ----------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root user — running as root is bad practice for a long-lived service
RUN groupadd --system --gid 1000 korpha && \
    useradd --system --uid 1000 --gid korpha --create-home --shell /bin/bash korpha

# Bare-minimum runtime libs (sqlite3 for FTS5, ca-certs for HTTPS, curl for healthcheck)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      sqlite3 \
      curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app /app

# Make the binary easily reachable
RUN ln -s /app/.venv/bin/korpha /usr/local/bin/korpha

USER korpha
WORKDIR /home/korpha

# Volume for persistent state — DB, config, themes, etc.
VOLUME ["/home/korpha/.korpha"]

# Web dashboard port
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8765/healthz || exit 1

CMD ["korpha", "server", "--host", "0.0.0.0"]
