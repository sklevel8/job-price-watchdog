# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WATCHDOG_UI_HOST=0.0.0.0 \
    WATCHDOG_UI_PORT=8000

WORKDIR /app

COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

COPY src ./src
COPY config ./config
COPY reports ./reports
COPY state ./state
COPY LICENSE README.md ./

RUN useradd --create-home --uid 10001 watchdog \
    && mkdir -p /app/state /app/reports \
    && chown -R watchdog:watchdog /app
USER watchdog

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["python", "src/webapp.py", "--host", "0.0.0.0", "--port", "8000"]
