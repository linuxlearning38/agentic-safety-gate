#Requires -Version 5.1
<#
.SYNOPSIS
    Checks whether AVA auto-start and the Windows host runner are healthy.

.DESCRIPTION
    This script is read-only. It does not start, stop, delete, or modify
    anything. Use it after reboot when AVA is open but provisioning or the web
    console says the host runner is not healthy.

.EXAMPLE
    .\scripts\check-ava-autostart.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$startupDir = [Environment]::GetFolderPath("Startup")
$startupRunner = Join-Path $startupDir "AVA Host Runner.cmd"
$hostRunnerLog = Join-Path $repoRoot ".ava-runner\host_runner.log"
$startupOutLog = Join-Path $repoRoot ".ava-runner\host_runner.startup.out.log"
$startupErrLog = Join-Path $repoRoot ".ava-runner\host_runner.startup.err.log"

function Write-Check {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail = ""
    )

    $status = if ($Ok) { "OK" } else { "WARN" }
    $color = if ($Ok) { "Green" } else { "Yellow" }
    if ($Detail) {
        Write-Host ("[{0}] {1}: {2}" -f $status, $Name, $Detail) -ForegroundColor $color
    } else {
        Write-Host ("[{0}] {1}" -f $status, $Name) -ForegroundColor $color
    }
}

function Invoke-CmdLine {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command
    )

    $output = & cmd.exe /d /c "$Command 2>&1"
    return [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = (($output | Out-String).Trim())
    }
}

function Test-RunnerHeartbeatHasInventory {
    param(
        [string]$Heartbeat,
        [string]$ExpectedFingerprint = ""
    )

    if ([string]::IsNullOrWhiteSpace($Heartbeat)) {
        return $false
    }

    try {
        $parsed = $Heartbeat | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $false
    }

    if (-not $parsed.PSObject.Properties.Name.Contains("metadata")) {
        return $false
    }

    $metadata = $parsed.metadata
    if ($null -eq $metadata) {
        return $false
    }

    if (-not $metadata.PSObject.Properties.Name.Contains("registered_vms")) {
        return $false
    }

    if (-not [string]::IsNullOrWhiteSpace($ExpectedFingerprint)) {
        if (-not $metadata.PSObject.Properties.Name.Contains("runner_code_fingerprint")) {
            return $false
        }

        $actualFingerprint = [string]$metadata.runner_code_fingerprint
        return $actualFingerprint.ToLowerInvariant() -eq $ExpectedFingerprint.ToLowerInvariant()
    }

    return $true
}

function Get-RunnerCodeFingerprint {
    $runnerPath = Join-Path $repoRoot "provisioning\runner\host_runner.py"
    if (-not (Test-Path -LiteralPath $runnerPath)) {
        return ""
    }

    try {
        return ((Get-FileHash -LiteralPath $runnerPath -Algorithm SHA256).Hash.Substring(0, 12)).ToLowerInvariant()
    } catch {
        return ""
    }
}

Write-Host "AVA auto-start health check"
Write-Host "Repo: $repoRoot"
Write-Host ""

$expectedRunnerFingerprint = Get-RunnerCodeFingerprint

$taskOutput = & schtasks.exe /Query /TN "AVA Startup" /FO LIST 2>&1
$taskOk = $LASTEXITCODE -eq 0
Write-Check "Scheduled task 'AVA Startup'" $taskOk ($(if ($taskOk) { "registered" } else { "not registered" }))

$startupFallbackOk = Test-Path -LiteralPath $startupRunner
Write-Check "Startup-folder fallback" $startupFallbackOk $startupRunner

$dockerResult = Invoke-CmdLine 'docker ps --format "{{.Names}} {{.Status}}"'
$dockerOk = $dockerResult.ExitCode -eq 0
Write-Check "Docker CLI" $dockerOk ($(if ($dockerOk) { "responding" } else { $dockerResult.Output }))

if ($dockerOk) {
    $dockerPs = $dockerResult.Output -split "`r?`n"
    $avaLine = $dockerPs | Where-Object { $_ -match "^ava-agent\s" } | Select-Object -First 1
    $redisLine = $dockerPs | Where-Object { $_ -match "^agent_redis\s" } | Select-Object -First 1
    Write-Check "AVA container" ([bool]$avaLine) ($(if ($avaLine) { $avaLine } else { "ava-agent is not running" }))
    Write-Check "Redis container" ([bool]$redisLine) ($(if ($redisLine) { $redisLine } else { "agent_redis is not running" }))

    $healthResult = Invoke-CmdLine 'curl.exe -sk https://localhost:5443/health'
    Write-Check "AVA health endpoint" ($healthResult.ExitCode -eq 0 -and $healthResult.Output -match '"status"\s*:\s*"ok"') $healthResult.Output

    $heartbeatResult = Invoke-CmdLine 'docker exec agent_redis redis-cli GET ava:provisioning:runner:heartbeat'
    $heartbeatOk = $heartbeatResult.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($heartbeatResult.Output)
    Write-Check "Runner heartbeat" $heartbeatOk ($(if ($heartbeatOk) { $heartbeatResult.Output } else { "missing or expired" }))
    if ($heartbeatOk) {
        Write-Check "Runner VirtualBox inventory/current code" (Test-RunnerHeartbeatHasInventory -Heartbeat $heartbeatResult.Output -ExpectedFingerprint $expectedRunnerFingerprint) "required for duplicate hostname checks before approval and stale-runner protection"
    }
}

$runnerProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match "provisioning\.runner\.host_runner"
    })
Write-Check "Host runner process" ([bool]$runnerProcesses) ($(if ($runnerProcesses) { "$($runnerProcesses.Count) process(es)" } else { "not found" }))

Write-Host ""
Write-Host "Useful logs:"
Write-Host "  $hostRunnerLog"
Write-Host "  $startupOutLog"
Write-Host "  $startupErrLog"
