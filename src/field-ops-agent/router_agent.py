"""Router Agent — Concept #3: Voice routing pattern (front desk).

═══════════════════════════════════════════════════════════════════════════════
DEMO POINT — "Voice latency: how to fill the dead space"

Talk track (the gap Demo-script.md step 10 explicitly calls out):
    "This will likely take a few seconds to pull up, so we need to capture
     the dead space ideally"

Without this pattern: user asks → silence → 8-15 s while tools run → answer.
With this pattern: user asks → 1 s "Looking that up." → optional 'still on it'
                   filler → final answer. Same total time; way better feel.

How it works (this file):
  - The router is a SINGLE LLM call with ``tool_choice="required"``.
    The model is forced to pick exactly one of five meta-tools — there is no
    free-text path. That eliminates the "I'll look into it…" hallucinations.
  - Five meta-tools (defined in ``ROUTER_TOOLS``):
        respond_directly      pure chat / acknowledgments / delivering results
        start_task            delegate real work to the worker agent
        check_task_status     report progress of a running task
        cancel_task           stop a running task
        get_task_result       relay a finished result
  - ``execute_router_tool`` is the dispatcher used by main.py's request handler.
  - The actual heavy lifting happens in ``worker_agent.py`` (the MAF agent).

Why this is a "Microsoft 365 Copilot routing" pattern, not a hack:
  - Router = front desk. Always responsive, never blocks.
  - Worker = back office. Does the work, takes as long as it takes.
  - Tasks survive client disconnect. If the voice channel drops mid-wait,
    the worker keeps running and the answer is delivered on the next turn.
═══════════════════════════════════════════════════════════════════════════════

Tools:
  respond_directly — for chat, greetings, delivering completed results
  start_task       — delegate field-ops work to worker (includes ack_message)
  check_task_status / cancel_task / get_task_result — meta-operations
"""

import json
import logging
import os
import pathlib

from task_store import Task, TaskStore, TaskStatus

logger = logging.getLogger(__name__)

# ── Router Tools ──────────────────────────────────────────────────────────────
# tool_choice: "required" means the model MUST call exactly one of these.

ROUTER_TOOLS = [
    {
        "type": "function",
        "name": "respond_directly",
        "description": (
            "Respond to the user directly without delegating. "
            "Use for: greetings, chat, clarifications, delivering completed results, "
            "or any response that does NOT require background tool execution."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The spoken response to the user. Keep concise for voice.",
                },
            },
            "required": ["message"],
        },
    },
    {
        "type": "function",
        "name": "start_task",
        "description": (
            "Delegate a field-ops request to the background worker agent. "
            "Use for: site specs lookup, Work IQ search, repair procedures, "
            "document analysis, and general technical lookups that require tools "
            "(including WebIQ queries)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request_summary": {
                    "type": "string",
                    "description": "Clear summary of what the user needs done",
                },
                "ack_message": {
                    "type": "string",
                    "description": "Very short spoken acknowledgment, max 5-6 words (e.g. 'Looking that up.' or 'On it.')",
                },
            },
            "required": ["request_summary", "ack_message"],
        },
    },
    {
        "type": "function",
        "name": "check_task_status",
        "description": (
            "Check progress of a running or completed task. "
            "Use when the user asks about status or progress."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID, or 'latest' for most recent task",
                },
                "ack_message": {
                    "type": "string",
                    "description": "Brief spoken response about the status",
                },
            },
            "required": ["task_id", "ack_message"],
        },
    },
    {
        "type": "function",
        "name": "cancel_task",
        "description": "Cancel a running or queued task.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to cancel, or 'latest'",
                },
                "ack_message": {
                    "type": "string",
                    "description": "Brief spoken confirmation to the user",
                },
            },
            "required": ["task_id", "ack_message"],
        },
    },
    {
        "type": "function",
        "name": "get_task_result",
        "description": (
            "Retrieve the final result of a completed task. "
            "Use when a task has finished and you need to relay the answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID, or 'latest'",
                },
            },
            "required": ["task_id"],
        },
    },
]

