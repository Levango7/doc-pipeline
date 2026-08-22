# =============================================================================
# Doc-Pipeline — Production Dockerfile
# =============================================================================
# Two-stage build:
#   Stage 1 (builder): pip-install dependencies into venv
#   Stage 2 (runtime): minimal image with project code + deps
# =============================================================================

# -- Stage 1: Builder --------------------------------------------------------
FROM python:3.12-slim AS builder

# Prevent Python from writing .pyc / buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install build tools (none needed — pure Python deps)
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# -- Stage 2: Runtime --------------------------------------------------------
FROM python:3.12-slim AS runtime

# System setup: non-root user + runtime dirs
RUN addgroup --system --gid 1001 app && \
    adduser --system --uid 1001 --gid 1001 --disabled-password --gecos "" app && \
    mkdir -p /app/checkpoints /app/logs /app/versions /app/backups /data && \
    chown -R app:app /app /data

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
VOLUME ["/app/checkpoints", "/app/logs", "/app/versions", "/app/backups"]

# Admin API
EXPOSE 8910

# Health check: 先确认 admin API 进程存活再探 /health
# （容器刚启动时 CMD 还是 --help，8910 未监听，直接 curl 会误报 unhealthy）
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD pgrep -f "python run.py" > /dev/null 2>&1 && \
        python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8910/health', timeout=3)" || exit 1

# Drop privileges
USER app:app

# Default: run pipeline with production config
ENTRYPOINT ["python", "run.py"]
CMD ["--help"]
