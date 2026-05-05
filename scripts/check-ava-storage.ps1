#Requires -Version 5.1
<#
.SYNOPSIS
    Non-destructive AVA storage diagnostics.

.DESCRIPTION
    Reports the Docker named volume and legacy WSL bind-mount state without
    deleting, changing permissions, or migrating data.

.PARAMETER WriteTest
    When set, performs a temporary mkdir/rmdir write test inside the existing
    ava_data Docker volume.  Default mode is read-only diagnostics.
#>

param(
    [switch]$WriteTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $repoRoot

Write-Host "AVA storage diagnostic"
Write-Host "Repo: $repoRoot"
Write-Host ""

Write-Host "[1/4] Compose /data mount"
Select-String -Path (Join-Path $repoRoot "docker-compose.yml") -Pattern "ava_data:/data|/home/.*/ava-data:/data" |
    ForEach-Object { Write-Host ("  " + $_.Line.Trim()) }

Write-Host ""
Write-Host "[2/4] Docker named volume"
$volumeExists = $false
try {
    $volumeName = (& docker volume ls --quiet --filter "name=^ava_data$" | Select-Object -First 1)
    if ($volumeName -eq "ava_data") {
        $volumeExists = $true
        Write-Host "  ava_data exists"
        & docker volume inspect ava_data | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Host "  ava_data does not exist yet"
    }
} catch {
    Write-Host ("  docker volume inspect failed: " + $_.Exception.Message)
}

Write-Host ""
Write-Host "[3/4] Container view of /data"
if (-not $volumeExists) {
    Write-Host "  ava_data does not exist yet; run docker compose up to create it."
} elseif (-not $WriteTest) {
    Write-Host "  ava_data exists. Skipping write test in default read-only mode."
    Write-Host "  To run a temporary mkdir/rmdir test, use: .\scripts\check-ava-storage.ps1 -WriteTest"
} else {
    try {
        & docker run --rm --entrypoint sh -v ava_data:/data ava-agent:latest -lc "id; ls -ldn /data; mkdir -p /data/__ava_storage_test && rmdir /data/__ava_storage_test && echo writable"
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Container /data write test failed."
        }
    } catch {
        Write-Warning ("Container /data check failed: " + $_.Exception.Message)
    }
}

Write-Host ""
Write-Host "[4/4] Legacy WSL bind path, if present"
try {
    & wsl sh -lc "if [ -d /home/manoj/ava-data ]; then ls -ldn /home/manoj/ava-data; ls -lan /home/manoj/ava-data | head -20; else echo '/home/manoj/ava-data not found'; fi"
} catch {
    Write-Host ("  WSL check failed: " + $_.Exception.Message)
}
