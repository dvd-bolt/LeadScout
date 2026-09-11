FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/leadscout-browsers
RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && patchright install --with-deps chromium
COPY . .
COPY --from=frontend /build/dist ./web/dist
RUN useradd --create-home --uid 10001 leadscout \
    && mkdir -p /app/data /app/backups \
    && chown -R leadscout:leadscout /app /opt/leadscout-browsers
USER leadscout
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 CMD curl --fail http://127.0.0.1:8000/healthz || exit 1
CMD ["python", "server_app.py"]
