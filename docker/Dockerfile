# syntax=docker/dockerfile:1
# Build context is the repository root: docker build -f docker/Dockerfile .

# ---- Stage 1: build the frontend ------------------------------------------
FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: install python deps + app package ---------------------------
FROM python:3.12-slim AS backend-build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY backend/ ./
RUN pip install --prefix=/install .

# ---- Stage 3: runtime ------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    STATIC_DIR=/app/static \
    DATABASE_URL=sqlite+aiosqlite:////data/gamestakes.db

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=backend-build /install /usr/local
COPY --from=frontend-build /build/dist /app/static
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config /data /logs

EXPOSE 8080
VOLUME ["/config", "/data", "/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; r=urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8080')}/api/health\", timeout=4); sys.exit(0 if r.status==200 else 1)"]

ENTRYPOINT ["/entrypoint.sh"]
