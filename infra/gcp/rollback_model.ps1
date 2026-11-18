<#
.SYNOPSIS
    Put the previous champion back in service (model registry rollback).

.DESCRIPTION
    Swaps "current" and "previous" in gs://<registry>/champion.json and records
    the rollback in its history. The VM downloads that version at its next
    forecast run (within 10 minutes). Run it again to undo.

.EXAMPLE
    .\infra\gcp\rollback_model.ps1 -WhatIf
    .\infra\gcp\rollback_model.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param([string]$Registry = "gs://veloflow-models-513808")

$ErrorActionPreference = "Stop"

function Invoke-Gcloud {
    $WhatIfPreference = $false
    gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args[0..2] -join ' ') failed." }
}

$pointer = (Invoke-Gcloud storage cat "$Registry/champion.json") -join "`n" | ConvertFrom-Json
if (-not $pointer.previous) { throw "No previous version to go back to." }
Write-Host "In service: $($pointer.current); previous: $($pointer.previous)"

if ($PSCmdlet.ShouldProcess("$Registry/champion.json", "put $($pointer.previous) back in service")) {
    $entry = [pscustomobject]@{
        version = $pointer.previous
        at = (Get-Date).ToUniversalTime().ToString("s") + "+00:00"
        decision = "rolled back"
        against = $null
        reason = "replaces $($pointer.current) by hand"
    }
    $current, $pointer.previous = $pointer.previous, $pointer.current
    $pointer.current = $current
    $pointer.history = @($pointer.history) + $entry
    $file = Join-Path $env:TEMP "veloflow-champion.json"
    $pointer | ConvertTo-Json -Depth 6 | Set-Content -Path $file -Encoding ascii
    Invoke-Gcloud storage cp $file "$Registry/champion.json"
    Write-Host "OK  $current back in service; the VM loads it within 10 minutes."
}
