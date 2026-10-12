#!/usr/bin/env bash
# One-shot Linux setup (NVIDIA GPU + Docker + NVIDIA runtime):  ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null && command -v xvfb-run >/dev/null || sudo apt-get install -y ffmpeg xvfb libnss3 libnspr4
docker compose up -d
docker exec recipebox-ollama ollama pull gemma4:e4b
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/patchright install chromium
echo "Done. Run: FOODBOX_DATA=/srv/recipebox venv/bin/uvicorn --app-dir app main:app --host 0.0.0.0 --port 8800"
