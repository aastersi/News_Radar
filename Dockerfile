FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install ".[telegram]"

RUN useradd --create-home --uid 10001 radar \
    && mkdir -p /app/data \
    && chown -R radar:radar /app

USER radar

CMD ["qmemo-radar", "run"]

