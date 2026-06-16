#Requires -Version 7.0
<#
.SYNOPSIS
Runs end-to-end evaluation for field-ops-agent and fibey-coordinator using azd invoke.

.DESCRIPTION
Evaluates each agent against JSONL test suites and computes keyword-hit scores.
Fails with non-zero exit code when an agent score is below threshold.
#>
[CmdletBinding()]
param(
    [string]$EnvName,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$FieldOpsDataset = (Join-Path $PSScriptRoot "evals\field-ops-agent.eval.jsonl"),
    [string]$FibeyDataset = (Join-Path $PSScriptRoot "evals\fibey-coordinator.eval.jsonl"),
    [double]$FieldOpsThreshold = 0.70,
    [double]$FibeyThreshold = 0.70,
    [int]$MaxCasesPerAgent = 5,
    [string]$OutputPath = (Join-Path $RepoRoot "artifacts\eval\agent-eval-summary.json"),
    [bool]$PublishToAzurePortal = $true,
    [bool]$FailOnPortalPublishError = $true,
    [string]$PortalEventPrefix = "AgentEval"
)

$ErrorActionPreference = "Stop"

function Read-Jsonl {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        throw "Dataset file not found: $Path"
    }
    $items = @()
    foreach ($line in (Get-Content -Path $Path -Encoding UTF8)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $items += ($line | ConvertFrom-Json)
    }
    return $items
}

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

function Invoke-AgentQuery {
    param(
        [string]$AgentName,
        [string]$Query,
        [string]$Protocol
    )

    $args = @("ai", "agent", "invoke", $AgentName, "--new-session", "--new-conversation")
    if ($Protocol) {
        $args += @("--protocol", $Protocol)
    }
    $args += $Query

    $output = (& azd @args 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "Invoke failed for '$AgentName': $output"
    }
    return $output
}

function Score-Case {
    param(
        [string]$ResponseText,
        [object]$Case
    )

    $expected = @()
    if ($Case.expected_keywords) {
        $expected = @($Case.expected_keywords)
    }

    if ($expected.Count -eq 0) {
        return @{
            score = if ([string]::IsNullOrWhiteSpace($ResponseText)) { 0.0 } else { 1.0 }
            matched_keywords = @()
            expected_keywords = @()
        }
    }

    $matched = @()
    foreach ($keyword in $expected) {
        $pattern = [regex]::Escape([string]$keyword)
        if ($ResponseText -match $pattern) {
            $matched += [string]$keyword
        }
    }

    return @{
        score = [math]::Round(($matched.Count / $expected.Count), 4)
        matched_keywords = $matched
        expected_keywords = $expected
    }
}

function Evaluate-Agent {
    param(
        [string]$AgentName,
        [string]$Protocol,
        [string]$DatasetPath,
        [double]$Threshold,
        [int]$Limit
    )

    $cases = Read-Jsonl -Path $DatasetPath
    if ($Limit -gt 0 -and $cases.Count -gt $Limit) {
        $cases = $cases | Select-Object -First $Limit
    }

    Write-Host ""
    Write-Host "Evaluating $AgentName ($($cases.Count) case(s))..."

    $results = @()
    foreach ($case in $cases) {
        $responseText = Invoke-AgentQuery -AgentName $AgentName -Query $case.query -Protocol $Protocol
        $scored = Score-Case -ResponseText $responseText -Case $case
        $results += @{
            id = $case.id
            query = $case.query
            score = $scored.score
            matched_keywords = $scored.matched_keywords
            expected_keywords = $scored.expected_keywords
        }
        Write-Host ("  [{0}] score={1}" -f $case.id, $scored.score)
    }

    $average = if ($results.Count -eq 0) { 0.0 } else { [math]::Round((($results | Measure-Object -Property score -Average).Average), 4) }
    $passed = ($average -ge $Threshold)

    return @{
        agent = $AgentName
        protocol = $Protocol
        threshold = $Threshold
        average_score = $average
        passed = $passed
        case_results = $results
    }
}

