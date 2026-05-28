param(
    [switch]$SkipPull,
    [switch]$NoStopExisting
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
$port = "8765"

if (-not $SkipPull -and (Test-Path ".git")) {
    Write-Host "Updating from GitHub..."
    $trackedChanges = git status --porcelain --untracked-files=no
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Git status check failed. Starting server without pull."
    } else {
        if ($trackedChanges) {
            Write-Host "Tracked local changes found. Pulling with autostash..."
            git pull --ff-only --autostash
        } else {
            git pull --ff-only
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "git pull failed. Starting server with current local files."
        }
    }
    Write-Host ""
}

$existing = Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($existing.OwningProcess)" -ErrorAction SilentlyContinue
    $cmd = if ($proc) { [string]$proc.CommandLine } else { "" }
    $isCrataServer = $cmd -like "*server.py*"
    if (-not $isCrataServer) {
        Write-Warning "Port $port is already used by PID $($existing.OwningProcess). Not stopping it because it does not look like this CRATA server."
        Write-Warning "Close that process or run this script with a different port."
        exit 1
    }
    if ($NoStopExisting) {
        Write-Warning "CRATA server is already running on port $port. Stop it first or run without -NoStopExisting."
        exit 1
    }
    Write-Host "Stopping existing CRATA server on port $port (PID $($existing.OwningProcess))..."
    Stop-Process -Id $existing.OwningProcess -Force
    Start-Sleep -Seconds 1
}

$env:CRATA_HOST = "0.0.0.0"
$env:CRATA_PORT = $port

Write-Host "CRATA desktop execution server"
Write-Host "Host: $env:CRATA_HOST"
Write-Host "Port: $env:CRATA_PORT"
Write-Host "Notebook access through Tailscale: http://<desktop-tailscale-ip>:$env:CRATA_PORT"
Write-Host ""

python server.py
