#Requires -Version 5.1
<#
.SYNOPSIS
    Registers Windows startup hooks for AVA startup and seed cleanup.

.DESCRIPTION
    Run this script ONCE as the user who will operate AVA. It installs two
    non-admin startup hooks so AVA has a reliable user-logon recovery path:
    a current-user scheduled task and a Startup-folder fallback.

      "AVA Startup"              starts start-ava.ps1 at user logon.
                                 start-ava.ps1 waits for Docker, starts AVA,
                                 then starts the host runner.
      "AVA Host Runner.cmd"      Startup-folder fallback for the same flow.
      "AVA Cleanup Stale Seeds"  runs cleanup-stale-seeds.ps1 daily at 03:00
                                 when Windows permits task registration.

    After registration, use .\scripts\start-ava.ps1 as your daily startup
    command -- it starts the runner in the background automatically.

.EXAMPLE
    .\scripts\install-runner-task.ps1
#>

param(
    [ValidateRange(0, 30)]
    [int]$StartupDelayMinutes = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot      = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$startAvaScript = Join-Path $repoRoot "scripts\start-ava.ps1"
$runnerScript  = Join-Path $repoRoot "scripts\start_host_runner.ps1"
$cleanupScript = Join-Path $repoRoot "scripts\cleanup-stale-seeds.ps1"
$wrapperDir    = Join-Path $env:LOCALAPPDATA "AVA"
$startAvaWrapper = Join-Path $wrapperDir "start-ava.cmd"
$startAvaDelayedWrapper = Join-Path $wrapperDir "start-ava-delayed.cmd"
$runnerWrapper = Join-Path $wrapperDir "start-host-runner.cmd"
$cleanupWrapper = Join-Path $wrapperDir "cleanup-stale-seeds.cmd"
$startupDir = [Environment]::GetFolderPath("Startup")
$startupRunner = Join-Path $startupDir "AVA Host Runner.cmd"
$startupDelaySeconds = [Math]::Max(0, $StartupDelayMinutes * 60)
$startupDelayForTask = "{0:D4}:00" -f $StartupDelayMinutes

New-Item -ItemType Directory -Path $wrapperDir -Force | Out-Null

@"
@echo off
cd /d "$repoRoot"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$startAvaScript"
"@ | Set-Content -Path $startAvaWrapper -Encoding ASCII

@"
@echo off
timeout /t $startupDelaySeconds /nobreak >nul
call "$startAvaWrapper"
"@ | Set-Content -Path $startAvaDelayedWrapper -Encoding ASCII

@"
@echo off
cd /d "$repoRoot"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$runnerScript" -MaxJobs 0
"@ | Set-Content -Path $runnerWrapper -Encoding ASCII

@"
@echo off
cd /d "$repoRoot"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$cleanupScript"
"@ | Set-Content -Path $cleanupWrapper -Encoding ASCII

function Register-AvaTask {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TaskName,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = & schtasks.exe @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to register '$TaskName': $output"
    }
}

# ── AVA login startup ────────────────────────────────────────────────────────

@"
@echo off
start "AVA Startup" /min "$startAvaDelayedWrapper"
"@ | Set-Content -Path $startupRunner -Encoding ASCII

Write-Host "Registered: 'AVA Host Runner' (delayed startup-folder fallback for AVA + runner)"

try {
    $startupTaskArguments = @(
        "/Create",
        "/TN", "AVA Startup",
        "/SC", "ONLOGON",
        "/TR", $startAvaWrapper,
        "/F"
    )
    if ($StartupDelayMinutes -gt 0) {
        $startupTaskArguments += @("/DELAY", $startupDelayForTask)
    }

    Register-AvaTask `
        -TaskName "AVA Startup" `
        -Arguments $startupTaskArguments
    Write-Host "Registered: 'AVA Startup' (starts AVA + runner at user logon via Task Scheduler)"
    Write-Host "Startup delay: $StartupDelayMinutes minute(s)"
} catch {
    Write-Warning "Could not register AVA Startup scheduled task. Startup-folder fallback is still installed."
    Write-Warning $_
}

# ── AVA Cleanup Stale Seeds task ─────────────────────────────────────────────

try {
    Register-AvaTask `
        -TaskName "AVA Cleanup Stale Seeds" `
        -Arguments @(
            "/Create",
            "/TN", "AVA Cleanup Stale Seeds",
            "/SC", "DAILY",
            "/ST", "03:00",
            "/TR", $cleanupWrapper,
            "/F"
        )
    Write-Host "Registered: 'AVA Cleanup Stale Seeds' (daily at 03:00)"
} catch {
    Write-Warning "Could not register daily cleanup task. Startup still works; run cleanup manually if needed."
    Write-Warning $_
}
Write-Host ""
Write-Host "Generated wrappers:"
Write-Host "  $startAvaWrapper"
Write-Host "  $startAvaDelayedWrapper"
Write-Host "  $runnerWrapper"
Write-Host "  $cleanupWrapper"
Write-Host "  $startupRunner"
Write-Host ""
Write-Host "One-time setup complete.  From now on, use:"
Write-Host "  .\scripts\start-ava.ps1   -- brings up Docker + AVA + runner"
Write-Host "  .\scripts\check-ava-autostart.ps1   -- verifies startup hooks and runner heartbeat"