function Parse-AppInsightsConnectionString {
    param([string]$ConnectionString)

    if ([string]::IsNullOrWhiteSpace($ConnectionString)) {
        throw "APPLICATIONINSIGHTS_CONNECTION_STRING is empty."
    }

    $map = @{}
    foreach ($part in ($ConnectionString -split ";")) {
        if ($part -match "^\s*([^=]+)=(.*)$") {
            $map[$Matches[1].Trim()] = $Matches[2].Trim()
        }
    }

    $ikey = $map["InstrumentationKey"]
    if ([string]::IsNullOrWhiteSpace($ikey)) {
        throw "InstrumentationKey is missing in APPLICATIONINSIGHTS_CONNECTION_STRING."
    }

    $ingestion = $map["IngestionEndpoint"]
    if ([string]::IsNullOrWhiteSpace($ingestion)) {
        $ingestion = "https://dc.services.visualstudio.com/"
    }
    if (-not $ingestion.EndsWith("/")) {
        $ingestion += "/"
    }

    return @{
        instrumentation_key = $ikey
        ingestion_endpoint = $ingestion
    }
}

function Send-AppInsightsEvent {
    param(
        [string]$InstrumentationKey,
        [string]$IngestionEndpoint,
        [string]$EventName,
        [hashtable]$Properties,
        [hashtable]$Measurements
    )

    $safeProps = @{}
    if ($Properties) {
        foreach ($k in $Properties.Keys) {
            $safeProps[$k] = [string]$Properties[$k]
        }
    }

    $safeMeasures = @{}
    if ($Measurements) {
        foreach ($k in $Measurements.Keys) {
            $safeMeasures[$k] = [double]$Measurements[$k]
        }
    }

    $envelope = @(
        @{
            name = "Microsoft.ApplicationInsights.$InstrumentationKey.Event"
            time = (Get-Date).ToUniversalTime().ToString("o")
            iKey = $InstrumentationKey
            data = @{
                baseType = "EventData"
                baseData = @{
                    ver = 2
                    name = $EventName
                    properties = $safeProps
                    measurements = $safeMeasures
                }
            }
        }
    ) | ConvertTo-Json -Depth 12

    $trackUrl = "${IngestionEndpoint}v2/track"
    $response = Invoke-RestMethod -Method Post -Uri $trackUrl -ContentType "application/json" -Body $envelope
    if ($response.itemsAccepted -lt 1) {
        throw "Application Insights rejected event '$EventName'. itemsReceived=$($response.itemsReceived), itemsAccepted=$($response.itemsAccepted)"
    }
}

