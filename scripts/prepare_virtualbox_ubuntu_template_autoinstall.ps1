param(
    [Parameter(Mandatory = $true)]
    [string]$SourceIsoPath,
    [string]$TemplateName = "ubuntu-cloud-image",
    [string]$VmRoot = "I:\ai-lab\virtualbox-vms",
    [string]$VBoxManagePath = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    [int]$MemoryMB = 4096,
    [int]$Cpu = 2,
    [int]$DiskGB = 30,
    [switch]$StartVm,
    [switch]$ForceRecreate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$BootstrapUsername = "ubuntu"
$BootstrapPasswordPlaintext = "ubuntu"
$BootstrapPasswordHash = '$6$exDY1mhS4KUYCE/2$zmn9ToZwTKLhCw.b4/b.ZRTIZM30JZ4QrOQ2aOXJ8yk96xpcCof0kxKwuX1kqLG/ygbJ1f8wxED22bTL4F46P0'

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

if (-not (Test-Path $SourceIsoPath)) {
    throw "Source ISO path not found: $SourceIsoPath"
}

if ([System.IO.Path]::GetExtension($SourceIsoPath).ToLowerInvariant() -ne ".iso") {
    throw "SourceIsoPath must point to an .iso file"
}

if ($Cpu -lt 1 -or $Cpu -gt 16) {
    throw "Cpu must be between 1 and 16"
}

if ($MemoryMB -lt 2048 -or $MemoryMB -gt 65536) {
    throw "MemoryMB must be between 2048 and 65536"
}

if ($DiskGB -lt 20 -or $DiskGB -gt 2048) {
    throw "DiskGB must be between 20 and 2048"
}

New-Item -ItemType Directory -Force -Path $VmRoot | Out-Null

$templateDir = Join-Path $VmRoot $TemplateName
$templateDiskPath = Join-Path $templateDir "$TemplateName.vdi"
$grubCfgPath = Join-Path $templateDir "$TemplateName-autoinstall-grub.cfg"
$userDataPath = Join-Path $templateDir "$TemplateName-user-data"
$metaDataPath = Join-Path $templateDir "$TemplateName-meta-data"
$visoPath = Join-Path $templateDir "$TemplateName-autoinstall.viso"

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
    throw "Template VM '$TemplateName' already exists. Re-run with -ForceRecreate to rebuild it cleanly."
}

if ($existingVmName -and $ForceRecreate) {
    Write-Host "[INFO] Rebuilding existing template VM '$TemplateName'"
    try {
        & $VBoxManagePath controlvm $TemplateName poweroff | Out-Null
    } catch {
    }
    Start-Sleep -Seconds 2
    Invoke-VBoxManage -Arguments @("unregistervm", $TemplateName, "--delete")
}

if (Test-Path $templateDir) {
    Remove-Item -Recurse -Force $templateDir
}
New-Item -ItemType Directory -Force -Path $templateDir | Out-Null

$grubCfg = @"
set timeout=2
loadfont unicode
set menu_color_normal=white/black
set menu_color_highlight=black/light-gray
menuentry "Autoinstall Ubuntu Server" {
    set gfxpayload=keep
    linux /casper/vmlinuz quiet autoinstall ds=nocloud\;s=/cdrom/nocloud/ ---
    initrd /casper/initrd
}
menuentry "Autoinstall Ubuntu Server (HWE kernel)" {
    set gfxpayload=keep
    linux /casper/hwe-vmlinuz quiet autoinstall ds=nocloud\;s=/cdrom/nocloud/ ---
    initrd /casper/hwe-initrd
}
grub_platform
if [ "`$grub_platform" = "efi" ]; then
menuentry 'Boot from next volume' {
    exit 1
}
menuentry 'UEFI Firmware Settings' {
    fwsetup
}
else
menuentry 'Test memory' {
    linux16 /boot/memtest86+.bin
}
fi
"@

$userData = @"
#cloud-config
autoinstall:
  version: 1
  refresh-installer:
    update: false
  locale: en_US.UTF-8
  keyboard:
    layout: us
  timezone: Asia/Calcutta
  ssh:
    install-server: true
    allow-pw: true
  identity:
    hostname: $TemplateName
    username: $BootstrapUsername
    password: "$BootstrapPasswordHash"
  storage:
    layout:
      name: lvm
  packages:
    - cloud-init
  late-commands:
    - curtin in-target --target=/target systemctl enable ssh
    - curtin in-target --target=/target sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
    - curtin in-target --target=/target systemctl enable cloud-init
  shutdown: poweroff
"@

$metaData = @"
instance-id: $TemplateName
local-hostname: $TemplateName
"@

$viso = @"
--iprt-iso-maker-file-marker-bourne-sh a7f8504f-be8c-4fa5-a2d4-a79f52b64def --file-mode=0444 --dir-mode=0555 --no-file-mode --no-dir-mode --import-iso '$SourceIsoPath' --file-mode=0444 --dir-mode=0555 /boot/grub/grub.cfg=:must-remove: '/boot/grub/grub.cfg=$grubCfgPath' '/nocloud/user-data=$userDataPath' '/nocloud/meta-data=$metaDataPath'
"@

Set-Content -Path $grubCfgPath -Value $grubCfg -Encoding ascii
Set-Content -Path $userDataPath -Value $userData -Encoding ascii
Set-Content -Path $metaDataPath -Value $metaData -Encoding ascii
Set-Content -Path $visoPath -Value $viso -Encoding ascii

Invoke-VBoxManage -Arguments @("createvm", "--name", $TemplateName, "--ostype", "Ubuntu_64", "--basefolder", $VmRoot, "--register")
Invoke-VBoxManage -Arguments @("modifyvm", $TemplateName, "--memory", "$MemoryMB", "--cpus", "$Cpu", "--nic1", "nat", "--boot1", "dvd", "--boot2", "disk")
Invoke-VBoxManage -Arguments @("storagectl", $TemplateName, "--name", "SATA", "--add", "sata", "--controller", "IntelAhci")
Invoke-VBoxManage -Arguments @("createmedium", "disk", "--filename", $templateDiskPath, "--size", "$($DiskGB * 1024)")
Invoke-VBoxManage -Arguments @("storageattach", $TemplateName, "--storagectl", "SATA", "--port", "0", "--device", "0", "--type", "hdd", "--medium", $templateDiskPath)
Invoke-VBoxManage -Arguments @("storageattach", $TemplateName, "--storagectl", "SATA", "--port", "1", "--device", "0", "--type", "dvddrive", "--medium", $visoPath)
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template", "autoinstall_pending")
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template:source_iso", $SourceIsoPath)
Invoke-VBoxManage -Arguments @("setextradata", $TemplateName, "AVA:template:bootstrap_user", $BootstrapUsername)

if ($StartVm) {
    Invoke-VBoxManage -Arguments @("startvm", $TemplateName, "--type", "headless")
}

Write-Host ""
Write-Host "[PASS] VirtualBox Ubuntu autoinstall template VM prepared."
Write-Host "Template VM: $TemplateName"
Write-Host "Template disk: $templateDiskPath"
Write-Host "Bootstrap login after install: $BootstrapUsername / $BootstrapPasswordPlaintext"
Write-Host "Overlay VISO: $visoPath"
Write-Host ""
if ($StartVm) {
    Write-Host "The VM was started headless and should now install automatically."
} else {
    Write-Host "Start the VM when you are ready to run the unattended install."
}
