# Stage 1: frontend build
FROM node:22-alpine AS frontend
WORKDIR /web
COPY application/web/package.json application/web/package-lock.json ./
RUN npm ci
COPY application/web/ .
RUN npm run build

# Stage 1b: playground(/playground) frontend build
FROM node:22-alpine AS playground
WORKDIR /pg
COPY playground/package.json playground/package-lock.json ./
RUN npm ci
COPY playground/ .
RUN npm run build

# Stage 2: Python runtime
FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY application/ /app/application/
COPY --from=frontend /web/dist /app/application/web/dist
COPY --from=playground /pg/dist /app/application/playground_web/dist
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/application/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8501
HEALTHCHECK CMD curl --fail http://localhost:8501/api/health
CMD ["uvicorn", "application.server:app", "--host", "0.0.0.0", "--port", "8501", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips=*"]
