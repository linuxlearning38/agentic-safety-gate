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

function Ensure-RunnerPythonDependency {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ModuleName,
        [Parameter(Mandatory = $true)]
        [string]$PackageSpec
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & python -c "import $ModuleName" *> $null
        $importExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($importExitCode -eq 0) {
        return
    }

    Write-Host "Installing missing Windows runner dependency: $PackageSpec"
    & python -m pip install --disable-pip-version-check $PackageSpec
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install required Windows runner dependency '$PackageSpec'. Run: python -m pip install $PackageSpec"
    }

    & python -c "import $ModuleName"
    if ($LASTEXITCODE -ne 0) {
        throw "Windows runner dependency '$ModuleName' is still unavailable after install."
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $WorkDir) {
    $WorkDir = Join-Path $repoRoot ".ava-runner"
}

# Kill any stale runner process so the new launch gets fresh code.
# Get-Process does not reliably expose CommandLine on Windows, especially
# under StrictMode, so use CIM for command-line matching.
Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -match '^python(\.exe|3\.exe)?$' -and
        $_.CommandLine -match 'provisioning\.runner\.host_runner|host_runner\.py'
    } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$env:AVA_PROVISIONING_REDIS_URL = $RedisUrl
$env:AVA_VBOXMANAGE_PATH = $VBoxManagePath
$env:AVA_VBOX_TEMPLATE_NAME = $TemplateName
$env:AVA_HOST_RUNNER_WORK_DIR = $WorkDir
$env:AVA_HOST_RUNNER_LOG_PATH = Join-Path $WorkDir "host_runner.log"
$env:AVA_HOST_RUNNER_RETAIN_DEBUG = if ($RetainDebug) { "true" } else { "false" }
if ($MaxJobs -gt 0) {
    $env:AVA_HOST_RUNNER_MAX_JOBS = [string]$MaxJobs
} else {
    Remove-Item Env:\AVA_HOST_RUNNER_MAX_JOBS -ErrorAction SilentlyContinue
}

Write-Host "Starting AVA host runner"
Write-Host "Repo: $repoRoot"
Write-Host "Redis: $RedisUrl"
Write-Host "VBoxManage: $VBoxManagePath"
Write-Host "Template: $TemplateName"
Write-Host "WorkDir: $WorkDir"
Write-Host "Log: $env:AVA_HOST_RUNNER_LOG_PATH"

Set-Location $repoRoot
Ensure-RunnerPythonDependency -ModuleName "redis" -PackageSpec "redis==5.2.1"
python -m provisioning.runner.host_runner
