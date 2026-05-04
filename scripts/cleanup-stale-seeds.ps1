#Requires -Version 5.1
<#
.SYNOPSIS
    Removes seed.iso files older than 1 hour from .ava-runner job directories.

.DESCRIPTION
    The host runner intentionally leaves seed.iso on disk when VBoxSVC holds a
    file lock (commit 9946575).  This scheduled cleanup removes those leftovers
    once they are old enough that no active VM can still be using them.

    Files still locked by VBoxSVC are silently skipped and will be retried on
    the next run.  Safe to run while the runner is active.

.PARAMETER AgeHours
    Minimum age in hours before a seed.iso is eligible for deletion.  Default: 1.

.EXAMPLE
    .\scripts\cleanup-stale-seeds.ps1
    .\scripts\cleanup-stale-seeds.ps1 -AgeHours 2
#>

param([int]$AgeHours = 1)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot  = Split-Path -Parent $PSScriptRoot
$runnerDir = Join-Path $repoRoot ".ava-runner"

if (-not (Test-Path $runnerDir)) {
    Write-Host "No .ava-runner directory found -- nothing to clean up."
    exit 0
}

$cutoff  = (Get-Date).AddHours(-$AgeHours)
$found   = 0
$deleted = 0
$skipped = 0

Get-ChildItem -Path $runnerDir -Directory -ErrorAction SilentlyContinue |
ForEach-Object {
    $seedIso = Join-Path $_.FullName "seed.iso"
    if (-not (Test-Path $seedIso)) { return }

    $file = Get-Item $seedIso
    if ($file.LastWriteTime -ge $cutoff) { return }

    $found++
    try {
        Remove-Item $seedIso -Force -ErrorAction Stop
        $deleted++
        $ageMin = [int]((Get-Date) - $file.LastWriteTime).TotalMinutes
        Write-Host "Deleted: $seedIso (age ${ageMin} min)"
    } catch {
        $skipped++
    }
}

$notFound = $found - $deleted - $skipped
Write-Host "Done: $deleted deleted, $skipped skipped (locked), $notFound not found."
