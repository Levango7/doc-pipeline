# =============================================================================
# Doc-Pipeline — Production Dockerfile
# =============================================================================
# Two-stage build:
#   Stage 1 (builder): pip-install dependencies into venv
#   Stage 2 (runtime): minimal image with project code + deps
# =============================================================================

# -- Stage 1: Builder --------------------------------------------------------
FROM python:3.11-slim AS builder

# Prevent Python from writing .pyc / buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install build tools (none needed — pure Python deps)
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# -- Stage 2: Runtime --------------------------------------------------------
FROM python:3.11-slim AS runtime

# System setup: non-root user + runtime dirs
RUN addgroup --system --gid 1001 app && \
    adduser --system --uid 1001 --gid 1001 --disabled-password --gecos "" app && \
    mkdir -p /app /data && \
    chown app:app /app /data

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Make venv binaries available in PATH
ENV PATH=/opt/venv/bin:$PATH

# Copy project code
COPY --chown=app:app . .

# Runtime data directories (persistent volume mounts)
VOLUME ["/app/checkpoints", "/app/logs"]

# Admin API
EXPOSE 8910

# Drop privileges
USER app:app

# Default: show help
CMD ["python", "run.py", "--help"]
