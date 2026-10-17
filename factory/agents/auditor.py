"""
factory.agents.auditor
=======================
Auditor agent — deterministic final audit (the release gate).

The release decision comes exclusively from
:mod:`factory.validation.final_audit`, which re-checks persisted facts:
approved plan identity/hash, pipeline eligibility, completion status,
absence of uncertain work, evidence presence, and latest evidence file
hashes. No LLM output participates in the decision; retrying an
unverifiable state cannot be invented by a model.

Output artifact: <workspace>/audit_report.json
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from factory.models import AgentResult, TaskRecord
from factory.agents.base import BaseAgent

logger = logging.getLogger(__name__)

_MAX_EVENTS = 500


class AuditorAgent(BaseAgent):
    """Runs the deterministic final audit and fails closed on violations."""

    agent_type = "auditor"

    def run(self, task: TaskRecord) -> AgentResult:
        self._emit("agent.auditor.started", {"task_id": task.task_id})

        project_id = task.project_id or self._project_id or ""
        plan_id = str(task.metadata.get("plan_id", "") or "")
        plan_hash = str(task.metadata.get("plan_hash", "") or "")
        pipeline = str(task.metadata.get("pipeline", "") or "")

        # ------------------------------------------------------------------
        # Gather persisted facts (never trust in-memory claims)
        # ------------------------------------------------------------------
        plan_payload: dict[str, Any] | None = None
        if plan_id:
            try:
                from factory.planning.plan_store import PlanStore
                version = PlanStore().load(plan_id)
                if version is not None:
                    plan_payload = version.plan
                    if not pipeline:
                        pipeline = str(
                            plan_payload.get("pipeline", "") or ""
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Auditor: could not load plan %s: %s", plan_id, exc
                )

        task_rows: list[Any] = []
        if project_id:
            try:
                from factory.orchestration.task_store import TaskStore
                task_rows = TaskStore().list_rows(project_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Auditor: could not load task rows: %s", exc
                )

        from factory.validation.final_audit import run_final_audit

        audit_result = run_final_audit(
            project_id=project_id,
            task_rows=task_rows,
            exclude_task_id=task.task_id,
            plan=plan_payload,
            plan_id=plan_id,
            plan_hash=plan_hash,
            pipeline=pipeline,
            workspace_root=self.workspace_root,
        )

        events: list[dict[str, Any]] = []
        type_counts: dict[str, int] = {}
        try:
            from factory.audit.logger import list_events
            events = list_events(project_id=project_id, limit=_MAX_EVENTS)
            for event in events:
                etype = event.get("event_type", "unknown")
                type_counts[etype] = type_counts.get(etype, 0) + 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("AuditorAgent: could not load events: %s", exc)

        report = {
            "task_id": task.task_id,
            "project_id": project_id,
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "pipeline": pipeline,
            **audit_result.to_dict(),
            "total_events": len(events),
            "event_type_summary": type_counts,
        }

        fs = self._make_filesystem_tool()
        write_result = fs.write_json(
            Path("audit_report.json"), report, task_id=task.task_id
        )
        if not write_result.success:
            logger.warning(
                "AuditorAgent: could not write audit_report.json: %s",
                write_result.error,
            )

        evidence = [{"kind": "audit_report", "path": "audit_report.json"}]
        outputs = {
            "report_path": str(self._artifact_path("audit_report.json")),
            "passed": audit_result.passed,
            "release_eligible": audit_result.release_eligible,
            "failed_checks": [
                check.name for check in audit_result.failed_checks()
            ],
            "limitations": audit_result.limitations,
        }

        self._emit("agent.auditor.completed", {
            "task_id": task.task_id,
            "passed": audit_result.passed,
            "release_eligible": audit_result.release_eligible,
            "failed_checks": outputs["failed_checks"],
            "limitations": audit_result.limitations,
        })

        if not audit_result.passed:
            failure = self._fail(
                task,
                "Deterministic final audit failed: "
                + audit_result.failure_summary(),
            )
            failure.outputs.update(outputs)
            failure.evidence = evidence
            failure.failure_kind = "permanent"
            return failure

        summary = (
            "Deterministic final audit passed. "
            f"release_eligible={audit_result.release_eligible}."
        )
        if audit_result.limitations:
            summary += (
                f" Limitations ({len(audit_result.limitations)}): "
                + "; ".join(limitation[:80] for limitation in
                            audit_result.limitations[:3])
            )
        return self._ok(task, summary=summary, outputs=outputs,
                        evidence=evidence)
