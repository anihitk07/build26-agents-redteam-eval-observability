#Requires -Version 7.0
<#
.SYNOPSIS
Ensures Foundry evaluator managed identities can call AOAI chat completions.

.DESCRIPTION
- Reads AZURE_AI_ACCOUNT_ID and AZURE_AI_PROJECT_ID from the selected azd environment.
- Resolves managed identity principal IDs for both resources.
- Grants "Cognitive Services OpenAI User" at the AI account scope.
- Idempotent: skips principals that already have the role assignment.
#>
[CmdletBinding()]
param(
    [string]$EnvName,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$RoleName = "Cognitive Services OpenAI User"
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

function Get-ResourcePrincipalId {
    param([string]$ResourceId)

    if ([string]::IsNullOrWhiteSpace($ResourceId)) {
        throw "Resource ID is required."
    }

    $principalObjectId = (& az resource show --ids $ResourceId --query "identity.principalId" -o tsv 2>$null).Trim()
    if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($principalObjectId)) {
        return $principalObjectId
    }

    foreach ($apiVersion in @("2025-06-01", "2025-04-01-preview")) {
        $url = "https://management.azure.com$ResourceId?api-version=$apiVersion"
        $principalObjectId = (& az rest --method get --url $url --query "identity.principalId" -o tsv 2>$null).Trim()
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($principalObjectId)) {
            return $principalObjectId
        }
    }

    throw "Could not resolve identity.principalId for resource '$ResourceId'."
}

function Ensure-RoleAssignment {
    param(
        [string]$PrincipalObjectId,
        [string]$Scope,
        [string]$Role
    )

    $existingAssignmentId = (& az role assignment list `
        --assignee-object-id $PrincipalObjectId `
        --scope $Scope `
        --role $Role `
        --query "[0].id" `
        -o tsv).Trim()

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query existing role assignments for principal '$PrincipalObjectId'."
    }

    if (-not [string]::IsNullOrWhiteSpace($existingAssignmentId)) {
        Write-Host "RBAC already present: principal=$PrincipalObjectId role='$Role' scope='$Scope'"
        return
    }

    $maxAttempts = 4
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        $createOutput = & az role assignment create `
            --assignee-object-id $PrincipalObjectId `
            --assignee-principal-type ServicePrincipal `
            --role $Role `
            --scope $Scope `
            -o none 2>&1

        if ($LASTEXITCODE -eq 0) {
            Write-Host "RBAC added: principal=$PrincipalObjectId role='$Role' scope='$Scope'"
            return
        }

        $createText = ($createOutput | Out-String)
        if ($createText -match "already exists") {
            Write-Host "RBAC already present: principal=$PrincipalObjectId role='$Role' scope='$Scope'"
            return
        }

        if ($attempt -lt $maxAttempts -and ($createText -match "PrincipalNotFound|does not exist in the directory|insufficient privileges")) {
            Start-Sleep -Seconds (5 * $attempt)
            continue
        }

        throw "Failed to create role assignment for principal '$PrincipalObjectId'. Details: $createText"
    }
}

Push-Location $RepoRoot
try {
    if (-not [string]::IsNullOrWhiteSpace($EnvName)) {
        & azd env select $EnvName | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to select azd environment '$EnvName'."
        }
    }

    $envMap = Get-AzdEnvMap -WorkingDirectory $RepoRoot
    $aiAccountResourceId = $envMap["AZURE_AI_ACCOUNT_ID"]
    $aiProjectResourceId = $envMap["AZURE_AI_PROJECT_ID"]

    if ([string]::IsNullOrWhiteSpace($aiAccountResourceId)) {
        throw "AZURE_AI_ACCOUNT_ID is missing from azd environment values."
    }
    if ([string]::IsNullOrWhiteSpace($aiProjectResourceId)) {
        throw "AZURE_AI_PROJECT_ID is missing from azd environment values."
    }

    $principalIds = @(
        Get-ResourcePrincipalId -ResourceId $aiAccountResourceId
        Get-ResourcePrincipalId -ResourceId $aiProjectResourceId
    ) | Sort-Object -Unique

    foreach ($principalObjectId in $principalIds) {
        Ensure-RoleAssignment -PrincipalObjectId $principalObjectId -Scope $aiAccountResourceId -Role $RoleName
    }

    Write-Host "Foundry evaluator RBAC check complete."
}
finally {
    Pop-Location
}
