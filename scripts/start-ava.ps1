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
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $false
}

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

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    foreach ($line in $output) {
        Write-Host "  $line"
    }
    if ($exitCode -ne 0) {
        throw "$FilePath exited with code $exitCode"
    }
}

function Test-AvaHealth {
    param([string]$Url)
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $content = & $curl.Source -k -s --max-time 5 $Url 2>$null
        if ($LASTEXITCODE -eq 0 -and $content) {
            return [pscustomobject]@{ Healthy = $true; Content = $content }
        }
        return [pscustomobject]@{ Healthy = $false; Content = "" }
    }

    try {
        if (-not ([System.Management.Automation.PSTypeName]'AvaTrustAllCertsPolicy').Type) {
            Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class AvaTrustAllCertsPolicy : ICertificatePolicy {
    public bool CheckValidationResult(ServicePoint srvPoint, X509Certificate certificate, WebRequest request, int certificateProblem) {
        return true;
    }
}
"@
        }
        [System.Net.ServicePointManager]::CertificatePolicy = New-Object AvaTrustAllCertsPolicy
        $resp = Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        return [pscustomobject]@{ Healthy = ($resp.StatusCode -eq 200); Content = $resp.Content }
    } catch {
        return [pscustomobject]@{ Healthy = $false; Content = "" }
    }
}

function Test-DockerVolume {
    param([string]$Name)
    $volumeName = (& docker volume ls --quiet --filter "name=^$Name$" | Select-Object -First 1)
    return ($volumeName -eq $Name)
}

function Test-LegacyAvaData {
    try {
        $legacy = & wsl sh -lc "if [ -d /home/manoj/ava-data ] && [ -n \"`$(find /home/manoj/ava-data -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)\" ]; then echo present; else echo absent; fi"
        return (($legacy | Select-Object -Last 1).Trim() -eq "present")
    } catch {
        return $false
    }
}

function Ensure-AvaDataVolume {
    $volumeName = "ava_data"
    if (Test-DockerVolume -Name $volumeName) {
        Write-Host "  Docker volume ready: $volumeName"
        return
    }

    if (Test-LegacyAvaData) {
        throw ("Docker volume '" + $volumeName + "' is missing, but legacy data exists at /home/manoj/ava-data. " +
               "Run .\scripts\migrate-ava-data-to-volume.ps1 -Execute first to preserve state.")
    }

    Write-Host "  Creating empty Docker volume: $volumeName"
    Invoke-Native docker "volume" "create" $volumeName
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
Ensure-AvaDataVolume
Invoke-Native docker "compose" "up" "-d"

# ── Step 4: wait for /health ─────────────────────────────────────────────────

Write-Host "[4/5] Waiting for AVA health check (max ${HealthTimeout}s)..."
$healthUrl = "https://localhost:5443/health"
$waited    = 0
$healthy   = $false

while ($waited -lt $HealthTimeout) {
    $health = Test-AvaHealth -Url $healthUrl
    if ($health.Healthy) {
        Write-Host ("  AVA healthy: " + $health.Content)
        $healthy = $true
        break
    }
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
if (-not $healthy) {
    Write-Warning "Skipping host runner start because AVA is not healthy yet."
} elseif (Test-Path $runnerScript) {
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
