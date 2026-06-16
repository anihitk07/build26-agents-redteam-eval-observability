#Requires -Version 7.0
<#
.SYNOPSIS
Creates (or reuses) a Foundry toolbox and wires its MCP endpoint into both hosted agents.

.DESCRIPTION
- Creates a toolbox using the azd toolbox extension (if missing).
- Configures toolbox tools to include WebIQ MCP (`https://api.microsoft.ai/v3/mcp`).
- Stores the resulting endpoint in azd env values:
  - FIELD_OPS_TOOLBOX_ENDPOINT
  - FIBEY_TOOLBOX_ENDPOINT
  - TOOLBOX_FEATURES
- Optionally redeploys both agents so TOOLBOX_ENDPOINT is available at runtime.
#>
[CmdletBinding()]
param(
    [string]$EnvName,
    [string]$ToolboxName = "shared-agent-tools",
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$WebIqApiKey = $env:WEBIQ_API_KEY,
    [switch]$DeployAgents
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

function Ensure-AzdExtension {
    param([string]$Name)
    $null = & azd extension show $Name 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing azd extension '$Name'..."
        & azd extension install $Name | Out-Host
    }
}

function Set-YamlEnvVarValue {
    param(
        [string]$Path,
        [string]$VarName,
        [string]$Value
    )
    $lines = Get-Content -Path $Path -Encoding UTF8
    $found = $false
    for ($i = 0; $i -lt ($lines.Count - 1); $i++) {
        if ($lines[$i] -match "^\s*-\s*name:\s*$([regex]::Escape($VarName))\s*$") {
            $indent = ([regex]::Match($lines[$i + 1], "^\s*")).Value
            $escapedValue = $Value.Replace('"', '\"')
            $lines[$i + 1] = "$($indent)value: `"$escapedValue`""
            $found = $true
        }
    }
    if (-not $found) {
        throw "Could not find environment variable '$VarName' in $Path"
    }
    Set-Content -Path $Path -Value $lines -Encoding UTF8
}

function Invoke-AzdDeployWithRetry {
    param(
        [string]$ServiceName,
        [int]$MaxAttempts = 2
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        Write-Host "Deploying service '$ServiceName' (attempt $attempt/$MaxAttempts)..."
        & azd deploy $ServiceName --no-prompt | Out-Host
        if ($LASTEXITCODE -eq 0) {
            return
        }
        if ($attempt -lt $MaxAttempts) {
            Write-Host "Deploy failed for '$ServiceName'. Retrying in 15 seconds..."
            Start-Sleep -Seconds 15
        }
    }

    throw "azd deploy failed for service '$ServiceName' after $MaxAttempts attempt(s)."
}

function New-ToolboxSpec {
    param(
        [string]$ApiKey,
        [string]$WebIqServerUrl
    )

    if ([string]::IsNullOrWhiteSpace($ApiKey)) {
        throw "WEBIQ_API_KEY is required to configure the WebIQ toolbox tool. Set env var WEBIQ_API_KEY or pass -WebIqApiKey."
    }

    return @{
        description = "Shared toolbox for field-ops-agent and fibey-coordinator"
        tools       = @(
            @{ type = "toolbox_search_preview" },
            @{
                type             = "mcp"
                server_label     = "WebIQ"
                server_url       = $WebIqServerUrl
                require_approval = "never"
                headers          = @{ "x-apikey" = $ApiKey }
            }
        )
    }
}

function Test-ToolboxHasWebIQ {
    param(
        [object]$Toolbox,
        [string]$WebIqServerUrl
    )

    $tools = @()
    if ($Toolbox.version -and $Toolbox.version.tools) {
        $tools += @($Toolbox.version.tools)
    }
    if ($Toolbox.tools) {
        $tools += @($Toolbox.tools)
    }

    if ($tools.Count -eq 0) {
        return $false
    }

    $hasWebIq = $false
    foreach ($tool in $tools) {
        if ($tool.type -eq "web_search") {
            return $false
        }
        if ($tool.type -eq "mcp" -and (($tool.server_label -eq "WebIQ") -or ($tool.server_url -eq $WebIqServerUrl))) {
            $hasWebIq = $true
        }
    }

    return $hasWebIq
}

Push-Location $RepoRoot
try {
    $webIqServerUrl = "https://api.microsoft.ai/v3/mcp"

    if ($EnvName) {
        & azd env select $EnvName | Out-Host
    }

    Ensure-AzdExtension -Name "azure.ai.agents"
    Ensure-AzdExtension -Name "azure.ai.toolboxes"

    $envMap = Get-AzdEnvMap -WorkingDirectory $RepoRoot
    $projectEndpoint = $envMap["AZURE_AI_PROJECT_ENDPOINT"]
    if (-not $projectEndpoint) {
        throw "AZURE_AI_PROJECT_ENDPOINT is missing. Run 'azd provision' first."
    }

    Write-Host "Using project endpoint: $projectEndpoint"

    $existing = & azd ai toolbox show $ToolboxName --project-endpoint $projectEndpoint --output json 2>$null
    $needsCreate = $true
    if ($LASTEXITCODE -eq 0) {
        $existingObj = $existing | ConvertFrom-Json
        if (Test-ToolboxHasWebIQ -Toolbox $existingObj -WebIqServerUrl $webIqServerUrl) {
            Write-Host "Toolbox '$ToolboxName' already exists with WebIQ configured. Reusing it."
            $needsCreate = $false
        }
        else {
            if ([string]::IsNullOrWhiteSpace($WebIqApiKey)) {
                throw "Toolbox '$ToolboxName' must be recreated for WebIQ, but WEBIQ_API_KEY is missing."
            }
            Write-Host "Toolbox '$ToolboxName' exists but is not configured with WebIQ. Recreating toolbox..."
            & azd ai toolbox delete $ToolboxName --project-endpoint $projectEndpoint --no-prompt --force | Out-Host
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to delete existing toolbox '$ToolboxName' before recreation."
            }
        }
    }

    if ($needsCreate) {
        $spec = New-ToolboxSpec -ApiKey $WebIqApiKey -WebIqServerUrl $webIqServerUrl
        $tmpDir = [System.IO.Path]::GetTempPath()
        if ([string]::IsNullOrWhiteSpace($tmpDir)) {
            throw "Could not resolve a temporary directory for toolbox spec creation."
        }
        $tmpSpec = Join-Path $tmpDir ("toolbox-{0}-{1}.json" -f $ToolboxName, [guid]::NewGuid().ToString("N"))
        $spec | ConvertTo-Json -Depth 10 | Set-Content -Path $tmpSpec -Encoding UTF8
        try {
            Write-Host "Creating toolbox '$ToolboxName'..."
            & azd ai toolbox create $ToolboxName --from-file $tmpSpec --project-endpoint $projectEndpoint --output table | Out-Host
        }
        finally {
            Remove-Item -Path $tmpSpec -ErrorAction SilentlyContinue
        }
    }

    $toolboxEndpoint = "$projectEndpoint/toolboxes/$ToolboxName/mcp?api-version=v1"

    # Some deploy flows require FOUNDRY_PROJECT_ENDPOINT even when AZURE_AI_PROJECT_ENDPOINT exists.
    & azd env set FOUNDRY_PROJECT_ENDPOINT $projectEndpoint | Out-Host
    & azd env set FIELD_OPS_TOOLBOX_ENDPOINT $toolboxEndpoint | Out-Host
    & azd env set FIBEY_TOOLBOX_ENDPOINT $toolboxEndpoint | Out-Host
    & azd env set TOOLBOX_FEATURES "Toolboxes=V1Preview" | Out-Host

    Write-Host ""
    Write-Host "Toolbox endpoint configured for both agents:"
    Write-Host "  $toolboxEndpoint"

    if ($DeployAgents) {
        Write-Host ""
        Write-Host "Redeploying agents with toolbox endpoint..."

        $fieldOpsYaml = Join-Path (Join-Path (Join-Path $RepoRoot "src") "field-ops-agent") "agent.yaml"
        $fibeyYaml = Join-Path (Join-Path (Join-Path $RepoRoot "src") "fibey-coordinator") "agent.yaml"
        $original = @{
            $fieldOpsYaml = Get-Content -Path $fieldOpsYaml -Raw -Encoding UTF8
            $fibeyYaml = Get-Content -Path $fibeyYaml -Raw -Encoding UTF8
        }

        try {
            Set-YamlEnvVarValue -Path $fieldOpsYaml -VarName "TOOLBOX_ENDPOINT" -Value $toolboxEndpoint
            Set-YamlEnvVarValue -Path $fieldOpsYaml -VarName "TOOLBOX_FEATURES" -Value "Toolboxes=V1Preview"
            Set-YamlEnvVarValue -Path $fibeyYaml -VarName "TOOLBOX_ENDPOINT" -Value $toolboxEndpoint
            Set-YamlEnvVarValue -Path $fibeyYaml -VarName "TOOLBOX_FEATURES" -Value "Toolboxes=V1Preview"

            Invoke-AzdDeployWithRetry -ServiceName "field-ops-agent"
            Invoke-AzdDeployWithRetry -ServiceName "fibey-coordinator"
        }
        finally {
            foreach ($path in $original.Keys) {
                Set-Content -Path $path -Value $original[$path] -Encoding UTF8
            }
        }
    }

    Write-Host ""
    Write-Host "Quick checks:"
    Write-Host "  azd ai toolbox show $ToolboxName --project-endpoint $projectEndpoint --output json"
    Write-Host "  azd ai agent invoke field-ops-agent --new-session --new-conversation `"What tools do you have?`""
    Write-Host "  azd ai agent invoke fibey-coordinator --protocol responses --new-session --new-conversation `"What tools do you have?`""
}
finally {
    Pop-Location
}
