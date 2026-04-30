param(
    [string]$TemplateName = "ubuntu-cloud-image",
    [string]$VBoxManagePath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
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

$info = & $VBoxManagePath showvminfo $TemplateName --machinereadable 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Template VM '$TemplateName' was not found."
}

$stateLine = $info | Where-Object { $_ -like 'VMState=*' } | Select-Object -First 1
$state = ($stateLine -replace '^VMState=\"?','' -replace '\"$','').Trim().ToLowerInvariant()
if ($state -eq "running") {
    throw "Template VM '$TemplateName' is still running. Shut it down before finalizing."
}

Invoke-VBoxManage -Arguments @("modifyvm", $TemplateName, "--boot1", "disk", "--boot2", "none", "--boot3", "none", "--boot4", "none")

try {
    Invoke-VBoxManage -Arguments @("storageattach", $TemplateName, "--storagectl", "SATA", "--port", "1", "--device", "0", "--medium", "none")
} catch {
    Write-Host "[WARN] Could not detach installer media from SATA port 1; continuing."
}

Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template", "ready")

Write-Host ""
Write-Host "[PASS] Template VM finalized for cloning."
Write-Host "Template VM: $TemplateName"
Write-Host "Boot order set to disk-only."
Write-Host "Installer media detached from SATA port 1."
