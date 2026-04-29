param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDiskPath,

    [string]$TemplateName = "ubuntu-cloud-image",
    [string]$VmRoot = "I:\ai-lab\virtualbox-vms",
    [string]$VBoxManagePath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
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

if (-not (Test-Path $VBoxManagePath)) {
    throw "VBoxManage not found: $VBoxManagePath"
}

if (-not (Test-Path $SourceDiskPath)) {
    throw "Source disk path not found: $SourceDiskPath"
}

$sourceExt = [System.IO.Path]::GetExtension($SourceDiskPath).ToLowerInvariant()
if ($sourceExt -notin @(".vdi", ".vmdk", ".vhd", ".qcow2", ".img")) {
    throw "Unsupported source disk extension: $sourceExt"
}

if ($Cpu -lt 1 -or $Cpu -gt 16) {
    throw "Cpu must be between 1 and 16"
}

if ($MemoryMB -lt 1024 -or $MemoryMB -gt 65536) {
    throw "MemoryMB must be between 1024 and 65536"
}

New-Item -ItemType Directory -Force -Path $VmRoot | Out-Null

$templateDir = Join-Path $VmRoot $TemplateName
$templateDiskPath = Join-Path $templateDir "$TemplateName.vdi"

$existingVmName = $null
$existingList = & $VBoxManagePath list vms
if ($LASTEXITCODE -ne 0) {
    throw "Unable to list existing VirtualBox VMs"
}
foreach ($line in ($existingList -split "`r?`n")) {
    if ($line -match '^"(?<name>[^"]+)"\s+\{') {
        if ($Matches.name -eq $TemplateName) {
            $existingVmName = $TemplateName
            break
        }
    }
}

if ($existingVmName -and -not $ForceRecreate) {
    throw "Template VM '$TemplateName' already exists. Re-run with -ForceRecreate if you want to rebuild it."
}

if ($existingVmName -and $ForceRecreate) {
    Write-Host "[INFO] Rebuilding existing template VM '$TemplateName'"
    try {
        & $VBoxManagePath controlvm $TemplateName poweroff | Out-Null
    } catch {
    }
    Invoke-VBoxManage -Arguments @("unregistervm", $TemplateName, "--delete")
}

if (Test-Path $templateDir) {
    Remove-Item -Recurse -Force $templateDir
}
New-Item -ItemType Directory -Force -Path $templateDir | Out-Null

Invoke-VBoxManage -Arguments @("createvm", "--name", $TemplateName, "--ostype", "Ubuntu_64", "--basefolder", $VmRoot, "--register")
Invoke-VBoxManage -Arguments @("modifyvm", $TemplateName, "--memory", "$MemoryMB", "--cpus", "$Cpu", "--nic1", "nat", "--boot1", "disk", "--boot2", "none")
Invoke-VBoxManage -Arguments @("storagectl", $TemplateName, "--name", "SATA", "--add", "sata", "--controller", "IntelAhci")
Invoke-VBoxManage -Arguments @("clonemedium", "disk", $SourceDiskPath, $templateDiskPath, "--format", "VDI")
Invoke-VBoxManage -Arguments @("storageattach", $TemplateName, "--storagectl", "SATA", "--port", "0", "--device", "0", "--type", "hdd", "--medium", $templateDiskPath)
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template", "true")
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template:source_disk", $SourceDiskPath)

Write-Host ""
Write-Host "[PASS] VirtualBox Ubuntu template prepared."
Write-Host "Template VM: $TemplateName"
Write-Host "Template disk: $templateDiskPath"
Write-Host ""
Write-Host "Suggested next step:"
Write-Host "python tests/virtualbox_adapter_live_smoke.py"
