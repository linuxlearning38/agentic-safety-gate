param(
    [string]$SourceDiskPath,
    [string]$SourceIsoPath,
    [string]$TemplateName = "ubuntu-cloud-image",
    [string]$VmRoot = "I:\ai-lab\virtualbox-vms",
    [string]$VBoxManagePath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    [int]$MemoryMB = 2048,
    [int]$Cpu = 2,
    [int]$DiskGB = 30,
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

if (-not $SourceDiskPath -and -not $SourceIsoPath) {
    throw "Provide either -SourceDiskPath or -SourceIsoPath"
}

if ($SourceDiskPath -and $SourceIsoPath) {
    throw "Provide only one source input: -SourceDiskPath or -SourceIsoPath"
}

if ($SourceDiskPath) {
    if (-not (Test-Path $SourceDiskPath)) {
        throw "Source disk path not found: $SourceDiskPath"
    }

    $sourceExt = [System.IO.Path]::GetExtension($SourceDiskPath).ToLowerInvariant()
    if ($sourceExt -notin @(".vdi", ".vmdk", ".vhd", ".qcow2", ".img")) {
        throw "Unsupported source disk extension: $sourceExt"
    }
}

if ($SourceIsoPath) {
    if (-not (Test-Path $SourceIsoPath)) {
        throw "Source ISO path not found: $SourceIsoPath"
    }

    $isoExt = [System.IO.Path]::GetExtension($SourceIsoPath).ToLowerInvariant()
    if ($isoExt -ne ".iso") {
        throw "SourceIsoPath must point to an .iso file"
    }
}

if ($Cpu -lt 1 -or $Cpu -gt 16) {
    throw "Cpu must be between 1 and 16"
}

if ($MemoryMB -lt 1024 -or $MemoryMB -gt 65536) {
    throw "MemoryMB must be between 1024 and 65536"
}

if ($DiskGB -lt 20 -or $DiskGB -gt 2048) {
    throw "DiskGB must be between 20 and 2048"
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
Invoke-VBoxManage -Arguments @("modifyvm", $TemplateName, "--memory", "$MemoryMB", "--cpus", "$Cpu", "--nic1", "nat")
Invoke-VBoxManage -Arguments @("storagectl", $TemplateName, "--name", "SATA", "--add", "sata", "--controller", "IntelAhci")

if ($SourceDiskPath) {
    Invoke-VBoxManage -Arguments @("modifyvm", $TemplateName, "--boot1", "disk", "--boot2", "none")
    Invoke-VBoxManage -Arguments @("clonemedium", "disk", $SourceDiskPath, $templateDiskPath, "--format", "VDI")
    Invoke-VBoxManage -Arguments @("storageattach", $TemplateName, "--storagectl", "SATA", "--port", "0", "--device", "0", "--type", "hdd", "--medium", $templateDiskPath)
    Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template", "true")
    Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template:source_disk", $SourceDiskPath)

    Write-Host ""
    Write-Host "[PASS] VirtualBox Ubuntu template prepared from source disk."
    Write-Host "Template VM: $TemplateName"
    Write-Host "Template disk: $templateDiskPath"
    Write-Host ""
    Write-Host "Suggested next step:"
    Write-Host "python tests/virtualbox_adapter_live_smoke.py"
    exit 0
}

Invoke-VBoxManage -Arguments @("modifyvm", $TemplateName, "--boot1", "dvd", "--boot2", "disk")
Invoke-VBoxManage -Arguments @("createmedium", "disk", "--filename", $templateDiskPath, "--size", "$($DiskGB * 1024)")
Invoke-VBoxManage -Arguments @("storageattach", $TemplateName, "--storagectl", "SATA", "--port", "0", "--device", "0", "--type", "hdd", "--medium", $templateDiskPath)
Invoke-VBoxManage -Arguments @("storageattach", $TemplateName, "--storagectl", "SATA", "--port", "1", "--device", "0", "--type", "dvddrive", "--medium", $SourceIsoPath)
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template", "bootstrap_pending")
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template:source_iso", $SourceIsoPath)

Write-Host ""
Write-Host "[PASS] VirtualBox Ubuntu template bootstrap VM created from ISO."
Write-Host "Template VM: $TemplateName"
Write-Host "Template disk: $templateDiskPath"
Write-Host "Attached ISO: $SourceIsoPath"
Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Start '$TemplateName' in VirtualBox and complete the Ubuntu Server install once."
Write-Host "2. Shut the VM down cleanly after installation."
Write-Host "3. Re-run this command with -ForceRecreate only if you want to rebuild from scratch."
Write-Host "4. Then run: python tests/virtualbox_adapter_live_smoke.py"
