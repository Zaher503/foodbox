# Foodbox

A self-hosted recipe box. Send it a TikTok, YouTube/Shorts or recipe-page link and it saves a clean recipe (title, ingredients, directions) plus the source link and the original video or page, then shows it in a phone-friendly library. Everything, including the AI, runs locally. No cloud API.

## Features

- **Add by link** from a phone Share Sheet shortcut, the web page's "+ Add" box, or a PowerShell script. Duplicate links return the existing recipe.
- **TikTok / YouTube:** yt-dlp downloads the video, faster-whisper transcribes it, frames are read for on-screen text, and a local vision-language model (Ollama, `qwen2.5vl:7b`) structures the recipe.
- **Recipe pages:** uses schema.org Recipe data when present (exact quantities, no AI), otherwise extracts the page text and lets the local model structure it. Pages behind a Cloudflare challenge are fetched with a stealth Chromium (patchright) under Xvfb.
- **No invented numbers:** every quantity, time and nutrition value must appear in the source or it is removed and the recipe is flagged "Needs review".
- **Library:** photo grid, category tabs (Breakfast, Lunch, Dinner, Snack, Dessert, Other), favorites, search, model-suggested and editable tags with a filter row, personal notes.
- **Recipe page:** servings scaler (multiplies the ingredients), metric toggle, numbered directions, prep/cook/total time, collapsible editable nutrition facts, original video player, photo upload / camera, editable cover picture.
- **Installable:** web app manifest and icon for "Add to Home Screen".

## Requirements

- Linux host with an NVIDIA GPU (8 GB VRAM is enough: Whisper `large-v3` and the 7B vision model never run at the same time), Docker with the NVIDIA runtime
- Python 3.11+, `ffmpeg`, `xvfb` (for the Cloudflare fallback), `libnss3`, `libnspr4`
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) on the PATH (keep it updated; TikTok and YouTube change often)

## Setup

```bash
git clone https://github.com/<you>/foodbox /opt/foodbox && cd /opt/foodbox

# 1. Local model server
docker compose up -d
docker exec recipebox-ollama ollama pull qwen2.5vl:7b

# 2. Python environment
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/patchright install chromium

# 3. Secret and data folder
cp .env.example .env   # then put a long random value in RECIPE_KEY
sudo mkdir -p /srv/recipebox && sudo chown $USER /srv/recipebox

# 4. Run (see deploy/recipebox.service for a systemd unit)
cd app && set -a && . ../.env && set +a && ../venv/bin/uvicorn main:app --host 0.0.0.0 --port 8800
```

Open `http://<host>:8800`. Recipes are stored as plain folders under `/srv/recipebox/recipes/` (one `recipe.json` plus media each), so copying a folder is a complete backup.

## Adding recipes

```bash
curl -X POST http://<host>:8800/api/add \
  -H "X-Recipe-Key: $RECIPE_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@someone/video/123", "meal": "Dinner"}'
```

`meal` is optional. The endpoint accepts a whole shared message and uses the first link. On Windows use `scripts/add-recipe.ps1`. For iPhone/iPad/Mac create a Shortcut "Get Contents of URL" (POST, header `X-Recipe-Key`, JSON body `url` = Shortcut Input) and enable it in the Share Sheet.

## Security notes

- **The web UI and every API route except `/api/add` have no login of their own.** Put the app behind an authenticating reverse proxy (Authentik, Authelia, Cloudflare Access, ...) or keep it on a trusted LAN. Expose only `POST /api/add` (which checks the key) without the proxy login if you want phone shortcuts to work from anywhere.
- The fetcher refuses private/LAN addresses (including after redirects). Saved pages are served with a sandboxing Content-Security-Policy.
- Ollama listens on `127.0.0.1` only.

## Layout

```
app/main.py         API, job queue (SQLite), single worker thread, static UI
app/fetch.py        yt-dlp / page download, SSRF checks
app/browser_fetch.py  Cloudflare-challenge fallback (patchright)
app/process.py      Whisper -> frame OCR -> structuring, grounding checks, tags, times, nutrition, servings
app/static/         single-page UI, icons, manifest
compose.yaml        Ollama (GPU)
deploy/             example systemd unit
scripts/            Windows helper to send links
```

## Known limits

Amounts exist only if the source states them. Instagram is not supported. Whisper can mishear cooking words. Reprocessing a recipe discards manual edits except favorite, notes and tags. Tags and servings estimates are model guesses; check them.
