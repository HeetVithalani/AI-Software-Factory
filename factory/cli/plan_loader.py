"""
factory.cli.plan_loader
=======================
Loads a plan.json file into a TaskDAG and registers all factory agents
on an Orchestrator.

plan.json schema (from plan.json / PlannerAgent output)::

    {
      "project_id": "...",
      "description": "...",
      "tasks": [
        {
          "task_id": "task-build",
          "name": "Build",
          "agent_type": "builder",
          "description": "...",
          "depends_on": ["task-plan"],
          "metadata": {}
        },
        ...
      ]
    }

Design notes
------------
* This module is additive — it does NOT modify any existing module.
* All dependency validation is delegated to TaskDAG.validate() which
  already performs cycle detection and reference checking.
* Agent construction is centralised here so the CLI only needs to call
  two functions: load_dag_from_plan() and register_all_agents().
* Agents are only built when actually needed — this avoids unnecessary
  import overhead when running the status subcommand.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from factory.orchestration.dag import TaskDAG, TaskNode
from factory.errors import ConfigurationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Built-in agent types (Phase 2c)
# ---------------------------------------------------------------------------

# The complete set of registered built-in agent executors. Plans may
# only reference these types; nothing — least of all skill reference
# content, which is inert prompt text — can register a new executor or
# make the loader accept a foreign agent class.
BUILTIN_AGENT_TYPES: tuple[str, ...] = (
    "planner",
    "builder",
    "reviewer",
    "qa",
    "security",
    "documentation",
    "auditor",
)


# ---------------------------------------------------------------------------
# DAG loader
# ---------------------------------------------------------------------------

def load_dag_from_plan(plan_path: Path) -> tuple[TaskDAG, dict[str, Any]]:
    """
    Load a plan.json file and build a validated TaskDAG.

    Returns
    -------
    dag:
        TaskDAG populated with TaskNode objects in plan order.
    plan:
        The raw plan dict (for callers that need project_id etc.).

    Raises
    ------
    ConfigurationError
        If the file is missing, malformed, or produces a cyclic/invalid DAG.
    """
    if not plan_path.exists():
        raise ConfigurationError(
            f"plan.json not found at {plan_path}. "
            "Run 'factory run <project_path>' with a valid plan.json "
            "or let the PlannerAgent create one first."
        )

    try:
        with open(plan_path, encoding="utf-8") as fh:
            plan: dict[str, Any] = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"plan.json at {plan_path} is not valid JSON: {exc}"
        ) from exc

    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ConfigurationError(
            f"plan.json at {plan_path} has no 'tasks' list."
        )

    dag = TaskDAG()

    for raw in tasks:
        if not isinstance(raw, dict):
            raise ConfigurationError(
                f"plan.json: task entry is not a dict: {raw!r}"
            )
        task_id = str(raw.get("task_id", "")).strip()
        if not task_id:
            raise ConfigurationError(
                f"plan.json: task entry missing 'task_id': {raw!r}"
            )
        name = str(raw.get("name", task_id))
        agent_type = str(raw.get("agent_type", "")).strip()
        # Fail-closed (Phase 2c): any non-empty agent_type must be one of
        # the registered built-in agent types. Empty stays allowed for
        # backward compatibility (dispatch reports the missing executor
        # as before), but a *foreign* type is rejected at load time so it
        # can never reach agent construction or execution.
        if agent_type and agent_type not in BUILTIN_AGENT_TYPES:
            raise ConfigurationError(
                f"plan.json: task {task_id!r} has unknown agent_type "
                f"{agent_type!r}; only the registered built-in agent "
                f"types are allowed: {', '.join(BUILTIN_AGENT_TYPES)}"
            )
        depends_on = [str(d) for d in raw.get("depends_on", [])]
        metadata: dict[str, Any] = dict(raw.get("metadata", {}))
        # Carry description into metadata so agents can find it
        description = str(raw.get("description", ""))
        if description:
            metadata.setdefault("description", description)

        node = TaskNode(
            task_id=task_id,
            name=name,
            agent_type=agent_type,
            depends_on=depends_on,
            metadata=metadata,
        )
        dag.add(node)

    # Validate: missing dep references + cycle check
    try:
        dag.validate()
    except (ValueError, Exception) as exc:
        raise ConfigurationError(
            f"plan.json DAG validation failed: {exc}"
        ) from exc

    task_ids = [n.task_id for n in dag.all_nodes()]
    logger.info(
        "Loaded plan from %s: %d tasks — %s",
        plan_path,
        dag.node_count(),
        task_ids,
    )
    return dag, plan


def find_plan_path(project_path: Path, explicit_plan: Path | None = None) -> Path:
    """
    Resolve the plan.json path with a search order:
    1. Explicit --plan argument (if given)
    2. <project_path>/plan.json
    3. ./plan.json (factory root)
    """
    if explicit_plan is not None:
        return explicit_plan.resolve()
    candidate = project_path / "plan.json"
    if candidate.exists():
        return candidate
    fallback = Path("plan.json").resolve()
    return fallback


# ---------------------------------------------------------------------------
# Agent registration
# ---------------------------------------------------------------------------

def register_all_agents(
    orchestrator: Any,
    *,
    workspace_root: Path,
    broker: Any,
    audit_logger: Any,
    llm_router: Any,
    context_builder: Any,
    project_id: str = "",
) -> list[str]:
    """
    Instantiate all seven factory agents and register their run() methods
    as executors on the Orchestrator.

    Returns the list of registered agent_type strings.

    Design note
    -----------
    Imports are local to this function to avoid import cost on status/help
    subcommands. This matches the pattern used in cli/main.py.
    """
    from factory.agents.planner import PlannerAgent
    from factory.agents.builder import BuilderAgent
    from factory.agents.reviewer import ReviewerAgent
    from factory.agents.qa import QAAgent
    from factory.agents.security import SecurityAgent
    from factory.agents.documentation import DocumentationAgent
    from factory.agents.auditor import AuditorAgent

    agent_kwargs = dict(
        workspace_root=workspace_root,
        broker=broker,
        audit_logger=audit_logger,
        llm_router=llm_router,
        context_builder=context_builder,
        project_id=project_id,
    )

    agents = [
        PlannerAgent(**agent_kwargs),
        BuilderAgent(**agent_kwargs),
        ReviewerAgent(**agent_kwargs),
        QAAgent(**agent_kwargs),
        SecurityAgent(**agent_kwargs),
        DocumentationAgent(**agent_kwargs),
        AuditorAgent(**agent_kwargs),
    ]

    registered: list[str] = []
    for agent in agents:
        orchestrator.register_executor(agent.agent_type, agent.run)
        registered.append(agent.agent_type)
        logger.debug("Registered executor: %s", agent.agent_type)

    logger.info("Registered %d agents: %s", len(registered), registered)
    return registered
