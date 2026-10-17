"""
factory.orchestration.boot
===========================
Startup reconstruction for plan execution (roadmap 3.3).

Before a run starts, this module reconciles three sources of truth:

1. the approved plan's DAG (what should exist),
2. the persisted task rows (what the last run recorded), and
3. the last checkpoint (plan identity + workspace file hashes).

Rules
-----
* A plan's exact identity is verified against the checkpoint and every
  persisted task row that carries a plan id.
* A task recorded RUNNING (or PAUSED) when the process stopped is
  **uncertain**: its side effects may be partial. It is converted to a
  terminal ``failed`` state marked ``recovery_required`` and never
  retried automatically.
* A completed task whose evidence is missing is not trusted as complete.
* A workspace file whose hash no longer matches the checkpoint taints the
  checkpoint's task and requires human resolution.
* Everything else is preserved exactly: completed stays completed,
  awaiting-approval stays parked, failed keeps its retry semantics.

The function is idempotent: repeated boots produce the same result and
never re-apply destructive conversions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.audit.evidence import hash_file
from factory.database import get_connection
from factory.models import TaskStatus
from factory.orchestration.dag import TaskDAG
from factory.orchestration.recovery import RecoveryManager
from factory.orchestration.task_store import TaskStore

logger = logging.getLogger(__name__)

#: Version of the state blob written into checkpoint.orchestrator_state.
CHECKPOINT_STATE_VERSION = 1


@dataclass
class BootResult:
    """Outcome of a startup reconstruction."""

    dag: TaskDAG
    issues: list[str] = field(default_factory=list)
    recovery_required: bool = False
    restored: dict[str, str] = field(default_factory=dict)
    completed_task_ids: list[str] = field(default_factory=list)
    uncertain_task_ids: list[str] = field(default_factory=list)
    awaiting_approval_task_ids: list[str] = field(default_factory=list)
    checkpoint_id: str | None = None


def _load_metadata(row: Any) -> dict[str, Any]:
    try:
        value = json.loads(row["metadata"]) if row is not None else {}
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _has_evidence(task_id: str, db_path: Path | None) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return bool(row and int(row["n"]) > 0)


def prepare_dag_for_execution(
    dag: TaskDAG,
    *,
    project_id: str,
    plan_id: str = "",
    plan_hash: str = "",
    db_path: Path | None = None,
    recovery_manager: RecoveryManager | None = None,
    audit_logger: Any = None,
    require_evidence_for_completed: bool = True,
) -> BootResult:
    """
    Reconcile *dag* with persisted state and return the boot result.

    Callers must refuse to execute when ``recovery_required`` is True.
    """
    store = TaskStore(db_path)
    manager = recovery_manager or RecoveryManager(db_path)
    result = BootResult(dag=dag)

    def _emit(event_type: str, payload: dict[str, Any], task_id: str | None = None) -> None:
        if audit_logger is not None:
            try:
                audit_logger.emit(event_type, payload, task_id=task_id)
            except Exception:  # noqa: BLE001 — audit must not break recovery
                logger.exception("Audit emit failed for %s", event_type)

    _emit("recovery.started", {
        "project_id": project_id,
        "plan_id": plan_id,
        "plan_hash": plan_hash,
    })

    checkpoint = manager.load_last_checkpoint(project_id)
    forced_uncertain: dict[str, str] = {}

    if checkpoint is not None:
        result.checkpoint_id = checkpoint.checkpoint_id
        state = checkpoint.orchestrator_state or {}
        state_version = state.get("schema_version")
        if state_version is not None and int(state_version) != CHECKPOINT_STATE_VERSION:
            issue = (
                f"Checkpoint {checkpoint.checkpoint_id} was written by an "
                f"incompatible state version ({state_version}); human review "
                "is required before continuing."
            )
            result.issues.append(issue)
            result.recovery_required = True
        checkpoint_plan_hash = str(state.get("plan_hash", "") or "")
        if checkpoint_plan_hash and plan_hash and checkpoint_plan_hash != plan_hash:
            issue = (
                f"Checkpoint {checkpoint.checkpoint_id} belongs to plan hash "
                f"{checkpoint_plan_hash[:12]}… but the requested plan is "
                f"{plan_hash[:12]}…. Refusing to mix plan versions."
            )
            result.issues.append(issue)
            result.recovery_required = True

        for path_str, expected in (checkpoint.workspace_hashes or {}).items():
            path = Path(path_str)
            if not path.exists():
                issue = (
                    f"Checkpoint workspace file {path_str!r} is missing; "
                    "the previous state cannot be verified."
                )
                result.issues.append(issue)
                result.recovery_required = True
                continue
            actual = hash_file(path)
            if actual != expected:
                issue = (
                    f"Workspace file {path_str!r} changed since checkpoint "
                    f"{checkpoint.checkpoint_id}: content hash mismatch."
                )
                result.issues.append(issue)
                result.recovery_required = True
                tainted_task = str(checkpoint.task_id or "")
                if tainted_task and dag.get(tainted_task) is not None:
                    forced_uncertain[tainted_task] = (
                        "Outputs changed after the task completed; the "
                        "recorded evidence no longer matches."
                    )

    rows = {row["task_id"]: row for row in store.list_rows(project_id)}

    for node in dag.all_nodes():
        row = rows.get(node.task_id)
        if row is None:
            result.restored[node.task_id] = "pending"
            continue

        row_meta = _load_metadata(row)

        # Runtime facts persisted by the last run overlay the plan metadata.
        for key, value in row_meta.items():
            if key in ("plan_id", "plan_hash", "project_id"):
                continue
            node.metadata[key] = value
        node.metadata["plan_id"] = plan_id
        node.metadata["plan_hash"] = plan_hash
        node.metadata["project_id"] = project_id

        row_plan_id = str(row_meta.get("plan_id", "") or "")
        if row_plan_id and plan_id and row_plan_id != plan_id:
            issue = (
                f"Task {node.task_id!r} belongs to plan {row_plan_id} but "
                f"the requested plan is {plan_id}; refusing to reuse it."
            )
            result.issues.append(issue)
            result.recovery_required = True
            node.metadata["terminal_failure"] = True
            node.metadata["recovery_required"] = True
            dag.update_status(node.task_id, TaskStatus.FAILED)
            result.restored[node.task_id] = "failed"
            _emit("recovery.task_plan_mismatch", {
                "issue": issue,
            }, task_id=node.task_id)
            continue

        status = str(row["status"])

        if node.task_id in forced_uncertain or status == TaskStatus.RUNNING.value:
            reason = forced_uncertain.get(node.task_id) or (
                f"Task {node.task_id!r} was RUNNING when the process "
                "stopped; side effects are uncertain."
            )
            store.mark_running_uncertain(node.task_id, reason)
            node.metadata["recovery_required"] = True
            node.metadata["terminal_failure"] = True
            dag.update_status(node.task_id, TaskStatus.FAILED)
            result.restored[node.task_id] = "recovery_required"
            result.uncertain_task_ids.append(node.task_id)
            result.issues.append(reason)
            result.recovery_required = True
            _emit("recovery.task_uncertain", {
                "reason": reason,
            }, task_id=node.task_id)
            continue

        if status == TaskStatus.PAUSED.value:
            reason = (
                f"Task {node.task_id!r} was PAUSED at shutdown; it is not "
                "safe to continue automatically."
            )
            store.mark_running_uncertain(node.task_id, reason)
            node.metadata["recovery_required"] = True
            node.metadata["terminal_failure"] = True
            dag.update_status(node.task_id, TaskStatus.FAILED)
            result.restored[node.task_id] = "recovery_required"
            result.uncertain_task_ids.append(node.task_id)
            result.issues.append(reason)
            result.recovery_required = True
            _emit("recovery.task_uncertain", {
                "reason": reason,
            }, task_id=node.task_id)
            continue

        if status == TaskStatus.COMPLETED.value:
            if require_evidence_for_completed and not _has_evidence(
                node.task_id, db_path
            ):
                reason = (
                    f"Task {node.task_id!r} is recorded COMPLETED but has no "
                    "evidence rows; completion cannot be verified."
                )
                store.mark_running_uncertain(node.task_id, reason)
                node.metadata["recovery_required"] = True
                node.metadata["terminal_failure"] = True
                dag.update_status(node.task_id, TaskStatus.FAILED)
                result.restored[node.task_id] = "recovery_required"
                result.uncertain_task_ids.append(node.task_id)
                result.issues.append(reason)
                result.recovery_required = True
                _emit("recovery.task_without_evidence", {
                    "reason": reason,
                }, task_id=node.task_id)
                continue
            # Idempotent preservation: never re-run completed work.
            dag.update_status(node.task_id, TaskStatus.COMPLETED)
            result.restored[node.task_id] = "completed"
            result.completed_task_ids.append(node.task_id)
            _emit("recovery.task_preserved", {
                "status": "completed",
            }, task_id=node.task_id)
            continue

        if status == TaskStatus.AWAITING_APPROVAL.value:
            request_id = str(row_meta.get("approval_request_id", "") or "")
            node.metadata["approval_request_id"] = request_id
            if not request_id:
                reason = (
                    f"Task {node.task_id!r} is AWAITING_APPROVAL without an "
                    "approval request id; nothing can ever resume it."
                )
                node.metadata["terminal_failure"] = True
                node.metadata["recovery_required"] = True
                dag.update_status(node.task_id, TaskStatus.FAILED)
                result.restored[node.task_id] = "recovery_required"
                result.uncertain_task_ids.append(node.task_id)
                result.issues.append(reason)
                result.recovery_required = True
                _emit("recovery.task_uncertain", {
                    "reason": reason,
                }, task_id=node.task_id)
                continue
            dag.update_status(node.task_id, TaskStatus.AWAITING_APPROVAL)
            result.restored[node.task_id] = "awaiting_approval"
            result.awaiting_approval_task_ids.append(node.task_id)
            _emit("recovery.task_preserved", {
                "status": "awaiting_approval",
                "approval_request_id": request_id,
            }, task_id=node.task_id)
            continue

        if status == TaskStatus.FAILED.value:
            dag.update_status(node.task_id, TaskStatus.FAILED)
            result.restored[node.task_id] = "failed"
            if row_meta.get("recovery_required"):
                result.recovery_required = True
                result.uncertain_task_ids.append(node.task_id)
                result.issues.append(
                    f"Task {node.task_id!r} requires recovery resolution: "
                    + str(row_meta.get("recovery_reason", "uncertain state"))
                )
            continue

        if status == TaskStatus.SKIPPED.value:
            dag.update_status(node.task_id, TaskStatus.SKIPPED)
            result.restored[node.task_id] = "skipped"
            continue

        if status == TaskStatus.CANCELLED.value:
            dag.update_status(node.task_id, TaskStatus.CANCELLED)
            result.restored[node.task_id] = "cancelled"
            continue

        if status == TaskStatus.PENDING.value:
            dag.update_status(node.task_id, TaskStatus.PENDING)
            result.restored[node.task_id] = "pending"
            continue

        issue = (
            f"Task {node.task_id!r} has unknown persisted status "
            f"{status!r}; human review is required."
        )
        result.issues.append(issue)
        result.recovery_required = True
        result.uncertain_task_ids.append(node.task_id)
        node.metadata["terminal_failure"] = True
        node.metadata["recovery_required"] = True
        dag.update_status(node.task_id, TaskStatus.FAILED)
        _emit("recovery.unknown_status", {
            "status": status,
        }, task_id=node.task_id)

    _emit("recovery.finished", {
        "recovery_required": result.recovery_required,
        "issues": result.issues,
        "completed": result.completed_task_ids,
        "uncertain": result.uncertain_task_ids,
        "restored": result.restored,
    })
    logger.info(
        "Startup reconstruction for project %s: %d completed, %d uncertain, "
        "recovery_required=%s.",
        project_id, len(result.completed_task_ids),
        len(result.uncertain_task_ids), result.recovery_required,
    )
    return result
