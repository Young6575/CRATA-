$ErrorActionPreference = "Stop"

$env:CRATA_HOST = "0.0.0.0"
$env:CRATA_PORT = "8765"

Write-Host "CRATA desktop execution server"
Write-Host "Host: $env:CRATA_HOST"
Write-Host "Port: $env:CRATA_PORT"
Write-Host "Notebook access through Tailscale: http://<desktop-tailscale-ip>:$env:CRATA_PORT"
Write-Host ""

python server.py
