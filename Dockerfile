# Stage 1: Build wheel and install into venv
FROM python:3.12-slim AS builder

WORKDIR /build

# uv is only used to export the exact, tested versions from uv.lock
RUN pip install --no-cache-dir "uv>=0.5,<1"

# Copy only what's needed for the build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/

# Install the locked dependency set (runtime + agent extra) into a clean venv,
# then the app itself without re-resolving dependencies. Installing from
# pyproject ranges let pip pull new major versions (mcp 2.x broke startup).
RUN uv export --frozen --no-dev --extra agent --no-emit-project --no-hashes -o /tmp/requirements.txt \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && /opt/venv/bin/pip install --no-cache-dir --no-deps .
ENV PATH="/opt/venv/bin:$PATH"


# Stage 2: Runtime image
FROM python:3.12-slim

ARG APP_VERSION=dev
ARG GIT_SHA=unknown
ARG BUILD_DATE=

LABEL org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# Create non-root user
RUN groupadd --gid 1000 maillard && \
    useradd --uid 1000 --gid maillard --create-home maillard

WORKDIR /app

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    APP_VERSION="${APP_VERSION}" \
    GIT_SHA="${GIT_SHA}" \
    BUILD_DATE="${BUILD_DATE}"

# Copy static assets (dashboard HTML/JS/CSS) and data files (themes, fonts)
COPY src/maillard/web/static/ /opt/venv/lib/python3.12/site-packages/maillard/web/static/
COPY src/maillard/web/label-styles/ /opt/venv/lib/python3.12/site-packages/maillard/web/label-styles/
COPY src/maillard/web/fonts/ /opt/venv/lib/python3.12/site-packages/maillard/web/fonts/

COPY scripts/backfill_roast_events.py /app/scripts/backfill_roast_events.py
COPY scripts/fit_calibration.py /app/scripts/fit_calibration.py
COPY scripts/cleanup_roast_history.py /app/scripts/cleanup_roast_history.py
COPY scripts/import_green_counts.py /app/scripts/import_green_counts.py

EXPOSE 8080

# Health check using Python stdlib (no curl needed in slim image)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]

USER maillard

ENTRYPOINT ["maillard"]
CMD ["web", "--host", "0.0.0.0", "--port", "8080"]
