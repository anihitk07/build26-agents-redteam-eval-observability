#Requires -Version 7.0
<#
.SYNOPSIS
Queries Azure Monitor for MAF traces and prints App Insights/Grafana pointers.

.DESCRIPTION
Inspired by ai-observability-starter-kit telemetry scripts:
- queries invoke_agent/chat/tool spans
- exports JSON summary for CI artifacts
- prints portal links for Agents pane and Grafana dashboards
#>
[CmdletBinding()]
param(
    [string]$EnvName,
    [string]$Timespan = "PT2H",
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$OutputPath = (Join-Path $RepoRoot "artifacts\observability\maf-observability.json"),
    [string]$GrafanaDashboardJson = "C:\Flutter\ai-observability-starter-kit\artifacts\grafana\agent-observability-dashboard.json"
)

$ErrorActionPreference = "Stop"

function Get-AzdEnvMap {
    param([string]$WorkingDirectory)
    Push-Location $WorkingDirectory
    try {
        $raw = (& azd env get-values) -split "`n"
    }
    finally {
        Pop-Location
    }

    $map = @{}
    foreach ($line in $raw) {
        if ($line -match '^\s*([A-Z0-9_]+)="?(.*?)"?\s*$') {
            $map[$Matches[1]] = $Matches[2]
        }
    }
    return $map
}

function Run-WorkspaceKql {
    param(
        [string]$WorkspaceId,
        [string]$Query,
        [string]$TimespanIso
    )
    return & az monitor log-analytics query `
        --workspace $WorkspaceId `
        --analytics-query $Query `
        --timespan $TimespanIso `
        --output json 2>&1 | Out-String
}

Push-Location $RepoRoot
try {
    if ($EnvName) {
        & azd env select $EnvName | Out-Host
    }

    $envMap = Get-AzdEnvMap -WorkingDirectory $RepoRoot
    $resourceGroup = $envMap["AZURE_RESOURCE_GROUP"]
    $appInsightsId = $envMap["APPLICATIONINSIGHTS_RESOURCE_ID"]
    $workspaceId = $envMap["LOG_ANALYTICS_WORKSPACE_ID"]

    if (-not $resourceGroup) {
        throw "AZURE_RESOURCE_GROUP missing in azd env values."
    }

    if (-not $appInsightsId) {
        $appInsightsId = (& az resource list -g $resourceGroup --resource-type "microsoft.insights/components" --query "[0].id" -o tsv 2>$null).Trim()
    }
    if (-not $workspaceId) {
        $workspaceId = (& az monitor log-analytics workspace list -g $resourceGroup --query "[0].customerId" -o tsv 2>$null).Trim()
    }

    if (-not $workspaceId) {
        throw "Log Analytics workspace not found. Configure Application Insights workspace-based logging first."
    }

    $queries = [ordered]@{
        invoke_agent_spans = @'
AppDependencies
| where TimeGenerated > ago(2h)
| where Name startswith "invoke_agent"
| summarize total=count(), failed=countif(Success == false), p95_ms=percentile(DurationMs, 95)
'@
        tool_spans = @'
AppDependencies
| where TimeGenerated > ago(2h)
| where Name startswith "execute_tool"
| summarize total=count(), failed=countif(Success == false), p95_ms=percentile(DurationMs, 95) by Name
| order by total desc
'@
        chat_spans = @'
AppDependencies
| where TimeGenerated > ago(2h)
| where Name startswith "chat "
| summarize total=count(), failed=countif(Success == false), p95_ms=percentile(DurationMs, 95) by Name
| order by total desc
'@
        request_overview = @'
AppRequests
| where TimeGenerated > ago(2h)
| summarize total=count(), failed=countif(Success == false), p95_ms=percentile(DurationMs, 95) by Name
| order by total desc
'@
        eval_events = @'
AppEvents
| where TimeGenerated > ago(2h)
| where Name startswith "AgentEval" or Name startswith "AgentRedTeam"
| summarize total=count() by Name
| order by total desc
'@
    }

    $summary = [ordered]@{
        run_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        env_name = $envMap["AZURE_ENV_NAME"]
        resource_group = $resourceGroup
        app_insights_resource_id = $appInsightsId
        log_analytics_workspace_id = $workspaceId
        timespan = $Timespan
        queries = @{}
    }

    foreach ($queryName in $queries.Keys) {
        Write-Host "Running query: $queryName"
        $result = Run-WorkspaceKql -WorkspaceId $workspaceId -Query $queries[$queryName] -TimespanIso $Timespan
        if ($LASTEXITCODE -ne 0) {
            $summary.queries[$queryName] = @{
                success = $false
                error = $result
            }
            continue
        }
        $summary.queries[$queryName] = @{
            success = $true
            raw_json = ($result | ConvertFrom-Json)
        }
    }

    $outDir = Split-Path -Parent $OutputPath
    if (-not (Test-Path $outDir)) {
        New-Item -Path $outDir -ItemType Directory -Force | Out-Null
    }
    $summary | ConvertTo-Json -Depth 20 | Set-Content -Path $OutputPath -Encoding UTF8

    Write-Host ""
    Write-Host "Saved observability report: $OutputPath"
    if ($appInsightsId) {
        $portalBase = "https://portal.azure.com/#@/resource$appInsightsId"
        Write-Host "App Insights overview: $portalBase/overview"
        Write-Host "Agents pane:          $portalBase/agents"
        Write-Host "Grafana pane:         $portalBase/dashboardsWithGrafana"
    }
    if (Test-Path $GrafanaDashboardJson) {
        Write-Host "Dashboard JSON ready for import:"
        Write-Host "  $GrafanaDashboardJson"
    }
}
finally {
    Pop-Location
}
