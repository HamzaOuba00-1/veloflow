<#
.SYNOPSIS
    Take Editor away from the Compute Engine default account and close SSH to
    the internet (SSH through IAP only).

.DESCRIPTION
    Two changes, each checked before the next one:

    1. Build identity. Cloud Build builds the Cloud Run image as the Compute
       Engine default account, which Google creates with Editor on the whole
       project. The script grants it roles/run.builder (read the uploaded
       sources, push the image, write build logs), then removes Editor.
       Nothing else runs as this account: the VM runs as veloflow-publisher,
       the Cloud Run services as veloflow-run; the script checks the VM.

    2. SSH. The default network lets the whole internet reach port 22 (and
       3389, unused on Debian). The script opens port 22 to the IAP range
       35.235.240.0/20 only, checks that SSH through IAP works, then disables
       the two default rules. Disabled, not deleted: one command reverts it.
       From then on every gcloud compute ssh / scp needs --tunnel-through-iap.

    Rollback, if needed:
        gcloud compute firewall-rules update default-allow-ssh --no-disabled
        gcloud projects add-iam-policy-binding <project> --role=roles/editor `
            --member=serviceAccount:<number>-compute@developer.gserviceaccount.com

.EXAMPLE
    .\infra\gcp\harden_project.ps1 -WhatIf
    .\infra\gcp\harden_project.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Project = "veloflow-513808",
    [string]$Zone = "us-east1-b",
    [string]$Vm = "veloflow-collector",
    [string]$IapRange = "35.235.240.0/20",
    [string]$IapRule = "veloflow-allow-ssh-from-iap"
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

$number = Invoke-Gcloud projects describe $Project --format="value(projectNumber)"
$compute = "serviceAccount:$number-compute@developer.gserviceaccount.com"

# --- 1. Build identity -------------------------------------------------------

$vmAccounts = Invoke-Gcloud compute instances describe $Vm --project=$Project --zone=$Zone `
    --format="value(serviceAccounts[].email)"
if ("$vmAccounts" -match "-compute@developer") {
    throw "The VM runs as the Compute Engine default account: removing Editor would break it."
}

$roles = Invoke-Gcloud projects get-iam-policy $Project --flatten="bindings[].members" `
    --filter="bindings.members:$compute" --format="value(bindings.role)"

if ($roles -contains "roles/run.builder") {
    Write-Host "The default account already holds roles/run.builder."
} else {
    Step "grant roles/run.builder to the Compute Engine default account" {
        Invoke-Gcloud projects add-iam-policy-binding $Project --member=$compute `
            --role=roles/run.builder --condition=None --quiet | Out-Null
    }
}

if ($roles -contains "roles/editor") {
    Step "remove roles/editor from the Compute Engine default account" {
        Invoke-Gcloud projects remove-iam-policy-binding $Project --member=$compute `
            --role=roles/editor --condition=None --quiet | Out-Null
    }
} else {
    Write-Host "The default account no longer holds Editor."
}

# --- 2. SSH through IAP only -------------------------------------------------

Step "enable the IAP API" {
    Invoke-Gcloud services enable iap.googleapis.com --project=$Project
}

$existing = Invoke-Gcloud compute firewall-rules list --project=$Project `
    --filter="name=$IapRule" --format="value(name)"
if ($existing) {
    Write-Host "Firewall rule $IapRule already exists."
} else {
    Step "open port 22 to $IapRange only (rule $IapRule)" {
        Invoke-Gcloud compute firewall-rules create $IapRule --project=$Project `
            --network=default --direction=INGRESS --action=allow --rules=tcp:22 `
            --source-ranges=$IapRange --description="SSH through Identity-Aware Proxy only"
    }
}

# Never close the old door before the new one is known to open.
Step "check SSH through IAP" {
    Invoke-Gcloud compute ssh $Vm --project=$Project --zone=$Zone --tunnel-through-iap `
        --command="echo ssh-through-iap-ok"
}

foreach ($rule in "default-allow-ssh", "default-allow-rdp") {
    $disabled = Invoke-Gcloud compute firewall-rules list --project=$Project `
        --filter="name=$rule" --format="value(disabled)"
    if (-not $disabled) { Write-Host "Rule $rule does not exist."; continue }
    if ($disabled -eq "True") { Write-Host "Rule $rule is already disabled."; continue }
    Step "disable firewall rule $rule (open to the whole internet)" {
        Invoke-Gcloud compute firewall-rules update $rule --project=$Project --disabled
    }
}

Write-Host ""
Write-Host "From now on: gcloud compute ssh / scp ... --tunnel-through-iap"
