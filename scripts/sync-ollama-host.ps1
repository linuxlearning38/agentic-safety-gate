#Requires -Version 5.1
<#
.SYNOPSIS
    Detects the current WSL2 IP and writes OLLAMA_HOST to .env so that
    docker-compose picks up the correct address after a WSL restart.

.DESCRIPTION
    WSL2 assigns a new IP on every restart.  docker-compose.yml reads
    OLLAMA_HOST from .env via ${OLLAMA_HOST:-<fallback>} substitution.
    Run this script before `docker compose up` to keep the address current.

.EXAMPLE
    .\scripts\sync-ollama-host.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile  = Join-Path $repoRoot ".env"

# Get the primary WSL2 IP (first address from `wsl hostname -I`)
try {
    $raw   = (& wsl hostname -I 2>$null).Trim()
    $wslIp = ($raw -split '\s+')[0]
} catch {
    Write-Warning "wsl.exe not found or returned an error — keeping existing OLLAMA_HOST."
    exit 0
}

if (-not $wslIp -or $wslIp -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    Write-Warning "Could not parse a valid WSL IP from: '$raw' — keeping existing OLLAMA_HOST."
    exit 0
}

$ollamaHost = "http://${wslIp}:11434"
Write-Host "WSL2 IP: $wslIp  →  OLLAMA_HOST=$ollamaHost"

# Read existing .env, strip any previous OLLAMA_HOST line, append the new one.
$existing = if (Test-Path $envFile) { Get-Content $envFile } else { @() }
$filtered = $existing | Where-Object { $_ -notmatch '^OLLAMA_HOST\s*=' }
($filtered + "OLLAMA_HOST=$ollamaHost") | Set-Content $envFile -Encoding UTF8

Write-Host "Updated $envFile"
