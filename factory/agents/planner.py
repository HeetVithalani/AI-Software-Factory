"""
factory.agents.planner
=======================
Planner agent — decomposes a project description into a task DAG.

The planner reads project metadata from its TaskRecord and produces a
``plan.json`` artifact in the workspace. This artifact is consumed by
the orchestrator to build the actual TaskDAG.

Current implementation: deterministic keyword-based decomposition.
LLM-based decomposition can be layered on once the model registry is
integrated (the plan.json output format will stay identical).

Output artifact: ``<workspace>/plan.json``

plan.json format::

    {
      "project_id": "...",
      "tasks": [
        {
          "task_id": "task-build",
          "name": "Build",
          "agent_type": "builder",
          "depends_on": ["task-plan"],
          "description": "...",
          "metadata": {}
        },
        ...
      ]
    }
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from factory.models import AgentResult, TaskRecord
from factory.agents.base import BaseAgent

logger = logging.getLogger(__name__)

# Default task pipeline for a standard software project
_DEFAULT_PIPELINE: list[dict[str, Any]] = [
    {
        "name": "Requirements Analysis",
        "agent_type": "planner",
        "description": "Analyse and structure the project requirements",
        "depends_on": [],
    },
    {
        "name": "Security Baseline",
        "agent_type": "security",
        "description": "Scan workspace for pre-existing sensitive data",
        "depends_on": ["requirements"],
    },
    {
        "name": "Build",
        "agent_type": "builder",
        "description": "Implement the required functionality",
        "depends_on": ["requirements"],
    },
    {
        "name": "Review",
        "agent_type": "reviewer",
        "description": "Review the built artefacts for correctness",
        "depends_on": ["build"],
    },
    {
        "name": "Test",
        "agent_type": "qa",
        "description": "Run the automated test suite",
        "depends_on": ["build"],
    },
    {
        "name": "Security Scan",
        "agent_type": "security",
        "description": "Scan for sensitive data leaks and vulnerabilities",
        "depends_on": ["build"],
    },
    {
        "name": "Documentation",
        "agent_type": "documentation",
        "description": "Generate project documentation",
        "depends_on": ["review", "test"],
    },
    {
        "name": "Audit",
        "agent_type": "auditor",
        "description": "Produce final audit report",
        "depends_on": ["security_scan", "documentation"],
    },
]

# Keyword → additional tasks to inject
_KEYWORD_TASK_AUGMENTS: dict[str, list[dict[str, Any]]] = {
    "api": [{
        "name": "API Integration Test",
        "agent_type": "qa",
        "description": "Run API-specific integration tests",
        "depends_on": ["build"],
    }],
    "database": [{
        "name": "Database Migration Check",
        "agent_type": "reviewer",
        "description": "Review database schema and migration scripts",
        "depends_on": ["build"],
    }],
}


def _slugify(name: str) -> str:
    """Convert a task name to a safe task_id slug."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "task"


