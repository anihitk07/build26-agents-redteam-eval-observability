"""Run Foundry red-team evals so results appear in the Foundry portal.

This script follows the cloud red-team pattern from the ai-observability-starter-kit
reference scripts:
- create temporary prompt agents
- run red-team eval runs (Flip + Base64 strategies)
- wait for completion
- write artifacts

Unlike local heuristic red-team checks, these eval runs are visible in the Foundry
portal Evaluations experience.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentTaxonomyInput,
    AzureAIAgentTarget,
    AzureAIDataSourceConfig,
    EvaluationTaxonomy,
    RedTeamEvalRunDataSource,
    RiskCategory,
    TestingCriterionAzureAIEvaluator,
)
from azure.ai.projects.models._models import PromptAgentDefinition
from azure.identity import DefaultAzureCredential

TERMINAL_STATUSES = {"completed", "failed", "canceled", "cancelled"}
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 30 * 60


@dataclass
class AgentConfig:
    name: str
    prompt_path: pathlib.Path


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
        key = key.strip()
        value = value.strip().strip('"')
        env[key] = value
    return env


def _load_azd_env(env_name: str | None) -> dict[str, str]:
    cmd = ["azd", "env", "get-values"]
    if env_name:
        cmd += ["-e", env_name]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to read azd env values: {proc.stderr.strip()}")
    return _parse_azd_env_get_values(proc.stdout)


def _create_redteam_run_for_agent(
    project: AIProjectClient,
    openai_client: Any,
    agent_cfg: AgentConfig,
    model_deployment: str,
    artifacts_dir: pathlib.Path,
) -> dict[str, Any]:
    instructions = agent_cfg.prompt_path.read_text(encoding="utf-8").strip()
    if not instructions:
        raise ValueError(f"Prompt file is empty: {agent_cfg.prompt_path}")

    suffix = int(time.time())
    temp_agent_name = f"redteam-{agent_cfg.name}-{suffix}".replace("_", "-")
    eval_name = f"{agent_cfg.name}-redteam-eval-{suffix}"
    run_name = f"{agent_cfg.name}-redteam-run-{suffix}"

    print(f"\n[{agent_cfg.name}] creating temporary prompt agent: {temp_agent_name}")
    agent_version = project.agents.create_version(
        agent_name=temp_agent_name,
        definition=PromptAgentDefinition(
            model=model_deployment,
            instructions=instructions,
            tools=[],
        ),
    )

    target = AzureAIAgentTarget(
        name=temp_agent_name,
        version=agent_version.version,
        tool_descriptions=[],
    )

    red_team_eval = openai_client.evals.create(
        name=eval_name,
        data_source_config=AzureAIDataSourceConfig(type="azure_ai_source", scenario="red_team"),
        testing_criteria=[
            TestingCriterionAzureAIEvaluator(
                type="azure_ai_evaluator",
                name="Prohibited Actions",
                evaluator_name="builtin.prohibited_actions",
                evaluator_version="1",
            ),
            TestingCriterionAzureAIEvaluator(
                type="azure_ai_evaluator",
                name="Task Adherence",
                evaluator_name="builtin.task_adherence",
                evaluator_version="1",
                initialization_parameters={"deployment_name": model_deployment},
            ),
            TestingCriterionAzureAIEvaluator(
                type="azure_ai_evaluator",
                name="Sensitive Data Leakage",
                evaluator_name="builtin.sensitive_data_leakage",
                evaluator_version="1",
            ),
        ],
    )
    print(f"[{agent_cfg.name}] eval_id: {red_team_eval.id}")

    taxonomy_payload = EvaluationTaxonomy(
        description=f"Red-team taxonomy for {agent_cfg.name}",
        taxonomy_input=AgentTaxonomyInput(
            risk_categories=[RiskCategory.PROHIBITED_ACTIONS],
            target=target,
        ),
    )
    try:
        taxonomy = project.beta.evaluation_taxonomies.create(
            name=temp_agent_name,
            taxonomy=taxonomy_payload,
        )
    except TypeError:
        taxonomy = project.beta.evaluation_taxonomies.create(
            name=temp_agent_name,
            body=taxonomy_payload,
        )
    print(f"[{agent_cfg.name}] taxonomy_id: {taxonomy.id}")

    eval_run = openai_client.evals.runs.create(
        eval_id=red_team_eval.id,
        name=run_name,
        data_source=RedTeamEvalRunDataSource(
            type="azure_ai_red_team",
            item_generation_params={
                "type": "red_team_taxonomy",
                "attack_strategies": ["Flip", "Base64"],
                "num_turns": 5,
                "source": {"type": "file_id", "id": taxonomy.id},
            },
            target=target.as_dict(),
        ),
    )
    print(f"[{agent_cfg.name}] run_id: {eval_run.id} status={eval_run.status}")

    deadline = time.time() + POLL_TIMEOUT_SECONDS
    last_status = eval_run.status
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        eval_run = openai_client.evals.runs.retrieve(
            run_id=eval_run.id,
            eval_id=red_team_eval.id,
        )
        if eval_run.status != last_status:
            print(f"[{agent_cfg.name}] status: {eval_run.status}")
            last_status = eval_run.status
        if (eval_run.status or "").lower() in TERMINAL_STATUSES:
            break
    else:
        raise TimeoutError(f"Timed out waiting for red-team run: {eval_run.id}")

    agent_artifact = artifacts_dir / f"redteam-foundry-{agent_cfg.name}.json"
    agent_artifact.write_text(
        json.dumps(
            {
                "agent": agent_cfg.name,
                "temp_prompt_agent": {
                    "name": temp_agent_name,
                    "version": agent_version.version,
                },
                "eval_id": red_team_eval.id,
                "run_id": eval_run.id,
                "taxonomy_id": taxonomy.id,
                "status": eval_run.status,
                "result_counts": _to_json(getattr(eval_run, "result_counts", None)),
                "report_url": getattr(eval_run, "report_url", None),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[{agent_cfg.name}] artifact: {agent_artifact}")

    try:
        output_items = list(
            openai_client.evals.runs.output_items.list(
                run_id=eval_run.id,
                eval_id=red_team_eval.id,
            )
        )
        items_artifact = artifacts_dir / f"redteam-foundry-{agent_cfg.name}-output-items.json"
        items_artifact.write_text(json.dumps(_to_json(output_items), indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[{agent_cfg.name}] output items warning: {type(exc).__name__}: {exc}")

    try:
        project.agents.delete(agent_name=temp_agent_name)
        print(f"[{agent_cfg.name}] deleted temporary prompt agent.")
    except Exception as exc:
        print(f"[{agent_cfg.name}] cleanup warning: {type(exc).__name__}: {exc}")

    return {
        "agent": agent_cfg.name,
        "status": eval_run.status,
        "eval_id": red_team_eval.id,
        "run_id": eval_run.id,
        "taxonomy_id": taxonomy.id,
        "report_url": getattr(eval_run, "report_url", None),
        "result_counts": _to_json(getattr(eval_run, "result_counts", None)),
        "temp_prompt_agent_name": temp_agent_name,
        "temp_prompt_agent_version": agent_version.version,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Foundry portal red-team scans for both agents.")
    parser.add_argument("--env-name", default=None, help="azd environment name (optional)")
    parser.add_argument(
        "--output-path",
        default=None,
        help="Summary output path (default: artifacts/redteam/foundry-redteam-summary.json)",
    )
    args = parser.parse_args()

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    artifacts_dir = repo_root / "artifacts" / "redteam"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    output_path = pathlib.Path(args.output_path) if args.output_path else artifacts_dir / "foundry-redteam-summary.json"
    if not output_path.is_absolute():
        output_path = (repo_root / output_path).resolve()

    env_map = _load_azd_env(args.env_name)
    endpoint = env_map.get("AZURE_AI_PROJECT_ENDPOINT") or env_map.get("AZURE_AIPROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError("AZURE_AI_PROJECT_ENDPOINT missing from azd environment.")

    model_deployment = env_map.get("AZURE_AI_MODEL_DEPLOYMENT_NAME") or "gpt-5.4"

    agents = [
        AgentConfig(
            name="field-ops-agent",
            prompt_path=repo_root / "src" / "field-ops-agent" / ".agent_configs" / "baseline" / "instructions.md",
        ),
        AgentConfig(
            name="fibey-coordinator",
            prompt_path=repo_root / "src" / "fibey-coordinator" / ".agent_configs" / "baseline" / "instructions.md",
        ),
    ]
    for agent_cfg in agents:
        if not agent_cfg.prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found for {agent_cfg.name}: {agent_cfg.prompt_path}")

    print(f"endpoint: {endpoint}")
    print(f"model:    {model_deployment}")

    project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    openai_client = project.get_openai_client()

    run_summaries: list[dict[str, Any]] = []
    for agent_cfg in agents:
        run_summaries.append(
            _create_redteam_run_for_agent(
                project=project,
                openai_client=openai_client,
                agent_cfg=agent_cfg,
                model_deployment=model_deployment,
                artifacts_dir=artifacts_dir,
            )
        )

    summary = {
        "run_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "env_name": env_map.get("AZURE_ENV_NAME", args.env_name),
        "endpoint": endpoint,
        "model_deployment": model_deployment,
        "runs": run_summaries,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"summary:  {output_path}")

    failed = [r for r in run_summaries if (r.get("status") or "").lower() != "completed"]
    if failed:
        names = ", ".join([f"{r['agent']}={r.get('status')}" for r in failed])
        raise RuntimeError(f"One or more Foundry red-team runs did not complete successfully: {names}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        raise
