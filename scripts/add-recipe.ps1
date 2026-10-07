# Send a recipe link to Foodbox.   Usage:  .\add-recipe.ps1 <link> [-Meal Dinner]
# Set FOODBOX_URL (e.g. https://food.example.com) and put the API key in a file named .secret next to this script.
# With no link it uses the link on the clipboard.
param([string]$Url, [ValidateSet("Breakfast","Lunch","Dinner","Snack","Dessert","Other")][string]$Meal)
if (-not $env:FOODBOX_URL) { Write-Host "Set the FOODBOX_URL environment variable first." -ForegroundColor Red; exit 1 }
$key = (Get-Content "$PSScriptRoot\.secret" -Raw).Trim()
if (-not $Url) { $Url = (Get-Clipboard -Raw).Trim() }
if ($Url -notmatch '^https?://') { Write-Host "No link given and the clipboard has no link." -ForegroundColor Red; exit 1 }
$body = @{ url = $Url }
if ($Meal) { $body.meal = $Meal }
try {
  $r = Invoke-RestMethod -Method Post -Uri "$($env:FOODBOX_URL)/api/add" `
        -Headers @{ "X-Recipe-Key" = $key } -ContentType "application/json" `
        -Body ($body | ConvertTo-Json) -TimeoutSec 20
  if ($r.status -eq "exists") { Write-Host "Already saved." -ForegroundColor Yellow }
  else { Write-Host "Saved. It will appear in a minute or two." -ForegroundColor Green }
} catch { Write-Host "Failed: $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
