<#
.SYNOPSIS
    Create the VeloFlow collector VM inside the Google Cloud free tier.

.DESCRIPTION
    Every flag is either a free-tier condition or a deliberate safety choice.
    A wrong flag here is billed, so the script refuses anything outside the
    free tier instead of trusting the defaults of the console.

    Free tier (Compute Engine): one non-preemptible e2-micro per billing
    account in us-west1, us-central1 or us-east1, 30 GB of standard persistent
    disk, no charge for its external IP address.

.EXAMPLE
    .\infra\gcp\create_collector_vm.ps1
    .\infra\gcp\create_collector_vm.ps1 -WhatIf   # print the command only
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Project = "veloflow-513808",
    # us-east1 (South Carolina): the free region closest to Europe, for SSH comfort.
    [string]$Zone = "us-east1-b",
    [string]$Name = "veloflow-collector"
)

$ErrorActionPreference = "Stop"

function Invoke-Gcloud {
    # On Windows, gcloud is itself a PowerShell script. Without this line,
    # -WhatIf leaks into it and prints its internal environment changes.
    $WhatIfPreference = $false
    gcloud @args
}

if ($Zone -notmatch '^us-(west1|central1|east1)-[a-z]$') {
    throw "Zone '$Zone' is outside the free-tier regions (us-west1, us-central1, us-east1)."
}

# Idempotent: running the script twice must not create a second VM
# (a second e2-micro would consume the same 744 free hours and be billed).
# When the project has no VM at all, gcloud warns that the "name" filter key
# matched nothing: that warning is expected and harmless.
$existing = Invoke-Gcloud compute instances list --project=$Project --filter="name=$Name" --format="value(name)"
if ($LASTEXITCODE -ne 0) { throw "gcloud failed while listing instances." }
if ($existing) {
    Write-Host "VM '$Name' already exists in $Project, nothing to do."
    exit 0
}

$flags = @(
    "--project=$Project",
    "--zone=$Zone",
    # The only machine type covered by the free tier.
    "--machine-type=e2-micro",
    # Debian stable; Python is not needed from the OS, uv installs its own.
    "--image-family=debian-13",
    "--image-project=debian-cloud",
    # The default is pd-balanced, which is NOT free. Standard disk, 30 GB max.
    "--boot-disk-type=pd-standard",
    "--boot-disk-size=30GB",
    # Standard network tier: cheaper than Premium for traffic to the internet.
    "--network-tier=STANDARD",
    # Least privilege: the collector calls no Google API, so the VM gets no
    # service account and no credentials an attacker could steal from it.
    "--no-service-account",
    "--no-scopes",
    # The raw archive lives on this disk: no deletion by a mistaken click.
    "--deletion-protection",
    "--labels=app=veloflow,role=collector"
)

if ($PSCmdlet.ShouldProcess("$Project/$Zone/$Name", "gcloud compute instances create")) {
    Invoke-Gcloud compute instances create $Name @flags
    if ($LASTEXITCODE -ne 0) { throw "VM creation failed." }
    Write-Host "VM '$Name' created. Check it in the console before going further."
} else {
    Write-Host "gcloud compute instances create $Name $($flags -join ' ')"
}
