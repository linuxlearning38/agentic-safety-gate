param(
    [Parameter(Mandatory = $true)]
    [string]$SeedDir,

    [Parameter(Mandatory = $true)]
    [string]$OutputIsoPath,

    [string]$VolumeName = "CIDATA"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $SeedDir -PathType Container)) {
    throw "SeedDir does not exist or is not a directory: $SeedDir"
}

$userDataPath = Join-Path $SeedDir "user-data"
$metaDataPath = Join-Path $SeedDir "meta-data"
if (-not (Test-Path -LiteralPath $userDataPath -PathType Leaf)) {
    throw "SeedDir is missing required cloud-init file: user-data"
}
if (-not (Test-Path -LiteralPath $metaDataPath -PathType Leaf)) {
    throw "SeedDir is missing required cloud-init file: meta-data"
}

$resolvedSeedDir = (Resolve-Path -LiteralPath $SeedDir).Path
$outputParent = Split-Path -Parent $OutputIsoPath
if ($outputParent) {
    New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
}
$resolvedOutputPath = [System.IO.Path]::GetFullPath($OutputIsoPath)

if (-not ("AvaComStreamWriter" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class AvaComStreamWriter
{
    public static void WriteToFile(object comStream, string path)
    {
        IStream stream = (IStream)comStream;
        System.Runtime.InteropServices.ComTypes.STATSTG stat;
        stream.Stat(out stat, 1);

        byte[] buffer = new byte[8192];
        IntPtr bytesReadPtr = Marshal.AllocHGlobal(sizeof(int));
        try
        {
            using (var output = File.Open(path, FileMode.Create, FileAccess.Write))
            {
                long remaining = stat.cbSize;
                while (remaining > 0)
                {
                    int toRead = (int)Math.Min(buffer.Length, remaining);
                    stream.Read(buffer, toRead, bytesReadPtr);
                    int bytesRead = Marshal.ReadInt32(bytesReadPtr);
                    if (bytesRead <= 0)
                    {
                        break;
                    }
                    output.Write(buffer, 0, bytesRead);
                    remaining -= bytesRead;
                }
            }
        }
        finally
        {
            Marshal.FreeHGlobal(bytesReadPtr);
        }
    }
}
"@
}

$fileSystemImage = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
# ISO9660 + Joliet. No external ISO tooling is required on the Windows host.
$fileSystemImage.FileSystemsToCreate = 3
$fileSystemImage.VolumeName = $VolumeName
$fileSystemImage.Root.AddTree($resolvedSeedDir, $false)

$resultImage = $fileSystemImage.CreateResultImage()
[AvaComStreamWriter]::WriteToFile($resultImage.ImageStream, $resolvedOutputPath)

$created = Get-Item -LiteralPath $resolvedOutputPath
if ($created.Length -le 0) {
    throw "Seed ISO was created but is empty: $resolvedOutputPath"
}

Write-Host "Created cloud-init seed ISO: $resolvedOutputPath"
Write-Host "Volume label: $VolumeName"
Write-Host "Size bytes: $($created.Length)"
