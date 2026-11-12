<#
.SYNOPSIS
    Create the public forecast bucket and let the collector VM write to it.

.DESCRIPTION
    The VM publishes forecasts.json every 10 minutes; the API and the app
    (Cloud Run) read it from its public URL. Every flag is a free-tier
    condition or a least-privilege choice:

    - bucket in us-east1, Standard class: the free tier covers 5 GB of
      regional Standard storage in us-east1, us-west1 and us-central1, and the
      VM is in us-east1 (no inter-region traffic);
    - soft delete off: every upload replaces the object, and 7 days of
      replaced versions would be kept (and counted) for nothing;
    - public read on objects only (allUsers: objectViewer): the forecasts are
      derived from open data (ODbL) and meant to be public; listing the bucket
      is not granted;
    - service account "veloflow-publisher" with one role on this bucket only
      (objectUser: create and replace objects), attached to the VM with the
      Cloud Storage scope alone. No key file is created.

    Attaching a service account requires stopping the VM: the collector
    pauses for about a minute (a gap of a few snapshots in the archive).

    Idempotent: each step checks first and skips what already exists.

.EXAMPLE
    .\infra\gcp\create_forecast_bucket.ps1 -WhatIf   # print the commands only
    .\infra\gcp\create_forecast_bucket.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Project = "veloflow-513808",
    [string]$Zone = "us-east1-b",
    [string]$Vm = "veloflow-collector",
    [string]$Bucket = "veloflow-forecasts-513808",
    [string]$Account = "veloflow-publisher"
)

$ErrorActionPreference = "Stop"
$Region = $Zone -replace '-[a-z]$', ''
$Email = "$Account@$Project.iam.gserviceaccount.com"

function Invoke-Gcloud {
    # gcloud is a PowerShell script on Windows: keep -WhatIf out of it.
    $WhatIfPreference = $false
    gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args[0..2] -join ' ') failed." }
}

function Step([string]$What, [scriptblock]$Do) {
    if ($PSCmdlet.ShouldProcess($What)) { & $Do; Write-Host "OK  $What" }
}

if ($Region -notmatch '^us-(west1|central1|east1)$') {
    throw "Region '$Region' is outside the free-tier regions for Cloud Storage."
}

# 1. Bucket.
$exists = gcloud storage buckets list --project=$Project --filter="name=$Bucket" --format="value(name)"
if ($exists) {
    Write-Host "Bucket gs://$Bucket already exists."
} else {
    Step "create bucket gs://$Bucket in $Region" {
        Invoke-Gcloud storage buckets create "gs://$Bucket" --project=$Project `
            --location=$Region --default-storage-class=STANDARD `
            --uniform-bucket-level-access --soft-delete-duration=0
    }
}

# 2. Public read of objects (the policy binding is idempotent).
Step "allUsers can read objects of gs://$Bucket" {
    Invoke-Gcloud storage buckets add-iam-policy-binding "gs://$Bucket" `
        --member=allUsers --role=roles/storage.objectViewer --quiet | Out-Null
}

# 3. Service account of the VM, allowed to write to this bucket only.
$sa = gcloud iam service-accounts list --project=$Project --filter="email=$Email" --format="value(email)"
if ($sa) {
    Write-Host "Service account $Email already exists."
} else {
    Step "create service account $Email" {
        Invoke-Gcloud iam service-accounts create $Account --project=$Project `
            --display-name="VeloFlow VM: publishes forecasts.json"
    }
}
Step "$Email can write objects of gs://$Bucket (and nothing else)" {
    Invoke-Gcloud storage buckets add-iam-policy-binding "gs://$Bucket" `
        --member="serviceAccount:$Email" --role=roles/storage.objectUser --quiet | Out-Null
}

# 4. Attach it to the VM (stop, set, start).
$current = gcloud compute instances describe $Vm --project=$Project --zone=$Zone `
    --format="value(serviceAccounts[0].email)"
if ($current -eq $Email) {
    Write-Host "VM $Vm already runs as $Email."
} else {
    Step "stop $Vm, attach $Email (Cloud Storage scope only), start it" {
        Invoke-Gcloud compute instances stop $Vm --project=$Project --zone=$Zone
        Invoke-Gcloud compute instances set-service-account $Vm --project=$Project --zone=$Zone `
            --service-account=$Email --scopes=https://www.googleapis.com/auth/devstorage.read_write
        Invoke-Gcloud compute instances start $Vm --project=$Project --zone=$Zone
    }
}

Write-Host ""
Write-Host "Public URL of the document: https://storage.googleapis.com/$Bucket/forecasts.json"
