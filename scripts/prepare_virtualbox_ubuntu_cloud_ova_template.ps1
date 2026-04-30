param(
    [string]$TemplateName = "ubuntu-cloud-image",
    [string]$VmRoot = "I:\ai-lab\virtualbox-vms",
    [string]$DownloadDir = "I:\ai-lab\downloads",
    [string]$VBoxManagePath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    [string]$CloudImageUrl = "https://cloud-images.ubuntu.com/releases/jammy/release/ubuntu-22.04-server-cloudimg-amd64.ova",
    [int]$MemoryMB = 2048,
    [int]$Cpu = 2,
    [switch]$ForceRecreate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-VBoxManage {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    Write-Host ("[VBoxManage] " + ($Arguments -join " "))
    & $VBoxManagePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "VBoxManage failed: $($Arguments -join ' ')"
    }
}

function Get-ExistingVmName {
    param([string]$Name)

    $existingList = & $VBoxManagePath list vms
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to list existing VirtualBox VMs"
    }
    foreach ($line in ($existingList -split "`r?`n")) {
        if ($line -match '^"(?<name>[^"]+)"\s+\{') {
            if ($Matches.name -eq $Name) {
                return $Name
            }
        }
    }
    return $null
}

if (-not (Test-Path $VBoxManagePath)) {
    throw "VBoxManage not found: $VBoxManagePath"
}

if ($Cpu -lt 1 -or $Cpu -gt 16) {
    throw "Cpu must be between 1 and 16"
}

if ($MemoryMB -lt 1024 -or $MemoryMB -gt 65536) {
    throw "MemoryMB must be between 1024 and 65536"
}

New-Item -ItemType Directory -Force -Path $VmRoot | Out-Null
New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null

$ovaName = [System.IO.Path]::GetFileName(([System.Uri]$CloudImageUrl).AbsolutePath)
$ovaPath = Join-Path $DownloadDir $ovaName

$existingVm = Get-ExistingVmName -Name $TemplateName
if ($existingVm -and -not $ForceRecreate) {
    throw "Template VM '$TemplateName' already exists. Re-run with -ForceRecreate to rebuild it from the cloud image."
}

if ($existingVm -and $ForceRecreate) {
    Write-Host "[INFO] Rebuilding existing template VM '$TemplateName'"
    try {
        & $VBoxManagePath controlvm $TemplateName poweroff | Out-Null
    } catch {
    }
    Start-Sleep -Seconds 2
    Invoke-VBoxManage -Arguments @("unregistervm", $TemplateName, "--delete")
}

if (-not (Test-Path $ovaPath)) {
    Write-Host "[DOWNLOAD] $CloudImageUrl"
    Write-Host "[TARGET]   $ovaPath"
    Invoke-WebRequest -Uri $CloudImageUrl -OutFile $ovaPath
} else {
    Write-Host "[CACHE] Using existing OVA: $ovaPath"
}

Invoke-VBoxManage -Arguments @(
    "import",
    $ovaPath,
    "--options",
    "importtovdi",
    "--vsys",
    "0",
    "--vmname",
    $TemplateName,
    "--basefolder",
    $VmRoot,
    "--memory",
    "$MemoryMB",
    "--cpus",
    "$Cpu"
)

Invoke-VBoxManage -Arguments @("modifyvm", $TemplateName, "--nic1", "nat", "--boot1", "disk", "--boot2", "none", "--boot3", "none", "--boot4", "none")
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template", "ready")
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template:source", "canonical-cloud-image-ova")
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template:source_url", $CloudImageUrl)

Write-Host ""
Write-Host "[PASS] VirtualBox Ubuntu cloud-image template imported."
Write-Host "Template VM: $TemplateName"
Write-Host "Source OVA: $ovaPath"
Write-Host ""
Write-Host "Suggested next step:"
Write-Host "python tests/virtualbox_adapter_live_smoke.py"
