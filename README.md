# Foodbox

A self-hosted recipe box. Send it a TikTok, YouTube/Shorts or recipe-page link and it saves a clean recipe (title, ingredients, directions) plus the source link and the original video or page, then shows it in a phone-friendly library. Everything, including the AI, runs locally. No cloud API.

## Features

- **Add by link** from a phone Share Sheet shortcut, the web page's "+ Add" box, or a PowerShell script. Duplicate links return the existing recipe.
- **TikTok / YouTube:** yt-dlp downloads the video, faster-whisper transcribes it, frames are read for on-screen text, and a local vision-language model (Ollama, `gemma4:e4b`) structures the recipe.
- **Recipe pages:** uses schema.org Recipe data when present (exact quantities, no AI), otherwise extracts the page text and lets the local model structure it. Pages behind a Cloudflare challenge are fetched with a stealth Chromium (patchright) (under Xvfb on Linux).
- **No invented numbers:** every quantity, time and nutrition value must appear in the source or it is removed and the recipe is flagged "Needs review".
- **Library:** photo grid, category tabs (Breakfast, Lunch, Dinner, Snack, Dessert, Other), favorites, search, model-suggested and editable tags with a filter row, personal notes.
- **Recipe page:** servings scaler (multiplies the ingredients), metric toggle, numbered directions, prep/cook/total time, collapsible editable nutrition facts, original video player, photo upload / camera, editable cover picture.
- **Installable:** web app manifest and icon for "Add to Home Screen".

## Requirements