def _build_plan(
    project_id: str,
    description: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """
    Build a plan dict deterministically from the project description.
    """
    description_lower = description.lower()

    pipeline = list(_DEFAULT_PIPELINE)

    # Inject keyword-triggered tasks
    for keyword, extra_tasks in _KEYWORD_TASK_AUGMENTS.items():
        if keyword in description_lower:
            pipeline.extend(extra_tasks)

    # Assign stable task_ids and resolve depends_on references
    # First pass: assign IDs
    name_to_id: dict[str, str] = {}
    tasks_with_ids: list[dict[str, Any]] = []

    for task_def in pipeline:
        slug = _slugify(task_def["name"])
        task_id = f"task-{slug}"
        # Deduplicate: if slug already used, append index
        if task_id in name_to_id.values():
            task_id = f"task-{slug}-{uuid.uuid4().hex[:4]}"
        name_to_id[task_def["name"].lower()] = task_id
        name_to_id[slug] = task_id
        tasks_with_ids.append({**task_def, "task_id": task_id})

    # Second pass: resolve depends_on slugs to task_ids
    resolved_tasks: list[dict[str, Any]] = []
    for task in tasks_with_ids:
        resolved_deps = []
        for dep in task.get("depends_on", []):
            dep_slug = _slugify(dep)
            dep_id = name_to_id.get(dep_slug) or name_to_id.get(dep)
            if dep_id:
                resolved_deps.append(dep_id)
            else:
                logger.warning("PlannerAgent: unknown dependency %r in task %r", dep, task["name"])
        resolved_tasks.append({
            "task_id": task["task_id"],
            "name": task["name"],
            "agent_type": task.get("agent_type", ""),
            "description": task.get("description", ""),
            "depends_on": resolved_deps,
            "metadata": task.get("metadata", {}),
        })

    return {
        "project_id": project_id,
        "description": description,
        "tasks": resolved_tasks,
    }


class PlannerAgent(BaseAgent):
    """
    Decomposes a project description into a task pipeline artifact using the LLM.

    Reads:  task.metadata["description"] and task.project_id
    Writes: <workspace>/plan.json
    Returns: AgentResult with outputs["plan_path"]
    """

    agent_type = "planner"

    def run(self, task: TaskRecord) -> AgentResult:
        self._emit("agent.planner.started", {"task_id": task.task_id})
        
        if not self._llm or not self._context:
            return self._fail(task, "LLM Router or Context Builder not provided to agent")

        description = (
            task.metadata.get("description")
            or task.description
            or "Generic software project"
        )
        project_id = task.project_id or ""

        logger.info(
            "PlannerAgent: planning project %r  %r",
            project_id, description[:80],
        )

        system_prompt = (
            "You are the Planner Agent. Your goal is to decompose a project description "
            "into a structured task DAG (Directed Acyclic Graph).\n"
            "Produce requirements, assumptions, architecture decisions, and a list of tasks.\n"
            "Each task must have a unique task_id, a descriptive name, an agent_type (e.g., 'builder', 'reviewer', 'qa', 'security', 'documentation', 'auditor'), "
            "and depends_on array referencing other task_ids.\n"
            "Typical flows involve builder -> qa -> reviewer -> security -> documentation -> auditor.\n"
        )
        
        messages = [
            {"role": "user", "content": f"Project Description:\n{description}\n\nPlease generate the comprehensive plan."}
        ]
        
        from factory.llm.schemas.planner import PlannerProposal
        
        try:
            request = self._context.build_request(
                project_id=project_id,
                system_prompt=system_prompt,
                messages=messages,
                response_schema=PlannerProposal,
            )
            response = self._llm.complete(request)
        except Exception as exc:
            return self._fail(task, f"LLM inference failed: {exc}")
            
        if not response.success:
            return self._fail(task, f"LLM failed: {response.error}")
            
        if not response.structured_output:
            return self._fail(task, "LLM returned no structured output")
            
        try:
            proposal = PlannerProposal.model_validate_json(response.structured_output)
        except Exception as exc:
            return self._fail(task, f"Failed to parse LLM output as PlannerProposal: {exc}")
            
        # Write plan artifact
        fs = self._make_filesystem_tool()
        plan_path = Path("plan.json")
        
        plan_data = {
            "project_id": project_id,
            "description": description,
            "decisions": proposal.decisions,
            "assumptions": proposal.assumptions,
            "tasks": [t.model_dump() for t in proposal.tasks],
        }
        
        write_result = fs.write_json(plan_path, plan_data, task_id=task.task_id)

        if not write_result.success:
            return self._fail(
                task,
                f"Failed to write plan.json: {write_result.error}",
            )

        task_count = len(plan_data["tasks"])
        self._emit("agent.planner.completed", {
            "task_id": task.task_id,
            "task_count": task_count,
            "plan_path": str(plan_path),
        })

        logger.info(
            "PlannerAgent: plan written to %s (%d tasks).",
            plan_path, task_count,
        )

        return self._ok(
            task,
            summary=proposal.summary,
            outputs={
                "plan_path": str(self._artifact_path("plan.json")),
                "task_count": task_count,
                "tasks": [t["task_id"] for t in plan_data["tasks"]],
                "decisions": proposal.decisions,
            },
            evidence=[{"kind": "file", "path": str(plan_path)}],
        )

