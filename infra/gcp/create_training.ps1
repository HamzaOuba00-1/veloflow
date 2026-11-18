<#
.SYNOPSIS
    Weekly retraining on Google Cloud: buckets, identities, Cloud Run job, schedule.

.DESCRIPTION
    What it creates (each step checks first and skips what exists):

    - gs://veloflow-data-513808: private; the VM shares the recent silver days
      and station references there every night (veloflow-share-data).
      Objects older than 60 days are deleted (the VM keeps the full archive).
    - gs://veloflow-models-513808: private; the model registry (versions,
      champion.json). The VM reads it before each forecast run.
    - veloflow-trainer: identity of the job. Reads the data bucket, writes the
      registry. Nothing else.
    - veloflow-scheduler: identity of the schedule. May start the job only.
    - the VM's account (veloflow-publisher) may also write the data bucket and
      read the registry.
    - Cloud Run job veloflow-train (the image of veloflow-api, command
      veloflow-retrain; 2 vCPU, 8 GiB, one hour at most, no retry).
    - Cloud Scheduler job veloflow-train-weekly: Mondays 03:13 UTC.
    - the model now on the laptop (data/models) as the first champion, unless
      the registry already has one.

    Free tier: about 20 minutes of a 2 vCPU job a week, 1 of the 3 free
    Cloud Scheduler jobs, a few MB of storage in us-east1.

    Run it after deploy_cloud_run.ps1, which builds the image with the job's
    command.

.EXAMPLE
    .\infra\gcp\create_training.ps1 -WhatIf
    .\infra\gcp\create_training.ps1
    gcloud run jobs execute veloflow-train --region=us-east1 --wait   # a first run now
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Project = "veloflow-513808",
    [string]$Region = "us-east1",
    [string]$DataBucket = "veloflow-data-513808",
    [string]$ModelsBucket = "veloflow-models-513808",
    [string]$Job = "veloflow-train",
    [string]$Schedule = "13 3 * * 1",
    [string]$LocalModel = "data/models/lightgbm.pkl"
)

$ErrorActionPreference = "Stop"
$Trainer = "veloflow-trainer@$Project.iam.gserviceaccount.com"
$Scheduler = "veloflow-scheduler@$Project.iam.gserviceaccount.com"
$Vm = "veloflow-publisher@$Project.iam.gserviceaccount.com"

function Invoke-Gcloud {
    # gcloud is a PowerShell script on Windows: keep -WhatIf out of it.
    $WhatIfPreference = $false
    gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args[0..2] -join ' ') failed." }
}

function Step([string]$What, [scriptblock]$Do) {
    if ($PSCmdlet.ShouldProcess($What)) { & $Do; Write-Host "OK  $What" }
}

Step "enable Cloud Run and Cloud Scheduler" {
    Invoke-Gcloud services enable run.googleapis.com cloudscheduler.googleapis.com --project=$Project
}

# 1. Private buckets.
foreach ($bucket in $DataBucket, $ModelsBucket) {
    $exists = gcloud storage buckets list --project=$Project --filter="name=$bucket" --format="value(name)"
    if ($exists) { Write-Host "Bucket gs://$bucket already exists."; continue }
    Step "create private bucket gs://$bucket in $Region" {
        Invoke-Gcloud storage buckets create "gs://$bucket" --project=$Project --location=$Region `
            --default-storage-class=STANDARD --uniform-bucket-level-access `
            --public-access-prevention --soft-delete-duration=0
    }
}
$lifecycle = Join-Path $env:TEMP "veloflow-lifecycle.json"
Set-Content -Path $lifecycle -Encoding ascii -Value '{"rule":[{"action":{"type":"Delete"},"condition":{"age":60}}]}'
Step "delete shared data after 60 days in gs://$DataBucket" {
    Invoke-Gcloud storage buckets update "gs://$DataBucket" --lifecycle-file=$lifecycle
}

