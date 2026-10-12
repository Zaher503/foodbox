# One-shot Windows setup:  .\setup.ps1
# Installs ffmpeg + Ollama (winget), the Python env (uv if present, else python), Chromium, and the AI model.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$total = 5
$script:n = 0
function Step($msg) { $script:n++; Write-Host "`n[$script:n/$total] $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "      $msg" -ForegroundColor Gray }
function Ok($msg)   { Write-Host "      OK: $msg" -ForegroundColor Green }
function Have($c)   { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Run($label, [scriptblock]$cmd) {
  Info "> $label"
  & $cmd
  if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $label" }
}

Write-Host "Foodbox setup - folder: $PSScriptRoot" -ForegroundColor White
$sw = [Diagnostics.Stopwatch]::StartNew()

Step "ffmpeg (audio/video extraction)"
if (Have ffmpeg) { Ok "already installed at $((Get-Command ffmpeg).Source)" }
else { Run "winget install Gyan.FFmpeg" { winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements }; Ok "installed" }

Step "Ollama (local AI model server)"
if (Have ollama) { Ok "already installed at $((Get-Command ollama).Source)" }
else { Run "winget install Ollama.Ollama" { winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements }; Ok "installed" }

Step "Python environment (.venv) and packages"
if (Have uv) {
  Info "Using uv $(uv --version)"
  Run "uv venv --python 3.12" { uv venv --python 3.12 --allow-existing }
  Run "uv pip install -r requirements.txt" { uv pip install -r requirements.txt }
} else {
  Info "uv not found, using python: $(python --version)"
  Run "python -m venv .venv" { python -m venv .venv }
  Run "pip install -r requirements.txt" { .\.venv\Scripts\python.exe -m pip install -r requirements.txt }
}
Ok "packages installed in $PSScriptRoot\.venv"

Step "Chromium browser (fallback for sites behind Cloudflare)"
Run "patchright install chromium" { .\.venv\Scripts\patchright.exe install chromium }
Ok "Chromium ready"

Step "AI model gemma4:e4b (about 7 GB, first run only)"
# winget changes PATH only for new terminals; fall back to the default install path.
$ollama = if (Have ollama) { "ollama" } else { "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
  Info "Starting the Ollama server in the background"
  Start-Process $ollama "serve" -WindowStyle Hidden
  Start-Sleep 4
}
Run "ollama pull gemma4:e4b" { & $ollama pull gemma4:e4b }
Ok "model ready"

$sw.Stop()
Write-Host ("`nSetup finished in {0:mm\:ss}." -f $sw.Elapsed) -ForegroundColor Green
Write-Host "If ffmpeg or Ollama were just installed, open a NEW terminal first (so PATH updates), then run:  .\run.ps1" -ForegroundColor Green
