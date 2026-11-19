# Stage 1: Build strict TypeScript + Vite frontend bundle
FROM node:22-alpine AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts && npm rebuild esbuild
COPY frontend/ ./
RUN npm run build

# Stage 2: Build Python wheels without shipping gcc in runtime image
FROM python:3.12-slim AS backend-builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# Stage 3: Minimal non-root runtime image
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system quantico \
    && useradd --system --gid quantico --home /app quantico

WORKDIR /app
COPY --from=backend-builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

COPY backend/ ./backend/
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist

RUN mkdir -p /app/media_storage && chown -R quantico:quantico /app

USER quantico
WORKDIR /app/backend
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"]
