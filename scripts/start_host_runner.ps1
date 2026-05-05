param(
    [string]$RedisUrl = "redis://127.0.0.1:6379/0",
    [string]$VBoxManagePath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    [string]$TemplateName = "ubuntu-cloud-image",
    [string]$WorkDir = "",
    [switch]$RetainDebug,
    [int]$MaxJobs = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $WorkDir) {
    $WorkDir = Join-Path $repoRoot ".ava-runner"
}

# Kill any stale runner process so the new launch gets fresh code.
Get-Process -Name python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'host_runner' } |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }

$env:AVA_PROVISIONING_REDIS_URL = $RedisUrl
$env:AVA_VBOXMANAGE_PATH = $VBoxManagePath
$env:AVA_VBOX_TEMPLATE_NAME = $TemplateName
$env:AVA_HOST_RUNNER_WORK_DIR = $WorkDir
$env:AVA_HOST_RUNNER_LOG_PATH = Join-Path $WorkDir "host_runner.log"
$env:AVA_HOST_RUNNER_RETAIN_DEBUG = if ($RetainDebug) { "true" } else { "false" }
if ($MaxJobs -gt 0) {
    $env:AVA_HOST_RUNNER_MAX_JOBS = [string]$MaxJobs
}

Write-Host "Starting AVA host runner"
Write-Host "Repo: $repoRoot"
Write-Host "Redis: $RedisUrl"
Write-Host "VBoxManage: $VBoxManagePath"
Write-Host "Template: $TemplateName"
Write-Host "WorkDir: $WorkDir"
Write-Host "Log: $env:AVA_HOST_RUNNER_LOG_PATH"

Set-Location $repoRoot
python -m provisioning.runner.host_runner
