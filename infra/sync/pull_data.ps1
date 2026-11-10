<#
.SYNOPSIS
    Download new or changed data files from the collector VM to the laptop.

.DESCRIPTION
    Copies the small datasets to data/ on the laptop, with the same layout as
    /var/lib/veloflow on the VM:

        silver/station_status     Parquet, one file per UTC day (~1.8 MB)
        raw/weather_forecast      hourly forecast snapshots (~110 KB a day)
        raw/station_information   station reference, hourly (~250 KB a day)
        quality/coverage          daily coverage report, JSON (~1 KB a day)

    The raw station_status archive (~43 MB a day) stays on the VM: it is the
    backup the silver layer can be rebuilt from (decision D10).

    Incremental: a file is downloaded only when it is missing locally or its
    size differs (today's raw files grow every hour). Each file lands under a
    temporary name first, so an interrupted run never leaves a truncated file
    that looks complete.

.EXAMPLE
    .\infra\sync\pull_data.ps1 -WhatIf   # list what would be downloaded
    .\infra\sync\pull_data.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Zone = "us-east1-b",
    [string]$Name = "veloflow-collector",
    [string]$RemoteRoot = "/var/lib/veloflow",
    [string]$LocalRoot = (Join-Path $PSScriptRoot "..\..\data"),
    [string[]]$Datasets = @("silver/station_status", "raw/weather_forecast", "raw/station_information", "quality/coverage")
)

$ErrorActionPreference = "Stop"

function Invoke-Gcloud {
    # On Windows, gcloud is itself a PowerShell script. Without this line,
    # -WhatIf leaks into it and prints its internal environment changes.
    $WhatIfPreference = $false
    gcloud @args
}

$LocalRoot = [System.IO.Path]::GetFullPath($LocalRoot)

# One SSH call lists every remote file as "<relative path><TAB><bytes>".
# No double quotes inside: Windows PowerShell 5.1 mangles them when passing
# arguments to native programs. Dataset names contain no spaces.
$command = "cd $RemoteRoot && for d in $($Datasets -join ' '); do [ -d `$d ] && find `$d -type f -printf '%p\t%s\n'; done; true"
$lines = Invoke-Gcloud compute ssh $Name --zone=$Zone --command=$command
if ($LASTEXITCODE -ne 0) { throw "Listing the files on the VM failed." }

$remoteFiles = @(
    foreach ($line in $lines) {
        $path, $size = "$line" -split "`t"
        # Anything else on stdout (banners, warnings) is not a file line.
        if ($path -and "$size" -match '^\d+$') {
            [pscustomobject]@{ Path = $path; Size = [long]$size }
        }
    }
)

$downloaded = 0
$bytes = 0
foreach ($file in $remoteFiles) {
    $local = Join-Path $LocalRoot ($file.Path -replace '/', '\')
    if ((Test-Path -LiteralPath $local) -and (Get-Item -LiteralPath $local).Length -eq $file.Size) {
        continue
    }
    if (-not $PSCmdlet.ShouldProcess($file.Path, "download $($file.Size) bytes")) {
        continue
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $local) | Out-Null
    $partial = "$local.part"
    Invoke-Gcloud compute scp --zone=$Zone "${Name}:$RemoteRoot/$($file.Path)" $partial
    if ($LASTEXITCODE -ne 0) { throw "Download of $($file.Path) failed." }
    Move-Item -LiteralPath $partial -Destination $local -Force
    $downloaded++
    $bytes += $file.Size
}

Write-Host ("{0} files on the VM, {1} downloaded ({2:N0} KB) into {3}" -f `
    $remoteFiles.Count, $downloaded, ($bytes / 1KB), $LocalRoot)
