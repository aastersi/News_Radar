FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RADAR_DB_PATH=/app/data/radar.db \
    RADAR_SOURCES_PATH=/app/sources.yaml

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install ".[telegram]"

RUN useradd --create-home --uid 10001 radar \
    && mkdir -p /app/data \
    && chown -R radar:radar /app/data

USER radar

VOLUME ["/app/data"]

# Healthy while the scheduler keeps writing its heartbeat into SQLite.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["qmemo-radar", "healthcheck"]

STOPSIGNAL SIGTERM

CMD ["qmemo-radar", "run"]
