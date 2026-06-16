# Fibey Coordinator

Network operations coordinator agent — a long-running AI teammate that monitors, investigates, dispatches, and escalates.

## What It Does

Fibey is not a chatbot. Fibey is an employee:
- **Proactively monitors** network telemetry via routines
- **Investigates anomalies** and builds investigation dossiers
- **Dispatches work orders** to field contractors
- **Waits for responses** (scales to zero while waiting — $0 compute)
- **Escalates** when SLAs are at risk
- **Persists context** to $HOME filesystem across sessions

## Architecture

Uses the Microsoft Agent Framework (MAF) pattern:
- FoundryChatClient + FunctionTool for tool orchestration
- ResponseEventStream for Responses protocol streaming
- Real file persistence via $HOME/investigations/
- azure-ai-agentserver-responses SDK for hosting

## Tools

| Tool | Description |
|------|-------------|
| check_network_telemetry | Monitor sites for anomalies and active alerts |
| dispatch_work_order | Send work orders to contractors via email |
| save_investigation | Save investigation context to persistent $HOME |
| get_active_incidents | List all active incidents across the portfolio |
| escalate_incident | Escalate to ops manager on SLA breach |

Currently using mock data with rich telemetry, alerts, and incident portfolios.

## Model

- Deployment: gpt-5.4 (configurable via AZURE_AI_MODEL_DEPLOYMENT_NAME)

## Example Prompts

- Check network telemetry for Quincy North - any active alerts?
- Dispatch a work order for the CRC error on Rack B-14
- Save an investigation report for the fiber degradation incident
- What active incidents do we have across all sites?
- Escalate the Quincy North optical power issue - SLA at risk

## Optional Toolbox MCP integration

Set `TOOLBOX_ENDPOINT` to your Foundry toolbox MCP endpoint to expose toolbox-managed tools in Fibey:

`https://<account>.services.ai.azure.com/api/projects/<project>/toolboxes/<toolbox>/mcp?api-version=v1`

## Running

azd ai agent run --service fibey-coordinator
azd deploy fibey-coordinator
azd ai agent invoke fibey-coordinator --protocol responses "Check network telemetry for Quincy North - any active alerts?"