# Force the model to always call a tool (no free-text path)
ROUTER_TOOL_CHOICE = "required"

def _load_router_system_prompt() -> str:
    configured = os.getenv("ROUTER_SYSTEM_PROMPT_FILE", "").strip()
    default_path = (
        pathlib.Path(__file__).resolve().parent
        / ".agent_configs"
        / "router-instructions.md"
    )
    prompt_path = pathlib.Path(configured) if configured else default_path
    if not prompt_path.is_absolute():
        prompt_path = (pathlib.Path(__file__).resolve().parent / prompt_path).resolve()
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Router prompt file not found: {prompt_path}. "
            "Set ROUTER_SYSTEM_PROMPT_FILE to a valid path."
        )
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"Router prompt file is empty: {prompt_path}")
    return prompt


ROUTER_SYSTEM_PROMPT = _load_router_system_prompt()


# ── Router Tool Execution ─────────────────────────────────────────────────────

def execute_router_tool(name: str, arguments: dict, store: TaskStore) -> tuple[str, Task | None]:
    """Execute a router meta-tool. Returns (result_json, new_task_or_none).

    All operations here are instant (no I/O, no blocking).
    """
    if name == "respond_directly":
        return json.dumps({"delivered": True}), None

    if name == "start_task":
        # Guard: don't create a duplicate task if one is already active
        active = store.active_tasks()
        if active:
            existing = active[-1]  # most recent active task
            return json.dumps({
                "task_id": existing.task_id,
                "status": existing.status.value,
                "note": "Task already running — not creating duplicate",
                "query": existing.query,
            }), None  # Return None → no new worker spawned
        task = store.create_task(arguments["request_summary"])
        return json.dumps({"task_id": task.task_id, "status": "queued"}), task

    if name == "check_task_status":
        task = store.get(arguments["task_id"])
        if not task:
            return json.dumps({"error": "No task found"}), None
        return json.dumps({
            "task_id": task.task_id,
            "status": task.status.value,
            "current_tool": task.current_tool,
            "rounds_completed": task.rounds_completed,
            "query": task.query,
        }), None

    if name == "cancel_task":
        task = store.get(arguments["task_id"])
        success = store.cancel(arguments["task_id"])
        # MAF's agent.run() is a single black-box call — setting cancel_event
        # alone won't interrupt it. Also cancel the asyncio handle so the run
        # raises CancelledError on its next await point.
        if task and task.asyncio_task and not task.asyncio_task.done():
            task.asyncio_task.cancel()
        return json.dumps({
            "cancelled": success,
            "task_id": task.task_id if task else None,
        }), None

    if name == "get_task_result":
        task = store.get(arguments["task_id"])
        if not task:
            return json.dumps({"error": "No task found"}), None
        if task.status in (TaskStatus.COMPLETED, TaskStatus.DELIVERED):
            return json.dumps({"task_id": task.task_id, "result": task.result}), None
        return json.dumps({
            "error": "Task not completed yet",
            "task_id": task.task_id,
            "status": task.status.value,
        }), None

    return json.dumps({"error": f"Unknown router tool: {name}"}), None


def build_task_context(store: TaskStore) -> str | None:
    """Build a context string about active tasks for the router prompt."""
    lines = []
    for t in store.active_tasks():
        lines.append(
            f"RUNNING: task {t.task_id} — '{t.query}' "
            f"(round {t.rounds_completed + 1}, tool: {t.current_tool or 'thinking'})"
        )
    return "\n".join(lines) if lines else None


def build_delivered_context(store: TaskStore) -> str | None:
    """Build a context string of recently delivered task results.

    This helps the router recall results that were already shown to the user,
    so it can respond to follow-ups like 'what was the result?' without re-running.
    """
    lines = []
    for t in store.delivered_tasks():
        result_preview = (t.result or "")[:300]
        lines.append(f"DELIVERED: task {t.task_id} — '{t.query}' → {result_preview}")
    return "\n".join(lines) if lines else None
