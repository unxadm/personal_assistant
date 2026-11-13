# syntax=docker/dockerfile:1

# --- web UI ---------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- app ------------------------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PA_SERVER__HOST=0.0.0.0 \
    PA_DATA_DIR=/data
WORKDIR /app

COPY pyproject.toml README.md ./
COPY assistant ./assistant
# Editable install: the app runs from /app, next to web/dist and plugins/.
RUN pip install -e . && rm -rf /root/.cache
COPY plugins ./plugins
COPY --from=web /web/dist ./web/dist

# Runs as an unprivileged user; docker-compose.yml overrides the uid to match ./data.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin assistant \
    && mkdir /data && chown assistant /data
USER assistant
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]
CMD ["python", "-m", "assistant"]
