# Implementation Steps

This guide lists the exact command syntax for all operational scripts in this repository.

## 1. Prerequisites

```powershell
az login
azd auth login
azd extension install azure.ai.agents --force
azd extension install azure.ai.toolboxes --force
pip install azure-ai-projects azure-identity
```

## 2. Select environment

```powershell
azd env select "<your-env>"
```

## 3. Setup toolbox (WebIQ) and deploy agents

```powershell
$env:WEBIQ_API_KEY = "<your-webiq-api-key>"
.\scripts\setup-toolbox.ps1 -EnvName "<your-env>" -ToolboxName "shared-agent-tools" -DeployAgents
azd ai toolbox show shared-agent-tools --output json
```

## 4. Run standard eval gate (local + Azure portal + Foundry quality eval UI)

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

## 5. Run Foundry quality eval only

```powershell
python .\scripts\run-quality-eval-foundry.py --env-name "<your-env>" --lookback-hours 2 --max-traces 20
```

## 6. Export observability snapshot

```powershell
.\scripts\show-maf-observability.ps1 -EnvName "<your-env>" -Timespan PT2H
```

## 7. Run red-team gate (local + Foundry red-team + Azure portal)

```powershell
.\scripts\run-red-team.ps1 `
  -EnvName "<your-env>" `
  -FieldOpsThreshold 0.75 `
  -FibeyThreshold 0.75 `
  -MaxCasesPerAgent 8
```

## 8. Run Foundry red-team only

```powershell
python .\scripts\run-red-team-foundry.py --env-name "<your-env>"
```

## 9. Trigger staged rollout workflow (dev -> test -> prod)

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

## 10. Evaluator RBAC fix (if Foundry eval shows partial/0 scored)

```powershell
az role assignment create `
  --assignee-object-id <principal-object-id-from-error> `
  --assignee-principal-type User `
  --role "Cognitive Services OpenAI User" `
  --scope "/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<ai-account-name>"
```
