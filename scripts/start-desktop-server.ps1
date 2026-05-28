param(
    [switch]$SkipPull
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not $SkipPull -and (Test-Path ".git")) {
    Write-Host "Updating from GitHub..."
    $trackedChanges = git status --porcelain --untracked-files=no
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Git status check failed. Starting server without pull."
    } elseif ($trackedChanges) {
        Write-Warning "Tracked local changes found. Skipping git pull to avoid overwriting work."
    } else {
        git pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "git pull failed. Starting server with current local files."
        }
    }
    Write-Host ""
}

$env:CRATA_HOST = "0.0.0.0"
$env:CRATA_PORT = "8765"

Write-Host "CRATA desktop execution server"
Write-Host "Host: $env:CRATA_HOST"
Write-Host "Port: $env:CRATA_PORT"
Write-Host "Notebook access through Tailscale: http://<desktop-tailscale-ip>:$env:CRATA_PORT"
Write-Host ""

python server.py
