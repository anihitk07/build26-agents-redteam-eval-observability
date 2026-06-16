# Field Operations Agent

Voice-enabled field technician assistant for data center operations. It deploys
to Microsoft Foundry as a hosted agent, can be voice-enabled, and answers
technician questions using local function tools plus optional Foundry tools.

## File map (open in this order)

| Step | File | What it shows | Concept |
| --- | --- | --- | --- |
| 1 | `agent.yaml` | Hosted-agent config Foundry deploys | Deploy to Foundry |
| 2 | `toolbox.py` | One function returns an MCP tool that drops into the agent's tool list | **Toolbox** |
| 3 | `worker_agent.py` | `@tool` functions + `FoundryChatClient.as_agent(...)` = the entire agent loop | **Agent Framework: tools + agent loop** |
| 4 | `router_agent.py` | `tool_choice="required"` over five meta-tools — front-desk pattern that kills voice dead-space | **Voice routing pattern** |
| 5 | `task_store.py` | Lifecycle state powering the voice pattern | Supporting state |
| 6 | `main.py` | Thin glue: server, request handler, streaming | Wiring |
| 7 | `route_worker_agent.md` | Talking points + diagrams for the voice pattern | Reference |

Each Python file starts with a `DEMO POINT` banner so the key teaching moments
are easy to find as you read.

## Architecture (one-liner per concept)

- **Toolbox** — `toolbox.py`'s `build_toolbox_tool()` returns an
  `MCPStreamableHTTPTool`. The agent just appends it to `tools=[...]`.
  Foundry-managed tools (Work IQ, Document Understanding, WebSearch…) look
  the same as local tools to the agent.
- **Agent Framework + loop** — `worker_agent.py` defines four `@tool`
  functions and wires them in with `FoundryChatClient(...).as_agent(name,
  instructions, tools=[...])`. That single `as_agent` call is the whole
  multi-round model-call-then-tool-call loop.
- **Voice routing** — `router_agent.py` runs one chat-completions call per
  user turn with `tool_choice="required"` over five meta-tools
  (`respond_directly`, `start_task`, `check_task_status`, `cancel_task`,
  `get_task_result`). `start_task` spawns the worker in the background and
  immediately speaks a short ack like *"Looking that up."* — the user hears
  speech in ~1 s instead of waiting for the full tool loop in silence.

```
voice in → router (fast ack) → worker (MAF agent loop) → final answer streams when ready
```

## Tools

| Tool | Source | Description |
| --- | --- | --- |
| `search_site_specs` | `worker_agent.py` (`@tool`) | Site infrastructure lookup |
| `search_work_iq` | `worker_agent.py` (`@tool`) | Active work orders / maintenance |
| `get_repair_procedure` | `worker_agent.py` (`@tool`) | Step-by-step repair guides |
| `analyze_document` | `worker_agent.py` (`@tool`) | Document Understanding placeholder |
| Anything in Toolbox | `toolbox.py` (MCP) | Work IQ, WebSearch, etc. — appears in the same tool list when `TOOLBOX_ENDPOINT` is set |

All four local tools return rich sample data so the agent runs deterministically
out of the box. Replace them with real backends to productionize.

## Example voice queries

```
"Hey, can you pull up the fiber termination spec for the Quincy North site
 and tell me which panel I should be looking at for the B-side connection?"

"What repair procedure should I follow for a QSFP-DD transceiver replacement?"

"Any P1 work orders assigned to me?"
```

## Run

```bash
# Locally
azd ai agent invoke --local "What is the fiber spec for Quincy North?"

# Deploy to Foundry
azd deploy field-ops-agent
```

For the VS Code Agent Inspector/debug workflow, install local tooling separately:

```bash
python -m pip install --pre agent-dev-cli debugpy
```

## Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | yes | Auto-injected in hosted containers |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | yes | Model deployment name |
| `TOOLBOX_ENDPOINT` | no | Toolbox MCP URL — when set, Toolbox tools appear in the agent |
| `TOOLBOX_FEATURES` | no | Toolbox feature-flag header (default `Toolboxes=V1Preview`) |

## Walkthrough talking points (suggested order)

1. **`agent.yaml`** — "One YAML file. Foundry hosts this."
2. **`toolbox.py`** — "Toolbox handles the Work IQ + Document Understanding +
   memory connections I set up in the portal. From the agent's perspective
   it's just one more entry in the tools list."
3. **`worker_agent.py`** — "Microsoft Agent Framework. Look at the `@tool`
   decorator — that's the entire tool definition. Look at `as_agent()` —
   that's the entire agent loop."
4. **`router_agent.py`** — "Voice has a dead-space problem. Here's the
   pattern: a single fast LLM call with `tool_choice='required'` picks
   between five meta-tools. The user hears 'Looking that up' in a second
   while the back-office worker does the actual research."
5. **Live voice query.** Ack arrives fast, filler covers the wait, final
   answer streams in.
