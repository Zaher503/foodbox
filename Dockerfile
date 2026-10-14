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

# Chromium has its own layer ahead of the other requirements, so a dependency bump does not
# download it again. Keep the version equal to the pin in requirements.txt.
ARG PATCHRIGHT_VERSION=1.63.0
RUN pip install patchright==${PATCHRIGHT_VERSION} \
    && patchright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# git fetches the private Keep forks. The token is a BuildKit secret passed through env-only git config,
# so it is never written to a layer or to disk.
RUN --mount=type=secret,id=gh_token \
    apt-get update && apt-get install -y --no-install-recommends git \
    && GIT_CONFIG_COUNT=1 \
       GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/gh_token)@github.com/.insteadOf" \
       GIT_CONFIG_VALUE_0="https://github.com/" \
       pip install -r requirements.txt \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY app ./app

RUN mkdir -p /data /cache && chmod 777 /data /cache
VOLUME ["/data", "/cache"]
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=4)"

CMD ["uvicorn", "--app-dir", "app", "main:app", "--host", "0.0.0.0", "--port", "8800"]
