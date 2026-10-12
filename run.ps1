# Start Foodbox on http://localhost:8800   (data goes in .\data, API key in .env - created on first run)
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"   # Windows defaults to cp1252, which breaks on fractions like ⅓
if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) { Start-Process ollama "serve" -WindowStyle Hidden }
.\.venv\Scripts\python.exe -m uvicorn --app-dir app main:app --host 0.0.0.0 --port 8800
