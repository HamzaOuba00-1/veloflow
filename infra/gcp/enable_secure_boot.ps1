<#
.SYNOPSIS
    Turn on Secure Boot (Shielded VM) on the collector VM.

.DESCRIPTION
    Secure Boot only lets the VM start a boot chain signed by trusted keys, so
    a tampered bootloader or kernel does not start. vTPM and integrity
    monitoring come with it: Cloud Monitoring reports a boot that differs from
    the first trusted one. All three are free.

    The setting can only change while the VM is stopped: the collector stops
    for about two minutes (healthchecks.io allows 10 before it alerts). The
    ephemeral external IP may change; nothing depends on it.

    The script checks first that the boot disk is UEFI compatible (Debian
    images are), and afterwards that the VM booted with Secure Boot on.

.EXAMPLE
    .\infra\gcp\enable_secure_boot.ps1 -WhatIf
    .\infra\gcp\enable_secure_boot.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Project = "veloflow-513808",
    [string]$Zone = "us-east1-b",
    [string]$Vm = "veloflow-collector"
)

$ErrorActionPreference = "Stop"

function Invoke-Gcloud {
    # gcloud is a PowerShell script on Windows: keep -WhatIf out of it.
    $WhatIfPreference = $false
    gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args[0..2] -join ' ') failed." }
}

function Step([string]$What, [scriptblock]$Do) {
    if ($PSCmdlet.ShouldProcess($What)) { & $Do; Write-Host "OK  $What" }
}

$where = @("--project=$Project", "--zone=$Zone")
$on = Invoke-Gcloud compute instances describe $Vm @where `
    --format="value(shieldedInstanceConfig.enableSecureBoot)"
if ($on -eq "True") {
    Write-Host "Secure Boot is already on for $Vm, nothing to do."
    exit 0
}

$disk = Invoke-Gcloud compute instances describe $Vm @where --format="value(disks[0].source.basename())"
$features = Invoke-Gcloud compute disks describe $disk @where --format="value(guestOsFeatures[].type)"
if ("$features" -notmatch "UEFI_COMPATIBLE") {
    throw "The boot disk $disk is not UEFI compatible: Secure Boot would not start it."
}

Step "stop $Vm (the collector pauses for about two minutes)" {
    Invoke-Gcloud compute instances stop $Vm @where
}
Step "turn on Secure Boot, vTPM and integrity monitoring" {
    Invoke-Gcloud compute instances update $Vm @where --shielded-secure-boot `
        --shielded-vtpm --shielded-integrity-monitoring
}
Step "start $Vm" {
    Invoke-Gcloud compute instances start $Vm @where
}
Step "check that the VM booted with Secure Boot on" {
    Start-Sleep -Seconds 30  # SSH answers once the guest has booted
    # The UEFI variable SecureBoot: its last byte is 1 when it is on.
    $efi = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
    $state = Invoke-Gcloud compute ssh $Vm @where --tunnel-through-iap `
        --command="sudo od -An -t u1 $efi | awk '{print `$NF}'; systemctl is-active veloflow-collector"
    Write-Host $state
    if (("$state" -split "\s+")[0] -ne "1") { throw "The VM did not boot with Secure Boot on." }
}
