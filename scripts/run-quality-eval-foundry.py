"""Run Foundry quality evals so normal evals appear in the Foundry Evaluations UI.

This script creates one quality eval run per hosted agent using recent traces
from App Insights (azure_ai_traces_preview), then waits for completion and
writes artifacts under artifacts/eval.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

try:
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential
except ImportError as exc:
    raise SystemExit(
        "Missing dependency for Foundry quality evals. "
        "Install with: pip install azure-ai-projects azure-identity\n"
        f"Import error: {exc}"
    )

TERMINAL_STATUSES = {"completed", "failed", "canceled", "cancelled"}
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 20 * 60
NO_TRACE_ERROR_SNIPPET = "No trace data found"
DEFAULT_EVALUATORS = [
    "intent_resolution",
    "task_adherence",
    "coherence",
    "fluency",
    "relevance",
]


@dataclass
class AgentConfig:
    name: str
    protocol: str
    seed_prompt: str


def _to_json(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_json(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_json(v) for k, v in obj.items()}
    for method in ("model_dump", "to_dict", "as_dict", "dict"):
        if hasattr(obj, method):
            try:
                return _to_json(getattr(obj, method)())
            except Exception:
                pass
    return str(obj)


def _parse_azd_env_get_values(raw: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"')
    return env


def _load_azd_env(env_name: str | None) -> dict[str, str]:
    cmd = ["azd", "env", "get-values"]
    if env_name:
        cmd += ["-e", env_name]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to read azd env values: {proc.stderr.strip()}")
    return _parse_azd_env_get_values(proc.stdout)


def _get_error_message(eval_run: Any) -> str:
    err = getattr(eval_run, "error", None)
    if not err:
        return ""
    if isinstance(err, dict):
        return str(err.get("message", ""))
    return str(getattr(err, "message", err))


def _create_eval(openai_client: Any, model_deployment: str, agent_name: str) -> str:
    data_mapping = {"query": "{{item.query}}", "response": "{{item.response}}"}
    testing_criteria: list[dict[str, Any]] = []
    for evaluator_name in DEFAULT_EVALUATORS:
        testing_criteria.append(
            {
                "type": "azure_ai_evaluator",
                "name": evaluator_name,
                "evaluator_name": f"builtin.{evaluator_name}",
                "data_mapping": data_mapping,
                "initialization_parameters": {"deployment_name": model_deployment},
            }
        )

    eval_object = openai_client.evals.create(
        name=f"{agent_name}-quality-eval-{int(time.time())}",
        data_source_config={"type": "azure_ai_source", "scenario": "responses"},
        testing_criteria=testing_criteria,
    )
    return eval_object.id


def _run_eval(
    openai_client: Any,
    eval_id: str,
    agent_name: str,
    lookback_hours: int,
    max_traces: int,
    run_name_prefix: str,
) -> Any:
    eval_run = openai_client.evals.runs.create(
        eval_id=eval_id,
        name=f"{run_name_prefix}-{int(time.time())}",
        data_source={
            "type": "azure_ai_traces_preview",
            "agent_name": agent_name,
            "lookback_hours": lookback_hours,
            "max_traces": max_traces,
            "ingestion_delay_seconds": 0,
        },
    )

    deadline = time.time() + POLL_TIMEOUT_SECONDS
    last_status = str(getattr(eval_run, "status", ""))
    while time.time() < deadline:
        status = str(getattr(eval_run, "status", "")).lower()
        if status in TERMINAL_STATUSES:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
        eval_run = openai_client.evals.runs.retrieve(run_id=eval_run.id, eval_id=eval_id)
        current_status = str(getattr(eval_run, "status", ""))
        if current_status != last_status:
            print(f"[{agent_name}] status: {current_status}")
            last_status = current_status
    else:
        raise TimeoutError(f"Timed out waiting for quality eval run: {eval_run.id}")

    return eval_run


def _seed_agent_trace(agent: AgentConfig, env_name: str | None) -> None:
    cmd = ["azd"]
    if env_name:
        cmd += ["-e", env_name]
    cmd += ["ai", "agent", "invoke", agent.name, "--new-session", "--new-conversation"]
    if agent.protocol:
        cmd += ["--protocol", agent.protocol]
    cmd += [agent.seed_prompt]

    print(f"[{agent.name}] seeding trace with an invoke call...")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr if stderr else stdout
        raise RuntimeError(f"Trace seed invoke failed for {agent.name}: {detail}")


def _collect_output_items(openai_client: Any, eval_id: str, run_id: str) -> list[Any]:
    return list(openai_client.evals.runs.output_items.list(run_id=run_id, eval_id=eval_id))


def _extract_first_evaluator_error(output_items: list[Any]) -> str:
    for item in output_items:
        results = item.get("results") if isinstance(item, dict) else None
        if not isinstance(results, list):
            continue
        for result in results:
            sample = result.get("sample") if isinstance(result, dict) else None
            if not isinstance(sample, dict):
                continue
            error = sample.get("error")
            if not isinstance(error, dict):
                continue
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return ""


def _build_openai_user_role_fix(
    permission_error_message: str,
    subscription_id: str,
    resource_group: str,
    account_name: str,
) -> str:
    if not permission_error_message:
        return ""
    if "lacks the required data action" not in permission_error_message:
        return ""

    match = re.search(r"principal `([^`]+)`", permission_error_message)
    principal_id = match.group(1) if match else "<principal-object-id>"
    principal_type_match = re.search(r"has type '([^']+)'", permission_error_message, flags=re.IGNORECASE)
    principal_type = principal_type_match.group(1) if principal_type_match else ""
    allowed_types = {"User", "ServicePrincipal", "Group", "ForeignGroup", "Device", "MSI"}
    assignee_type_arg = (
        f"--assignee-principal-type {principal_type}"
        if principal_type in allowed_types
        else ""
    )
    if not (subscription_id and resource_group and account_name):
        return (
            "Grant 'Cognitive Services OpenAI User' to the principal shown in the error "
            f"({principal_id}) on the Azure AI account scope, then rerun."
        )

    scope = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account_name}"
    )
    return (
        "Evaluator permission fix required. Run:\n"
        f"az role assignment create --assignee-object-id {principal_id} "
        f"{assignee_type_arg} "
        "--role \"Cognitive Services OpenAI User\" "
        f"--scope \"{scope}\""
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Foundry quality evals for hosted agents.")
    parser.add_argument("--env-name", default=None, help="azd environment name")
    parser.add_argument(
        "--output-path",
        default=None,
        help="Summary output path (default: artifacts/eval/foundry-quality-eval-summary.json)",
    )
    parser.add_argument("--lookback-hours", type=int, default=2, help="Trace lookback window in hours")
    parser.add_argument("--max-traces", type=int, default=20, help="Max traces sampled per run")
    parser.add_argument(
        "--retry-wait-seconds",
        type=int,
        default=45,
        help="Wait before retry after seeding traces",
    )
    args = parser.parse_args()

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    artifacts_dir = repo_root / "artifacts" / "eval"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    output_path = pathlib.Path(args.output_path) if args.output_path else artifacts_dir / "foundry-quality-eval-summary.json"
    if not output_path.is_absolute():
        output_path = (repo_root / output_path).resolve()

    env_map = _load_azd_env(args.env_name)
    endpoint = env_map.get("AZURE_AI_PROJECT_ENDPOINT") or env_map.get("AZURE_AIPROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("AZURE_AI_PROJECT_ENDPOINT missing from azd environment.")

    model_deployment = env_map.get("AZURE_AI_MODEL_DEPLOYMENT_NAME") or "gpt-5.4"
    resolved_env_name = env_map.get("AZURE_ENV_NAME") or args.env_name
    subscription_id = env_map.get("AZURE_SUBSCRIPTION_ID", "")
    resource_group = env_map.get("AZURE_RESOURCE_GROUP", "")
    account_name = env_map.get("AZURE_AI_ACCOUNT_NAME", "")

    agents = [
        AgentConfig(
            name="field-ops-agent",
            protocol="",
            seed_prompt="What is the IEEE 802.3bs optical power budget?",
        ),
        AgentConfig(
            name="fibey-coordinator",
            protocol="responses",
            seed_prompt="Check network telemetry for Quincy North and summarize active alerts.",
        ),
    ]

    print(f"endpoint: {endpoint}")
    print(f"model:    {model_deployment}")
    print(f"env:      {resolved_env_name}")
    print(f"agents:   {', '.join([a.name for a in agents])}")

    project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    openai_client = project.get_openai_client()

    run_summaries: list[dict[str, Any]] = []
    failures: list[str] = []

    for agent in agents:
        print(f"\n[{agent.name}] creating quality eval definition...")
        eval_id = _create_eval(openai_client=openai_client, model_deployment=model_deployment, agent_name=agent.name)
        print(f"[{agent.name}] eval_id: {eval_id}")

        eval_run = _run_eval(
            openai_client=openai_client,
            eval_id=eval_id,
            agent_name=agent.name,
            lookback_hours=args.lookback_hours,
            max_traces=args.max_traces,
            run_name_prefix=f"{agent.name}-quality-run",
        )
        error_message = _get_error_message(eval_run)
        retried_after_seed = False

        if str(getattr(eval_run, "status", "")).lower() == "failed" and NO_TRACE_ERROR_SNIPPET.lower() in error_message.lower():
            _seed_agent_trace(agent=agent, env_name=resolved_env_name)
            print(f"[{agent.name}] waiting {args.retry_wait_seconds}s for traces to ingest before retry...")
            time.sleep(args.retry_wait_seconds)
            eval_run = _run_eval(
                openai_client=openai_client,
                eval_id=eval_id,
                agent_name=agent.name,
                lookback_hours=args.lookback_hours,
                max_traces=args.max_traces,
                run_name_prefix=f"{agent.name}-quality-retry",
            )
            error_message = _get_error_message(eval_run)
            retried_after_seed = True

        status = str(getattr(eval_run, "status", ""))
        print(f"[{agent.name}] run_id: {eval_run.id} status={status}")
        if getattr(eval_run, "report_url", None):
            print(f"[{agent.name}] report: {eval_run.report_url}")

        output_items_path = artifacts_dir / f"foundry-quality-{agent.name}-output-items.json"
        output_items: list[Any] = []
        try:
            output_items = _collect_output_items(openai_client=openai_client, eval_id=eval_id, run_id=eval_run.id)
            output_items_path.write_text(json.dumps(_to_json(output_items), indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[{agent.name}] output items warning: {type(exc).__name__}: {exc}")

        result_counts = _to_json(getattr(eval_run, "result_counts", None))
        errored_count = int((result_counts or {}).get("errored", 0)) if isinstance(result_counts, dict) else 0
        first_eval_error = _extract_first_evaluator_error(output_items)
        role_fix = _build_openai_user_role_fix(
            permission_error_message=first_eval_error,
            subscription_id=subscription_id,
            resource_group=resource_group,
            account_name=account_name,
        )

        run_summaries.append(
            {
                "agent": agent.name,
                "eval_id": eval_id,
                "run_id": eval_run.id,
                "status": status,
                "result_counts": result_counts,
                "per_testing_criteria_results": _to_json(
                    getattr(eval_run, "per_testing_criteria_results", None)
                ),
                "report_url": getattr(eval_run, "report_url", None),
                "error": _to_json(getattr(eval_run, "error", None)),
                "retried_after_seed": retried_after_seed,
                "output_items_path": str(output_items_path),
                "first_evaluator_error": first_eval_error,
                "suggested_role_fix": role_fix,
            }
        )

        if status.lower() != "completed":
            failures.append(f"{agent.name}={status or 'unknown'}")
        elif errored_count > 0:
            if role_fix:
                failures.append(f"{agent.name}=evaluator-permission-error")
            else:
                failures.append(f"{agent.name}=evaluator-errors:{errored_count}")

    summary = {
        "run_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "env_name": resolved_env_name,
        "endpoint": endpoint,
        "model_deployment": model_deployment,
        "lookback_hours": args.lookback_hours,
        "max_traces": args.max_traces,
        "runs": run_summaries,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nsummary: {output_path}")

    if failures:
        fix_hints: list[str] = []
        for run in run_summaries:
            suggested = run.get("suggested_role_fix")
            if isinstance(suggested, str) and suggested.strip():
                fix_hints.append(suggested.strip())
        suffix = ""
        if fix_hints:
            suffix = "\n\n" + "\n\n".join(sorted(set(fix_hints)))
        raise RuntimeError(
            "One or more Foundry quality eval runs did not complete with usable scores: "
            + ", ".join(failures)
            + suffix
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
