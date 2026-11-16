FROM node:22-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    YUQING_DATA_DIR=/app/data \
    YUQING_PDF_BROWSER=/usr/bin/chromium

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml ./backend/
COPY backend/yuqing ./backend/yuqing
RUN python -m pip install --no-cache-dir ./backend

COPY --from=frontend-build /app/frontend/dist ./frontend/dist
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data

# Playwright's bundled Node driver needs libatomic at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m playwright --version

ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/re-gion/Su-Heng" \
      org.opencontainers.image.revision="${VCS_REF}"
ENV YUQING_CONTAINER=true

USER appuser

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "yuqing.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
