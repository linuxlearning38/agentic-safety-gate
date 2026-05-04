#Requires -Version 5.1
<#
.SYNOPSIS
    One-command AVA startup: detects WSL IP, brings up Docker, starts AVA,
    waits for a healthy health check, then starts the host runner.

.DESCRIPTION
    Replaces the previous multi-step manual startup sequence with a single
    command:  .\scripts\start-ava.ps1

    Steps performed automatically:
      1. Verify Docker is reachable; start Docker Desktop if not.
      2. Sync WSL2 IP into .env so OLLAMA_HOST is always current.
      3. docker compose up -d
      4. Poll https://localhost:5443/health until healthy (or timeout).
      5. Start the AVA host runner in a minimised window.

.PARAMETER HealthTimeout
    Seconds to wait for AVA's /health endpoint before warning.  Default: 120.

.PARAMETER DockerDesktopPath
    Path to Docker Desktop executable used for auto-start fallback.

.EXAMPLE
    .\scripts\start-ava.ps1
#>

param(
    [int]$HealthTimeout = 120,
    [string]$DockerDesktopPath = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

# ── helpers ───────────────────────────────────────────────────────────────────

function Test-Docker {
    try {
        $null = & docker info 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Wait-Docker {
    param([int]$MaxRetries = 3, [int]$DelaySeconds = 30)
    for ($i = 1; $i -le $MaxRetries; $i++) {
        if (Test-Docker) { return $true }
        Write-Host "  Docker not ready (attempt $i / $MaxRetries) -- waiting ${DelaySeconds}s..."
        if ($i -lt $MaxRetries) { Start-Sleep -Seconds $DelaySeconds }
    }
    return $false
}

# ── Step 1: Docker health ─────────────────────────────────────────────────────

Write-Host "[1/5] Checking Docker..."
if (-not (Wait-Docker -MaxRetries 3 -DelaySeconds 30)) {
    Write-Host "  Docker is not reachable.  Attempting to start Docker Desktop..."
    if (Test-Path $DockerDesktopPath) {
        Start-Process $DockerDesktopPath
        Write-Host "  Waiting up to 90s for Docker Desktop to initialise..."
        Start-Sleep -Seconds 30
        if (-not (Wait-Docker -MaxRetries 4 -DelaySeconds 15)) {
            Write-Error "Docker Desktop did not become ready.  Start it manually and re-run."
            exit 1
        }
    } else {
        Write-Error ("Docker Desktop not found at '" + $DockerDesktopPath + "'.  Start it manually.")
        exit 1
    }
}
Write-Host "  Docker is ready"

# ── Step 2: sync Ollama WSL IP ───────────────────────────────────────────────

Write-Host "[2/5] Syncing Ollama WSL2 IP..."
$syncScript = Join-Path $PSScriptRoot "sync-ollama-host.ps1"
if (Test-Path $syncScript) {
    & $syncScript
} else {
    Write-Warning "sync-ollama-host.ps1 not found -- OLLAMA_HOST uses the .env or compose fallback."
}

# ── Step 3: bring up containers ──────────────────────────────────────────────

Write-Host "[3/5] Starting AVA containers..."
Set-Location $repoRoot
& docker compose up -d 2>&1 | ForEach-Object { Write-Host "  $_" }

# ── Step 4: wait for /health ─────────────────────────────────────────────────

Write-Host "[4/5] Waiting for AVA health check (max ${HealthTimeout}s)..."
$healthUrl = "https://localhost:5443/health"
$waited    = 0
$healthy   = $false

while ($waited -lt $HealthTimeout) {
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 5 -UseBasicParsing `
            -SkipCertificateCheck -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            Write-Host ("  AVA healthy: " + $resp.Content)
            $healthy = $true
            break
        }
    } catch { }
    Start-Sleep -Seconds 5
    $waited += 5
    Write-Host "  ...${waited}s"
}

if (-not $healthy) {
    Write-Warning ("AVA did not become healthy within ${HealthTimeout}s.  " +
                   "Run: docker logs ava-agent --tail 50")
}

# ── Step 5: start host runner in background ───────────────────────────────────

Write-Host "[5/5] Starting AVA host runner..."
$runnerScript = Join-Path $PSScriptRoot "start_host_runner.ps1"
if (Test-Path $runnerScript) {
    $runnerArg = '-NoProfile -ExecutionPolicy Bypass -File "' + $runnerScript + '"'
    Start-Process powershell.exe -ArgumentList $runnerArg -WindowStyle Minimized
    Write-Host "  Host runner started (minimised window)"
} else {
    Write-Warning "start_host_runner.ps1 not found -- start it manually."
}

# ── Summary ───────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "AVA startup complete."
Write-Host "  Web UI : https://localhost:5443"
Write-Host "  Health : https://localhost:5443/health"
if (-not $healthy) {
    Write-Host "  Health check timed out -- check docker logs ava-agent"
}
