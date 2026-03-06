FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl: needed for Coolify proxy health checks (avoids "no available server" when check uses curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: run Web UI (browser access on PORT/8765). For watcher-only, override with: python main.py
ENV PORT=8765
EXPOSE 8765

# Explicit healthcheck: Coolify/Traefik use this (or run curl inside container). Requires curl above.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=2 \
    CMD curl -f http://127.0.0.1:8765/health || exit 1

CMD ["python", "-m", "app.web"]
