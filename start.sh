#!/usr/bin/env bash
# Linux / macOS: start Foodbox, using the NVIDIA GPU when one is present.   ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo "Install Docker first: https://docs.docker.com/get-docker/"; exit 1; }
files=(-f compose.yaml)
if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
  files+=(-f compose.gpu.yaml); echo "NVIDIA GPU found: using it."
else
  echo "No NVIDIA GPU found: running on the CPU (slower)."
fi
docker compose "${files[@]}" up -d --pull always
echo "Foodbox: http://localhost:${FOODBOX_PORT:-8800}  (first start downloads the models; watch: docker compose logs -f model-pull)"
