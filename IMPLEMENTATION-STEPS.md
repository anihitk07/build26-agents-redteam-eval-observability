# Implementation Steps

This guide lists the exact command syntax for all operational scripts in this repository.

## 1. Prerequisites

```powershell
az login
gh auth login
azd auth login
azd extension install azure.ai.agents --force
azd extension install azure.ai.toolboxes --force
pip install azure-ai-projects azure-identity
```

## 2. Configure Entra app + GitHub OIDC federation (for workflow auth)

Set variables:

```powershell
$subscriptionId = "<your-subscription-id>"
$tenantId       = "<your-tenant-id>"
$repo           = "anihitk07/build26-agents-redteam-eval-observability"
$appName        = "gh-build26-agents-oidc"
```

Create app registration + service principal:

```powershell
$app = az ad app create --display-name $appName --query "{appId:appId,id:id}" -o json | ConvertFrom-Json
$clientId = $app.appId
$appObjectId = $app.id
$spObjectId = az ad sp create --id $clientId --query id -o tsv
```

Create federated credentials for GitHub environments `dev`, `test`, `prod`:

```powershell
$issuer = "https://token.actions.githubusercontent.com"
$aud = @("api://AzureADTokenExchange")

foreach ($envName in @("dev","test","prod")) {
  $subject = "repo:$repo:environment:$envName"
  $params = @{
    name = "gh-$envName"
    issuer = $issuer
    subject = $subject
    audiences = $aud
    description = "GitHub Actions OIDC for $envName"
  } | ConvertTo-Json -Depth 5

  $tmp = New-TemporaryFile
  Set-Content -Path $tmp -Value $params -Encoding UTF8
  az ad app federated-credential create --id $appObjectId --parameters "@$tmp"
  Remove-Item $tmp -Force
}
```

Grant Azure RBAC to the service principal at your deployment scope (resource group or subscription):

```powershell
$scope = "/subscriptions/$subscriptionId/resourceGroups/<your-resource-group>"
az role assignment create --assignee-object-id $spObjectId --assignee-principal-type ServicePrincipal --role "Contributor" --scope $scope
```

If your deployment also creates role assignments, add this too:

```powershell
az role assignment create --assignee-object-id $spObjectId --assignee-principal-type ServicePrincipal --role "User Access Administrator" --scope $scope
```

Set GitHub secrets used by workflow:

```powershell
gh secret set AZURE_CLIENT_ID --repo $repo --body $clientId
gh secret set AZURE_TENANT_ID --repo $repo --body $tenantId
gh secret set AZURE_SUBSCRIPTION_ID --repo $repo --body $subscriptionId
gh secret set WEBIQ_API_KEY --repo $repo --body "<your-webiq-api-key>"
```

## 3. Select environment

```powershell
azd env select "<your-env>"
```

## 4. Setup toolbox (WebIQ) and deploy agents

```powershell
$env:WEBIQ_API_KEY = "<your-webiq-api-key>"
.\scripts\setup-toolbox.ps1 -EnvName "<your-env>" -ToolboxName "shared-agent-tools" -DeployAgents
azd ai toolbox show shared-agent-tools --output json
```

## 5. Run standard eval gate (local + Azure portal + Foundry quality eval UI)

```powershell
.\scripts\run-agent-evals.ps1 `
  -EnvName "<your-env>" `
  -FieldOpsThreshold 0.70 `
  -FibeyThreshold 0.70 `
  -MaxCasesPerAgent 5
```

Optional parameters:

```powershell
.\scripts\run-agent-evals.ps1 `
  -EnvName "<your-env>" `
  -PublishToFoundryPortal $true `
  -PublishToAzurePortal $true `
  -FailOnFoundryPortalPublishError $true `
  -FailOnPortalPublishError $true
```

## 6. Run Foundry quality eval only

```powershell
python .\scripts\run-quality-eval-foundry.py --env-name "<your-env>" --lookback-hours 2 --max-traces 20
```

## 7. Export observability snapshot

```powershell
.\scripts\show-maf-observability.ps1 -EnvName "<your-env>" -Timespan PT2H
```

## 8. Run red-team gate (local + Foundry red-team + Azure portal)

```powershell
.\scripts\run-red-team.ps1 `
  -EnvName "<your-env>" `
  -FieldOpsThreshold 0.75 `
  -FibeyThreshold 0.75 `
  -MaxCasesPerAgent 8
```

## 9. Run Foundry red-team only

```powershell
python .\scripts\run-red-team-foundry.py --env-name "<your-env>"
```

## 10. Trigger staged rollout workflow (dev -> test -> prod)

Required GitHub secrets:
- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`
- `WEBIQ_API_KEY`

```powershell
gh workflow run "Agents Stage Rollout (dev-test-prod)" `
  -f location=eastus2 `
  -f toolbox_name=shared-agent-tools `
  -f field_ops_threshold=0.70 `
  -f fibey_threshold=0.70 `
  -f max_eval_cases=5 `
  -f run_observability=true `
  -f run_redteam=true `
  -f redteam_threshold=0.75
```

## 11. Evaluator RBAC fix (if Foundry eval shows partial/0 scored)

```powershell
az role assignment create `
  --assignee-object-id <principal-object-id-from-error> `
  --assignee-principal-type User `
  --role "Cognitive Services OpenAI User" `
  --scope "/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<ai-account-name>"
```
