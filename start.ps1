# Windows: start Foodbox in Docker, using the NVIDIA GPU when one is present.   .\start.ps1
Set-Location $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Host "Install Docker Desktop first: https://docs.docker.com/desktop/"; exit 1 }
$files = @("-f", "compose.yaml")
if ((Get-Command nvidia-smi -ErrorAction SilentlyContinue) -and (nvidia-smi -L 2>$null)) {
    $files += @("-f", "compose.gpu.yaml"); Write-Host "NVIDIA GPU found: using it."
} else { Write-Host "No NVIDIA GPU found: running on the CPU (slower)." }
docker compose @files up -d --pull always
Write-Host "Foodbox: http://localhost:8800  (first start downloads the models; watch: docker compose logs -f model-pull)"
