#Requires -Version 5.1
<#
.SYNOPSIS
    Registers Windows Scheduled Tasks for the AVA host runner and seed cleanup.

.DESCRIPTION
    Run this script ONCE as the user who will operate AVA (no admin required
    for per-user tasks).  It creates two tasks:

      "AVA Host Runner"          starts start_host_runner.ps1 at user login
                                 and restarts automatically if it exits.
      "AVA Cleanup Stale Seeds"  runs cleanup-stale-seeds.ps1 daily at 03:00.

    After registration, use .\scripts\start-ava.ps1 as your daily startup
    command -- it starts the runner in the background automatically.

.EXAMPLE
    .\scripts\install-runner-task.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot      = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runnerScript  = Join-Path $repoRoot "scripts\start_host_runner.ps1"
$cleanupScript = Join-Path $repoRoot "scripts\cleanup-stale-seeds.ps1"
$psExe         = "powershell.exe"
$psFlags       = "-NoProfile -ExecutionPolicy Bypass"

# ── AVA Host Runner task ──────────────────────────────────────────────────────

$runnerArg    = $psFlags + ' -File "' + $runnerScript + '" -MaxJobs 0'
$runnerAction = New-ScheduledTaskAction -Execute $psExe -Argument $runnerArg
$runnerTrigger  = New-ScheduledTaskTrigger -AtLogOn
$runnerSettings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName "AVA Host Runner" `
    -Action   $runnerAction `
    -Trigger  $runnerTrigger `
    -Settings $runnerSettings `
    -RunLevel Limited `
    -Force | Out-Null

Write-Host "Registered: 'AVA Host Runner' (starts at login, restarts on exit)"

# ── AVA Cleanup Stale Seeds task ─────────────────────────────────────────────

$cleanArg     = $psFlags + ' -File "' + $cleanupScript + '"'
$cleanAction  = New-ScheduledTaskAction -Execute $psExe -Argument $cleanArg
$cleanTrigger  = New-ScheduledTaskTrigger -Daily -At "03:00"
$cleanSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName "AVA Cleanup Stale Seeds" `
    -Action   $cleanAction `
    -Trigger  $cleanTrigger `
    -Settings $cleanSettings `
    -RunLevel Limited `
    -Force | Out-Null

Write-Host "Registered: 'AVA Cleanup Stale Seeds' (daily at 03:00)"
Write-Host ""
Write-Host "One-time setup complete.  From now on, use:"
Write-Host "  .\scripts\start-ava.ps1   -- brings up Docker + AVA + runner"
