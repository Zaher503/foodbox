FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/cache \
    HF_HOME=/cache/hf \
    FOODBOX_DATA=/data \
    FOODBOX_ENV_FILE=/data/.env

# ffmpeg for audio/frames, xvfb for the headed Chromium fallback
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg xvfb xauth ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && patchright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

RUN mkdir -p /data /cache && chmod 777 /data /cache
VOLUME ["/data", "/cache"]
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=4)"

CMD ["uvicorn", "--app-dir", "app", "main:app", "--host", "0.0.0.0", "--port", "8800"]
