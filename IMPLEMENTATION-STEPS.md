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
$repoFull = "anihitk07/build26-agents-redteam-eval-observability"

foreach ($envName in @("dev","test","prod")) {
  # Use -f formatting to avoid PowerShell parsing issues with colon-delimited strings.
  $subject = ('repo:{0}:environment:{1}' -f $repoFull, $envName)
  $params = @{
    name = "gh-$envName-env"
    issuer = $issuer
    subject = $subject
    audiences = $aud
    description = "GitHub OIDC for $envName environment"
  } | ConvertTo-Json -Depth 5

  $tmp = New-TemporaryFile
  Set-Content -Path $tmp -Value $params -Encoding UTF8
  az ad app federated-credential create --id $appObjectId --parameters "@$tmp"
  Remove-Item $tmp -Force
}
```

Verify subjects:

```powershell
az ad app federated-credential list --id $appObjectId --query "[].{name:name,subject:subject,issuer:issuer,aud:audiences}" -o table
```

Expected subject values:
- `repo:anihitk07/build26-agents-redteam-eval-observability:environment:dev`
- `repo:anihitk07/build26-agents-redteam-eval-observability:environment:test`
- `repo:anihitk07/build26-agents-redteam-eval-observability:environment:prod`

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

Behavior note:
- Foundry quality eval now auto-heals evaluator RBAC at runtime by parsing evaluator permission errors, granting `Cognitive Services OpenAI User` to the reported principal on the AI account scope, then retrying with propagation-aware backoff.

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

Optional:
- Disable auto-heal for debugging: `--no-auto-fix-permission-errors`
- Tune propagation retries: `--permission-retry-wait-seconds <seconds>` and `--permission-max-retries <count>`

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

## 11. Evaluator RBAC fix (workflow auto + manual fallback)

The staged workflow now runs this automatically after `azd provision` in each stage:

```powershell
.\scripts\ensure-foundry-eval-rbac.ps1 -EnvName "brk241-dev"
```

It grants `Cognitive Services OpenAI User` on the AI account scope for the managed identities of:
- `AZURE_AI_ACCOUNT_ID`
- `AZURE_AI_PROJECT_ID`

Use the manual command below only when running outside GitHub Actions or for break-glass troubleshooting.

```powershell
az role assignment create `
  --assignee-object-id <principal-object-id-from-error> `
  --assignee-principal-type User `
  --role "Cognitive Services OpenAI User" `
  --scope "/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<ai-account-name>"
```

## 12. GitHub Actions workflow reliability checks

The workflow now includes these reliability fixes:
- Explicit `azd auth login` using federated OIDC in each stage.
- Python dependency install for Foundry eval scripts.
- Toolbox endpoint validation after deploy.
- Automatic evaluator RBAC assignment right after infra provision (`ensure-foundry-eval-rbac.ps1`).
- Azure IDs exported at job scope (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`) to satisfy azd postdeploy hooks.

If troubleshooting, verify the workflow file has all of the above in `deploy-dev`, `deploy-test`, and `deploy-prod`.

## 13. Common failures and exact fixes

### A) OIDC federation error (AADSTS700213 - no matching federated identity)

Symptom:
- Subject mismatch like `repo:...:environment:dev`

Fix:
- Create federated credentials with full subject format:
  - `repo:anihitk07/build26-agents-redteam-eval-observability:environment:dev`
  - `repo:anihitk07/build26-agents-redteam-eval-observability:environment:test`
  - `repo:anihitk07/build26-agents-redteam-eval-observability:environment:prod`

PowerShell-safe subject construction (important):

```powershell
$repoFull = "anihitk07/build26-agents-redteam-eval-observability"
$subject = ('repo:{0}:environment:{1}' -f $repoFull, $envName)
```

### B) `azd provision` fails with insufficient permissions

Symptom:
- Missing `Microsoft.Resources/deployments/validate/action` at subscription scope.

Fix:
- Grant service principal these roles at subscription scope:
  - `Contributor`
  - `User Access Administrator` (required when templates create role assignments)

```powershell
$scope = "/subscriptions/<subscription-id>"
az role assignment create --assignee-object-id <sp-object-id> --assignee-principal-type ServicePrincipal --role "Contributor" --scope $scope
az role assignment create --assignee-object-id <sp-object-id> --assignee-principal-type ServicePrincipal --role "User Access Administrator" --scope $scope
```

### C) `setup-toolbox.ps1` fails on Linux with null path

Symptom:
- `Cannot bind argument to parameter 'Path' because it is null.`

Fix applied:
- Script uses `[System.IO.Path]::GetTempPath()` instead of `$env:TEMP`.
- Script uses cross-platform `Join-Path` patterns for `agent.yaml` files.

### D) Deploy fails with `FOUNDRY_PROJECT_ENDPOINT is required`

Symptom:
- During `azd deploy <service>`, environment variable missing.

Fix applied:
- `setup-toolbox.ps1` now sets:

```powershell
azd env set FOUNDRY_PROJECT_ENDPOINT "<project-endpoint>"
```

### E) Deploy fails in postdeploy with `AZURE_TENANT_ID is not set in the environment`

Symptom:
- Happens after service deploy in azd event hooks.

Fix applied:
- Workflow exports `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and `AZURE_SUBSCRIPTION_ID` at job env scope for all stages.

### F) Foundry quality eval shows `Partial` and 0 scored

Symptom:
- Evaluations run appears in UI but metrics show 0/0 with evaluator errors.

Fix:
- Workflow auto-remediation now runs post-provision and should prevent this in normal staged runs.
- If this still happens, run manual RBAC assignment:
- Grant the principal from the evaluator error:
  - Role: `Cognitive Services OpenAI User`
  - Scope: AI account resource id

```powershell
az role assignment create `
  --assignee-object-id <principal-object-id-from-eval-error> `
  --assignee-principal-type User `
  --role "Cognitive Services OpenAI User" `
  --scope "/subscriptions/<subscription-id>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<ai-account-name>"
```
