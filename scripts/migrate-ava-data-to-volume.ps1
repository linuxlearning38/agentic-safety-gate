#Requires -Version 5.1
<#
.SYNOPSIS
    Safely migrates legacy WSL AVA data into the Docker named volume.

.DESCRIPTION
    Default mode is dry-run. Nothing is copied unless -Execute is provided.
    The legacy source is not deleted or modified.

.EXAMPLE
    .\scripts\migrate-ava-data-to-volume.ps1

.EXAMPLE
    .\scripts\migrate-ava-data-to-volume.ps1 -Execute
#>

param(
    [string]$Source = "/home/manoj/ava-data",
    [string]$VolumeName = "ava_data",
    [switch]$Execute,
    [string]$ConfirmText = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $repoRoot

Write-Host "AVA data migration"
Write-Host "Source (WSL): $Source"
Write-Host "Target volume: $VolumeName"
Write-Host "Mode: $(if ($Execute) { 'EXECUTE' } else { 'DRY RUN' })"
Write-Host ""

$volumeNamePattern = "name=^$VolumeName$"
$existingVolume = (& docker volume ls --quiet --filter $volumeNamePattern | Select-Object -First 1)

if ($existingVolume -eq $VolumeName) {
    Write-Host "Docker volume already exists: $VolumeName"
} elseif (-not $Execute) {
    Write-Host "Docker volume does not exist yet: $VolumeName"
    Write-Host "Dry run will not create it."
} else {
    Write-Host "Creating Docker volume: $VolumeName"
    & docker volume create $VolumeName | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "docker volume create failed" }
}

$sourceCheck = & wsl sh -lc "test -d '$Source' && echo exists || echo missing"
if (($sourceCheck | Select-Object -Last 1).Trim() -ne "exists") {
    throw "WSL source directory does not exist: $Source"
}

Write-Host ""
Write-Host "Legacy source preview:"
& wsl sh -lc "du -sh '$Source' 2>/dev/null || true; find '$Source' -maxdepth 1 -mindepth 1 | sed 's#^.*/##' | sort | head -30"

Write-Host ""
if (-not $Execute) {
    Write-Host "Dry run only. No files were copied."
    Write-Host "To copy into the Docker volume, rerun:"
    Write-Host "  .\scripts\migrate-ava-data-to-volume.ps1 -Execute"
    exit 0
}

$answer = $ConfirmText
if (-not $answer) {
    $answer = Read-Host "Copy legacy data into Docker volume '$VolumeName' now? Existing files in the volume may be overwritten. Type YES"
}
if ($answer -ne "YES") {
    Write-Host "Migration cancelled. No files copied."
    exit 0
}

Write-Host "Copying data into Docker named volume..."
$innerCommand = "set -eu; mkdir -p /target; cp -a /source/. /target/; chown -R 999:999 /target; chmod -R ug+rwX /target; find /target -type f -exec chmod ug+rw {} \;"
$copyCommand = "docker run --rm -v ${VolumeName}:/target -v ${Source}:/source:ro alpine:3.20 sh -lc '$innerCommand'"
& wsl sh -lc $copyCommand
$copyExit = $LASTEXITCODE

if ($copyExit -ne 0) {
    throw "Docker volume migration failed with exit code $copyExit"
}

Write-Host "Migration complete. Legacy source was not deleted: $Source"
