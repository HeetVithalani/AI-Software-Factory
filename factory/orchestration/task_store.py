"""
factory.orchestration.task_store
=================================
Persistent task-state repository used by plan execution and recovery.

The DAG is in-memory; this store is the durable record the recovery path
reads. It deliberately never overwrites a task's runtime facts when seeding
(so a resumed run keeps its status, retry counts, approval bindings), and
every status transition during execution flows through :meth:`upsert_node`.

Recovery semantics
------------------
* ``mark_running_uncertain`` converts a task that was RUNNING when the
  process died into a terminal ``failed`` state marked
  ``recovery_required`` + ``terminal_failure``. Automation must never
  repeat a side effect whose completion is unknown; a human resolves it
  explicitly via :meth:`resolve_recovery`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.database import get_connection
from factory.models import TaskStatus
from factory.orchestration.dag import TaskNode

logger = logging.getLogger(__name__)

_TERMINAL = {
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
    TaskStatus.SKIPPED.value,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """SQLite-backed task record store scoped to one project."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Seeding / updates
    # ------------------------------------------------------------------

    def seed_node(
        self,
        project_id: str,
        node: TaskNode,
        *,
        plan_id: str = "",
        plan_hash: str = "",
    ) -> None:
        """
        Ensure a row exists for *node* without disturbing runtime state.

        Plan identity is recorded in metadata so recovery can detect that a
        task row belongs to a different plan version.
        """
        identity = {
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "project_id": project_id,
        }
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT metadata FROM tasks WHERE task_id = ?",
                (node.task_id,),
            ).fetchone()
            if row is None:
                metadata = dict(node.metadata)
                metadata.update(identity)
                metadata.setdefault("max_retries", 3)
                conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, project_id, name, description, agent_type,
                        status, risk_level, depends_on, created_at,
                        retry_count, max_retries, recovery_strategy,
                        result_summary, metadata
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 'safe', ?, ?, 0,
                              ?, 'retry_same', '', ?)
                    """,
                    (
                        node.task_id,
                        project_id,
                        node.name,
                        str(node.metadata.get("description", "")),
                        node.agent_type,
                        json.dumps(node.depends_on),
                        _now(),
                        int(node.metadata.get("max_retries", 3)),
                        json.dumps(metadata),
                    ),
                )
            else:
                try:
                    metadata = json.loads(row["metadata"])
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                metadata.update(identity)
                conn.execute(
                    "UPDATE tasks SET name = ?, depends_on = ?, metadata = ? "
                    "WHERE task_id = ?",
                    (
                        node.name,
                        json.dumps(node.depends_on),
                        json.dumps(metadata),
                        node.task_id,
                    ),
                )
            conn.commit()

    def upsert_node(
        self,
        project_id: str,
        node: TaskNode,
        *,
        error: str | None = None,
        result_summary: str | None = None,
    ) -> None:
        """Persist the node's current status and runtime metadata."""
        status = node.status.value
        now = _now()
        metadata = {k: v for k, v in node.metadata.items()}
        metadata.setdefault("project_id", project_id)
        try:
            retry_count = int(node.metadata.get("retry_count", 0))
        except (TypeError, ValueError):
            retry_count = 0
        try:
            max_retries = int(node.metadata.get("max_retries", 3))
        except (TypeError, ValueError):
            max_retries = 3
        strategy = str(node.metadata.get("recovery_strategy", "retry_same"))

        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT created_at, started_at, completed_at FROM tasks "
                "WHERE task_id = ?",
                (node.task_id,),
            ).fetchone()
            created_at = row["created_at"] if row is not None else now
            started_at = row["started_at"] if row is not None else None
            completed_at = row["completed_at"] if row is not None else None

            if status == TaskStatus.RUNNING.value and not started_at:
                started_at = now
            if status in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value):
                completed_at = None
            elif status in _TERMINAL:
                completed_at = completed_at or now

            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, project_id, name, description, agent_type,
                    status, risk_level, depends_on, created_at, started_at,
                    completed_at, error, retry_count, max_retries,
                    recovery_strategy, result_summary, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, 'safe', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    error = excluded.error,
                    retry_count = excluded.retry_count,
                    max_retries = excluded.max_retries,
                    recovery_strategy = excluded.recovery_strategy,
                    result_summary = excluded.result_summary,
                    metadata = excluded.metadata,
                    name = excluded.name,
                    description = excluded.description,
                    agent_type = excluded.agent_type,
                    depends_on = excluded.depends_on
                """,
                (
                    node.task_id,
                    project_id,
                    node.name,
                    str(node.metadata.get("description", "")),
                    node.agent_type,
                    status,
                    json.dumps(node.depends_on),
                    created_at,
                    started_at,
                    completed_at,
                    error,
                    retry_count,
                    max_retries,
                    strategy,
                    result_summary or "",
                    json.dumps(metadata),
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_rows(self, project_id: str) -> list[Any]:
        with get_connection(self.db_path) as conn:
            return conn.execute(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()

    def get_row(self, task_id: str) -> Any:
        with get_connection(self.db_path) as conn:
            return conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()

    def status_counts(self, project_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.list_rows(project_id):
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Recovery-required resolution
    # ------------------------------------------------------------------

    def mark_running_uncertain(self, task_id: str, reason: str) -> None:
        """Convert an interrupted RUNNING task into a fail-closed state."""
        row = self.get_row(task_id)
        metadata: dict[str, Any] = {}
        if row is not None:
            try:
                metadata = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        metadata["recovery_required"] = True
        metadata["terminal_failure"] = True
        metadata["recovery_reason"] = reason
        metadata["recovered_at"] = _now()
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = 'failed', error = ?, "
                "completed_at = ?, metadata = ? WHERE task_id = ?",
                (reason, _now(), json.dumps(metadata), task_id),
            )
            conn.commit()
        logger.warning(
            "Task %s marked recovery_required: %s", task_id, reason
        )

    def resolve_recovery(
        self,
        task_id: str,
        action: str,
        *,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        """
        Apply an explicit human resolution to a recovery-required task.

        ``retry`` requeues the task (budget untouched); ``skip`` marks it
        terminal-skipped. Anything else is refused.
        """
        if action not in ("retry", "skip"):
            raise ValueError("action must be 'retry' or 'skip'")
        if not actor or not actor.strip():
            raise ValueError("an actor is required to resolve recovery")
        row = self.get_row(task_id)
        if row is None:
            raise ValueError(f"task {task_id!r} does not exist")
        try:
            metadata = json.loads(row["metadata"])
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if not metadata.get("recovery_required"):
            raise ValueError(
                f"task {task_id!r} is not marked recovery_required; "
                "explicit resolution is only for uncertain side effects"
            )

        metadata.pop("recovery_required", None)
        metadata.pop("terminal_failure", None)
        metadata["recovery_resolution"] = {
            "action": action,
            "actor": actor.strip(),
            "note": note,
            "resolved_at": _now(),
        }
        if action == "retry":
            with get_connection(self.db_path) as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'pending', error = NULL, "
                    "started_at = NULL, completed_at = NULL, metadata = ? "
                    "WHERE task_id = ?",
                    (json.dumps(metadata), task_id),
                )
                conn.commit()
        else:
            metadata["terminal_failure"] = True
            with get_connection(self.db_path) as conn:
                conn.execute(
                    "UPDATE tasks SET status = 'skipped', completed_at = ?, "
                    "metadata = ? WHERE task_id = ?",
                    (_now(), json.dumps(metadata), task_id),
                )
                conn.commit()
        logger.info(
            "Recovery resolved for task %s: action=%s actor=%s.",
            task_id, action, actor,
        )
        return metadata