# 2. Identities, with only what they need.
foreach ($account in @(
        @{ Email = $Trainer; Name = "veloflow-trainer"; Title = "VeloFlow weekly training job" },
        @{ Email = $Scheduler; Name = "veloflow-scheduler"; Title = "VeloFlow schedule: starts the training job" })) {
    $sa = gcloud iam service-accounts list --project=$Project --filter="email=$($account.Email)" --format="value(email)"
    if ($sa) { Write-Host "Service account $($account.Email) already exists."; continue }
    Step "create service account $($account.Email)" {
        Invoke-Gcloud iam service-accounts create $account.Name --project=$Project `
            --display-name=$account.Title
    }
}
$grants = @(
    @{ Bucket = $DataBucket; Member = $Trainer; Role = "roles/storage.objectViewer" },
    @{ Bucket = $ModelsBucket; Member = $Trainer; Role = "roles/storage.objectUser" },
    @{ Bucket = $DataBucket; Member = $Vm; Role = "roles/storage.objectUser" },
    @{ Bucket = $ModelsBucket; Member = $Vm; Role = "roles/storage.objectViewer" }
)
foreach ($grant in $grants) {
    Step "$($grant.Member) gets $($grant.Role) on gs://$($grant.Bucket)" {
        Invoke-Gcloud storage buckets add-iam-policy-binding "gs://$($grant.Bucket)" `
            --member="serviceAccount:$($grant.Member)" --role=$grant.Role --quiet | Out-Null
    }
}

# 3. The job, from the image of the API (built by deploy_cloud_run.ps1).
$image = gcloud run services describe veloflow-api --project=$Project --region=$Region `
    --format="value(spec.template.spec.containers[0].image)"
if (-not $image) { throw "No image for veloflow-api: run deploy_cloud_run.ps1 first." }
Step "deploy Cloud Run job $Job" {
    Invoke-Gcloud run jobs deploy $Job --project=$Project --region=$Region --image=$image `
        --command=veloflow-retrain --service-account=$Trainer --cpu=2 --memory=8Gi `
        --task-timeout=3600 --max-retries=0 `
        --set-env-vars="VELOFLOW_DATA_URL=gs://$DataBucket,VELOFLOW_MODELS_URL=gs://$ModelsBucket" `
        --quiet
}
Step "$Scheduler may start $Job (and nothing else)" {
    Invoke-Gcloud run jobs add-iam-policy-binding $Job --project=$Project --region=$Region `
        --member="serviceAccount:$Scheduler" --role=roles/run.invoker --quiet | Out-Null
}

# 4. The schedule.
$uri = "https://run.googleapis.com/v2/projects/$Project/locations/$Region/jobs/${Job}:run"
$scheduled = gcloud scheduler jobs list --project=$Project --location=$Region `
    --filter="name~$Job-weekly" --format="value(name)"
if ($scheduled) {
    Write-Host "Schedule $Job-weekly already exists."
} else {
    Step "run $Job every week ($Schedule UTC)" {
        Invoke-Gcloud scheduler jobs create http "$Job-weekly" --project=$Project `
            --location=$Region --schedule=$Schedule --time-zone=UTC --uri=$uri `
            --http-method=POST --oauth-service-account-email=$Scheduler
    }
}

# 5. The model copied by hand becomes the first champion.
$pointer = gcloud storage ls "gs://$ModelsBucket/champion.json" 2>$null
if ($pointer) {
    Write-Host "The registry already has a champion."
} elseif (-not (Test-Path $LocalModel)) {
    Write-Host "No $LocalModel: the first run of the job will face persistence."
} else {
    $card = Get-Content ($LocalModel -replace '\.pkl$', '.json') -Raw | ConvertFrom-Json
    $version = ([datetime]$card.trained_at).ToString("yyyyMMdd'T'HHmmss'Z'")
    Step "register $LocalModel as version $version, in service" {
        Invoke-Gcloud storage cp $LocalModel ($LocalModel -replace '\.pkl$', '.json') `
            "gs://$ModelsBucket/versions/$version/"
        $champion = Join-Path $env:TEMP "veloflow-champion.json"
        $entry = @{ version = $version; at = (Get-Date).ToUniversalTime().ToString("s") + "+00:00";
            decision = "registered"; against = $null; reason = "model trained on the laptop" }
        @{ current = $version; previous = $null; history = @($entry) } |
            ConvertTo-Json -Depth 5 | Set-Content -Path $champion -Encoding ascii
        Invoke-Gcloud storage cp $champion "gs://$ModelsBucket/champion.json"
    }
}

Write-Host ""
Write-Host "Add to /etc/veloflow/veloflow.env on the VM:"
Write-Host "  VELOFLOW_DATA_URL=gs://$DataBucket"
Write-Host "  VELOFLOW_MODELS_URL=gs://$ModelsBucket"
