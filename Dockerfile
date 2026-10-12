# One image for everything: the VPS deployment and the offline fallback on the laptop.
#   docker build -t robot-minimax .
#   docker run -p 8000:8000 -e ADMIN_TOKEN=... -e BRIDGE_TOKEN=... -e DEVICE_HASH_SECRET=... -v minimax-data:/data robot-minimax

# 1. Frontend (Vite + Svelte) -> static files
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY web/ ./
RUN npm run build

# 2. Python dependencies from the lockfile
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
WORKDIR /app
COPY server/pyproject.toml server/uv.lock ./
RUN uv export --frozen --no-dev --no-hashes -o requirements.txt \
 && uv pip install --system --no-cache -r requirements.txt

# 3. Runtime
FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/StanMar-bit/robot-minimax" \
      org.opencontainers.image.description="Classroom minimax demo with a NAO robot: phone voting, game tree board, admin panel" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/minimax.db \
    STATIC_DIR=/app/static
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin/uvicorn /usr/local/bin/uvicorn
WORKDIR /app
COPY server/app ./app
COPY --from=web /web/dist ./static
RUN useradd --system --uid 10001 minimax && mkdir /data && chown minimax /data
USER minimax
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status != 200)"
# One worker: all game state lives in memory. Proxy headers come from Traefik.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
