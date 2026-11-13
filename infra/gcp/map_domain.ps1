<#
.SYNOPSIS
    Serve the web app at a custom domain (veloflow.spaceh.online by default).

.DESCRIPTION
    Uses Cloud Run domain mapping: free, available in us-east1, with a
    certificate managed by Google. It is a Preview feature (no SLA, possible
    extra latency from some places); the alternative, a global external load
    balancer, costs about 18 USD a month, which the free-tier rule (D22)
    excludes.

    Three steps, two of them outside Google Cloud:

    1. Prove you own the base domain (once): `gcloud domains verify` opens
       Google Search Console, which gives a TXT record to add at the domain's
       DNS host. The Google account must be the one gcloud uses.
    2. This script creates the mapping (idempotent) and prints the DNS record
       to add: for a subdomain, a CNAME to ghs.googlehosted.com.
    3. Once the record is visible, Google issues the certificate (15 minutes to
       a few hours). Run the script again to see the state.

    The run.app address keeps working alongside the custom domain.

.EXAMPLE
    .\infra\gcp\map_domain.ps1 -WhatIf
    .\infra\gcp\map_domain.ps1
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Domain = "veloflow.spaceh.online",
    [string]$Service = "veloflow-app",
    [string]$Project = "veloflow-513808",
    [string]$Region = "us-east1"
)

$ErrorActionPreference = "Stop"
$BaseDomain = ($Domain -split "\.")[-2..-1] -join "."

function Invoke-Gcloud {
    # gcloud is a PowerShell script on Windows: keep -WhatIf out of it.
    $WhatIfPreference = $false
    gcloud @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args[0..3] -join ' ') failed." }
}

# 1. Ownership of the base domain, checked by Google for the current account.
$verified = gcloud domains list-user-verified --format="value(id)"
if ($verified -notcontains $BaseDomain) {
    Write-Host "$BaseDomain is not verified for this Google account yet. Run:"
    Write-Host "    gcloud domains verify $BaseDomain"
    Write-Host "Add the TXT record that Search Console shows at your DNS host, click Verify,"
    Write-Host "then run this script again."
    exit 1
}
Write-Host "OK  $BaseDomain verified"

# 2. The mapping, created once.
$existing = gcloud beta run domain-mappings list --project=$Project --region=$Region `
    --format="value(metadata.name)"
if ($existing -contains $Domain) {
    Write-Host "OK  mapping $Domain -> $Service exists"
}
elseif ($PSCmdlet.ShouldProcess("map $Domain to $Service")) {
    Invoke-Gcloud beta run domain-mappings create --service=$Service --domain=$Domain `
        --project=$Project --region=$Region --quiet
    Write-Host "OK  mapping $Domain -> $Service created"
}

if ($WhatIfPreference) { return }

# 3. The DNS record to add, and where the certificate stands.
Write-Host "`nDNS record to add at the host of $BaseDomain (no proxy, 'DNS only' on Cloudflare):"
Invoke-Gcloud beta run domain-mappings describe --domain=$Domain --project=$Project `
    --region=$Region --format="table(status.resourceRecords[].name, status.resourceRecords[].type, status.resourceRecords[].rrdata)"
Write-Host "`nState (Ready = True once the certificate is issued):"
Invoke-Gcloud beta run domain-mappings describe --domain=$Domain --project=$Project `
    --region=$Region --format="table(status.conditions[].type, status.conditions[].status, status.conditions[].message)"
Write-Host "`nThen: https://$Domain"