function Publish-EvalSummaryToPortal {
    param(
        [hashtable]$Summary,
        [hashtable]$EnvMap,
        [string]$EventPrefix,
        [string]$SummaryPath
    )

    $conn = $EnvMap["APPLICATIONINSIGHTS_CONNECTION_STRING"]
    if ([string]::IsNullOrWhiteSpace($conn)) {
        throw "APPLICATIONINSIGHTS_CONNECTION_STRING missing in azd env values. Cannot publish eval results to Azure portal."
    }

    $cs = Parse-AppInsightsConnectionString -ConnectionString $conn
    $runId = [guid]::NewGuid().ToString()
    $agentCount = [int]($Summary.agents.Count)

    Send-AppInsightsEvent `
        -InstrumentationKey $cs.instrumentation_key `
        -IngestionEndpoint $cs.ingestion_endpoint `
        -EventName "${EventPrefix}RunSummary" `
        -Properties @{
            run_id = $runId
            env_name = $Summary.env_name
            output_path = $SummaryPath
            run_at_utc = $Summary.run_at_utc
        } `
        -Measurements @{
            max_cases_per_agent = $Summary.max_cases_per_agent
            agent_count = $agentCount
        }

    foreach ($agent in $Summary.agents) {
        Send-AppInsightsEvent `
            -InstrumentationKey $cs.instrumentation_key `
            -IngestionEndpoint $cs.ingestion_endpoint `
            -EventName "${EventPrefix}AgentSummary" `
            -Properties @{
                run_id = $runId
                env_name = $Summary.env_name
                agent = $agent.agent
                protocol = $agent.protocol
                passed = $agent.passed
            } `
            -Measurements @{
                average_score = $agent.average_score
                threshold = $agent.threshold
                case_count = [int]($agent.case_results.Count)
            }

        foreach ($caseResult in $agent.case_results) {
            Send-AppInsightsEvent `
                -InstrumentationKey $cs.instrumentation_key `
                -IngestionEndpoint $cs.ingestion_endpoint `
                -EventName "${EventPrefix}CaseResult" `
                -Properties @{
                    run_id = $runId
                    env_name = $Summary.env_name
                    agent = $agent.agent
                    case_id = $caseResult.id
                    query = $caseResult.query
                    matched_keywords = (($caseResult.matched_keywords | ForEach-Object { [string]$_ }) -join "|")
                    expected_keywords = (($caseResult.expected_keywords | ForEach-Object { [string]$_ }) -join "|")
                } `
                -Measurements @{
                    score = $caseResult.score
                    matched_keyword_count = [int]($caseResult.matched_keywords.Count)
                    expected_keyword_count = [int]($caseResult.expected_keywords.Count)
                }
        }
    }
}

Push-Location $RepoRoot
try {
    if ($EnvName) {
        & azd env select $EnvName | Out-Host
    }

    $envMap = Get-AzdEnvMap -WorkingDirectory $RepoRoot

    $summary = [ordered]@{
        run_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        env_name = if ($envMap["AZURE_ENV_NAME"]) { $envMap["AZURE_ENV_NAME"] } else { $EnvName }
        max_cases_per_agent = $MaxCasesPerAgent
        agents = @()
    }

    $fieldOps = Evaluate-Agent `
        -AgentName "field-ops-agent" `
        -Protocol "" `
        -DatasetPath $FieldOpsDataset `
        -Threshold $FieldOpsThreshold `
        -Limit $MaxCasesPerAgent

    $fibey = Evaluate-Agent `
        -AgentName "fibey-coordinator" `
        -Protocol "responses" `
        -DatasetPath $FibeyDataset `
        -Threshold $FibeyThreshold `
        -Limit $MaxCasesPerAgent

    $summary.agents += $fieldOps
    $summary.agents += $fibey

    $outDir = Split-Path -Parent $OutputPath
    if (-not (Test-Path $outDir)) {
        New-Item -Path $outDir -ItemType Directory -Force | Out-Null
    }
    $summary | ConvertTo-Json -Depth 10 | Set-Content -Path $OutputPath -Encoding UTF8

    if ($PublishToAzurePortal) {
        try {
            Publish-EvalSummaryToPortal -Summary $summary -EnvMap $envMap -EventPrefix $PortalEventPrefix -SummaryPath $OutputPath
            Write-Host "Published evaluation summary events to Application Insights."
        }
        catch {
            if ($FailOnPortalPublishError) {
                throw
            }
            Write-Warning "Failed to publish evaluation results to Azure portal: $($_.Exception.Message)"
        }
    }

    Write-Host ""
    Write-Host "Evaluation summary written to: $OutputPath"
    foreach ($agent in $summary.agents) {
        $status = if ($agent.passed) { "PASS" } else { "FAIL" }
        Write-Host ("  {0}: avg={1} threshold={2} => {3}" -f $agent.agent, $agent.average_score, $agent.threshold, $status)
    }

    $failed = @($summary.agents | Where-Object { -not $_.passed })
    if ($failed.Count -gt 0) {
        throw "Evaluation threshold gate failed for: $($failed.agent -join ', ')"
    }
}
finally {
    Pop-Location
}
