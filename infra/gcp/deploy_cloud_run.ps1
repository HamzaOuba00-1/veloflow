<#
.SYNOPSIS
    Build the serving image and deploy the API and the web app on Cloud Run.

.DESCRIPTION
    Cloud Build builds the Dockerfile from the sources (no Docker needed on the
    laptop) and stores the image in Artifact Registry. The same image runs as
    two public services, which read the forecast document from its public URL:

    - veloflow-api: FastAPI (default command), 512 MiB;
    - veloflow-app: Streamlit (command veloflow-app), 1 GiB, session affinity
      (a Streamlit session lives on one instance, over a WebSocket).

    Free-tier choices: us-east1, like the bucket (no inter-region traffic);
    no instance kept warm (min 0: the first visit after a quiet period waits a
    few seconds); at most one instance each. The registry keeps the last two
    images only (Artifact Registry: 0.5 GB free).

    Run from the repository root, after the changes are pushed to main.

.EXAMPLE
    .\infra\gcp\deploy_cloud_run.ps1 -WhatIf
    .\infra\gcp\deploy_cloud_run.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Project = "veloflow-513808",
    [string]$Region = "us-east1",
    [string]$Forecasts = "https://storage.googleapis.com/veloflow-forecasts-513808/forecasts.json",
    [string]$Runtime = "veloflow-run"
)

$ErrorActionPreference = "Stop"
$Repository = "cloud-run-source-deploy"  # created by gcloud on the first --source deploy
# Identity of both services: a service account with no role at all. They only
# read a public document; without it Cloud Run would use the Compute Engine
# default account, which holds Editor on the whole project.
$RuntimeEmail = "$Runtime@$Project.iam.gserviceaccount.com"

function Invoke-Gcloud {
    # gcloud is a PowerShell script on Windows: keep -WhatIf out of it.
    $WhatIfPreference = $false
    gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args[0..2] -join ' ') failed." }
}

function Step([string]$What, [scriptblock]$Do) {
    if ($PSCmdlet.ShouldProcess($What)) { & $Do; Write-Host "OK  $What" }
}

if (-not (Test-Path Dockerfile)) { throw "Run this script from the repository root." }

$common = @(
    "--project=$Project", "--region=$Region", "--allow-unauthenticated",
    "--min-instances=0", "--max-instances=1", "--cpu=1", "--service-account=$RuntimeEmail",
    "--set-env-vars=VELOFLOW_FORECASTS=$Forecasts", "--quiet"
)

Step "enable Cloud Run, Cloud Build and Artifact Registry" {
    Invoke-Gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
        artifactregistry.googleapis.com --project=$Project
}

$sa = gcloud iam service-accounts list --project=$Project --filter="email=$RuntimeEmail" `
    --format="value(email)"
if ($sa) {
    Write-Host "Service account $RuntimeEmail already exists (no role)."
} else {
    Step "create service account $RuntimeEmail, with no role" {
        Invoke-Gcloud iam service-accounts create $Runtime --project=$Project `
            --display-name="VeloFlow API and app on Cloud Run: no permission"
    }
}

Step "build the image and deploy veloflow-api" {
    Invoke-Gcloud run deploy veloflow-api --source . --memory=512Mi @common
}

Step "deploy veloflow-app from the same image" {
    $image = gcloud run services describe veloflow-api --project=$Project --region=$Region `
        --format="value(spec.template.spec.containers[0].image)"
    if (-not $image) { throw "No image found for veloflow-api." }
    Invoke-Gcloud run deploy veloflow-app --image=$image --command=veloflow-app `
        --memory=1Gi --session-affinity --timeout=3600 @common
}

Step "keep the last two images in $Repository" {
    Invoke-Gcloud artifacts repositories set-cleanup-policies $Repository `
        --project=$Project --location=$Region `
        --policy=infra/gcp/registry_cleanup.json --no-dry-run | Out-Null
}

foreach ($service in "veloflow-api", "veloflow-app") {
    $url = gcloud run services describe $service --project=$Project --region=$Region --format="value(status.url)"
    Write-Host "$service : $url"
}