- NVIDIA GPU (8 GB VRAM is enough: Whisper `large-v3` and the 7B vision model never run at the same time)
- **Windows 10/11:** [winget](https://learn.microsoft.com/windows/package-manager/winget/) and Python 3.11+ or [uv](https://docs.astral.sh/uv/). Ollama and ffmpeg are installed by the setup script.
- **Linux:** Docker with the NVIDIA runtime, Python 3.11+. ffmpeg, xvfb and the Chromium libs are installed by `setup.sh` (apt).
- yt-dlp is installed with the Python requirements (keep it updated: `uv pip install -U yt-dlp` or `pip install -U yt-dlp`; TikTok and YouTube change often).

## Setup

### Docker (any PC: Windows, macOS, Linux)

One command starts the app, Ollama and the model download. Install [Docker](https://docs.docker.com/get-docker/), then:

```bash
git clone https://github.com/Zaher503/foodbox && cd foodbox
docker compose up -d          # CPU, works everywhere
```

With an NVIDIA GPU (Linux, or Windows with Docker Desktop + WSL2) use the GPU override, or let the helper script pick:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml up -d
./start.sh                    # Linux / macOS, auto-detects the GPU
.\start.ps1                   # Windows
```

Open <http://localhost:8800>. The first start downloads `gemma4:e4b` (about 10 GB, watch with `docker compose logs -f model-pull`); Whisper downloads on the first video. On CPU (including macOS, where Docker cannot use the GPU) Whisper uses the smaller `small` model and processing is much slower.

No `git clone` needed once the image is published: download only `compose.yaml` (and `compose.gpu.yaml`) and run the same command, which pulls `ghcr.io/zaher503/foodbox`.

| Variable (set in `.env` next to `compose.yaml`) | Default | Meaning |
|---|---|---|
| `RECIPE_KEY` | generated, saved in the data volume (`/data/.env`) | secret for `POST /api/add` |
| `FOODBOX_MODEL` | `gemma4:e4b` | Ollama model |
| `FOODBOX_PORT` | `8800` | host port |
| `FOODBOX_DATA_DIR` | volume `foodbox-data` | recipes; set a folder path to keep them on the host |
| `OLLAMA_DIR`, `HF_DIR` | named volumes | where the models are stored |
| `PUID`, `PGID` | `0` | run the app as this user (set to match the owner of `FOODBOX_DATA_DIR`) |
| `FOODBOX_WHISPER_DEVICE`, `FOODBOX_WHISPER_MODEL` | `auto`, `large-v3` (GPU) / `small` (CPU) | speech model |

Update: `docker compose pull && docker compose up -d`. Back up: copy the recipes folder (or `docker run --rm -v foodbox_foodbox-data:/d -v "$PWD":/b alpine tar czf /b/foodbox-data.tgz -C /d .`).

### Windows (without Docker)

```powershell
git clone https://github.com/<you>/foodbox; cd foodbox
.\setup.ps1      # ffmpeg, Ollama, venv + packages (uv or python), Chromium, gemma4:e4b
.\run.ps1        # http://localhost:8800
```

`setup.ps1` uses `uv` when it is installed, otherwise `python -m venv`. By hand, with uv:

```powershell
uv venv --python 3.12; uv pip install -r requirements.txt; .\.venv\Scripts\patchright install chromium
```

or with plain Python: `python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt`. Install [ffmpeg](https://ffmpeg.org/) and [Ollama](https://ollama.com/download) yourself and run `ollama pull gemma4:e4b`.

### Linux (without Docker)

```bash
git clone https://github.com/<you>/foodbox /opt/foodbox && cd /opt/foodbox
./setup.sh        # apt deps, Ollama container + model, venv + packages, Chromium
sudo mkdir -p /srv/recipebox && sudo chown $USER /srv/recipebox
FOODBOX_DATA=/srv/recipebox venv/bin/uvicorn --app-dir app main:app --host 0.0.0.0 --port 8800
```

For a service, copy `deploy/recipebox.service` to `/etc/systemd/system/` and `sudo systemctl enable --now recipebox`.

### Configuration

| Setting | Default | Notes |
|---|---|---|
| `RECIPE_KEY` | generated into `.env` on first run | secret for `POST /api/add` (`X-Recipe-Key` header) |
| `FOODBOX_DATA` | `./data` | where recipes, trash and the job queue live |

Open `http://<host>:8800`. Recipes are stored as plain folders under `<FOODBOX_DATA>/recipes/` (one `recipe.json` plus media each), so copying a folder is a complete backup.

## Adding recipes

Linux / macOS / Git Bash (`RECIPE_KEY` is in `.env`):

```bash
curl -X POST http://<host>:8800/api/add \
  -H "X-Recipe-Key: $RECIPE_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@someone/video/123", "meal": "Dinner"}'
```

Windows PowerShell (`curl` there is an alias for `Invoke-WebRequest` and does not take these flags, so use `Invoke-RestMethod`):

```powershell
$key = ((Get-Content .env | Select-String RECIPE_KEY).Line -split '=')[1]
Invoke-RestMethod -Method Post -Uri http://localhost:8800/api/add `
  -Headers @{ "X-Recipe-Key" = $key } -ContentType "application/json" `
  -Body '{"url": "https://www.tiktok.com/@someone/video/123", "meal": "Dinner"}'
```

`meal` is optional. The endpoint accepts a whole shared message and uses the first link. On Windows you can also use `scripts/add-recipe.ps1` (put the key from `.env` in `scripts/.secret` and set `FOODBOX_URL`, e.g. `http://localhost:8800`). For iPhone/iPad/Mac create a Shortcut "Get Contents of URL" (POST, header `X-Recipe-Key`, JSON body `url` = Shortcut Input) and enable it in the Share Sheet.

## CI/CD

- **CI** (`.github/workflows/ci.yml`) runs on every push and pull request: `ruff`, compile check, `pytest` smoke tests, compose validation and a Docker build.
- **CD** (`.github/workflows/cd.yml`) runs after CI passes on `main`: builds the multi-arch image (amd64 + arm64), pushes it to `ghcr.io/zaher503/foodbox`, then a self-hosted runner on the production server (`/opt/foodbox`) pulls it and restarts the stack, with a health check.
- Deploy = `git push origin main`. Pull requests never reach the server.

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
Dockerfile          app image
compose.yaml        app + Ollama + model download (compose.gpu.yaml adds the NVIDIA GPU)
start.sh / start.ps1 start the Docker stack, auto-detecting the GPU
.github/workflows/  CI and CD
tests/              smoke tests
setup.ps1 / run.ps1 Windows one-shot setup and launcher
setup.sh            Linux one-shot setup
deploy/             example systemd unit
scripts/            Windows helper to send links
```

## Known limits

Amounts exist only if the source states them. Instagram is not supported. Whisper can mishear cooking words. Reprocessing a recipe discards manual edits except favorite, notes and tags. Tags and servings estimates are model guesses; check them.

## License

[MIT](LICENSE). The dependencies (yt-dlp, faster-whisper, Ollama, the models you pull, etc.) have their own licenses.
