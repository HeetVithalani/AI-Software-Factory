"""
factory.orchestration.recovery
================================
Crash checkpoint/recovery foundations.

On a clean restart the RecoveryManager:
1. Loads the last valid checkpoint
2. Inspects the actual workspace state (file hashes, task DB records)
3. Compares checkpoint state vs. actual state
4. Determines which tasks completed, which are partial/unknown
5. Invalidates unreliable checkpoint records
6. Returns a recovery plan for the orchestrator

Design note
-----------
We never blindly trust the last log entry.  A task marked RUNNING in the
checkpoint may have been killed mid-write.  The recovery manager re-reads
the actual database and workspace before deciding what is safe to trust.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.database import get_connection, to_json
from factory.errors import RecoveryError
from factory.models import CheckpointRecord, TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """In-memory representation of a recovery checkpoint."""
    checkpoint_id: str
    project_id: str | None
    task_id: str | None
    created_at: datetime
    orchestrator_state: dict[str, Any]
    workspace_hashes: dict[str, str]
    is_valid: bool = True
    invalidated_reason: str = ""


@dataclass
class RecoveryPlan:
    """
    Produced by RecoveryManager after workspace inspection.

    The orchestrator uses this to decide where to continue.
    """
    safe_to_continue: bool = False
    completed_task_ids: list[str] = field(default_factory=list)
    # Tasks that were running and must be re-evaluated
    uncertain_task_ids: list[str] = field(default_factory=list)
    # Tasks whose partial work must be undone before retry
    tainted_task_ids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    last_valid_checkpoint_id: str | None = None


class RecoveryManager:
    """
    Manages crash checkpoints and recovery logic.

    Usage::

        rm = RecoveryManager()
        checkpoint_id = rm.save_checkpoint(state_dict, project_id="p1")
        plan = rm.recover(project_id="p1")
    """

    def __init__(self, db_path: Path | None = None) -> None:
        from factory.config import DB_PATH as _DB_PATH
        self._db_path = db_path or _DB_PATH

    # ------------------------------------------------------------------
    # Checkpoint persistence
    # ------------------------------------------------------------------

    def save_checkpoint(
        self,
        orchestrator_state: dict[str, Any],
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        workspace_paths: list[Path] | None = None,
    ) -> str:
        """
        Persist a checkpoint snapshot to the database.

        Optionally hashes workspace files for integrity verification.
        Returns the checkpoint_id.
        """
        from factory.audit.evidence import hash_file

        workspace_hashes: dict[str, str] = {}
        if workspace_paths:
            for p in workspace_paths:
                if p.is_file():
                    workspace_hashes[str(p)] = hash_file(p)

        record = CheckpointRecord(
            project_id=project_id,
            task_id=task_id,
            orchestrator_state=orchestrator_state,
            workspace_hashes=workspace_hashes,
        )

        now = datetime.now(timezone.utc).isoformat()
        with get_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, project_id, task_id, created_at,
                    orchestrator_state, workspace_hashes, is_valid,
                    invalidated_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.checkpoint_id,
                    record.project_id,
                    record.task_id,
                    now,
                    to_json(record.orchestrator_state),
                    to_json(record.workspace_hashes),
                    1,
                    "",
                ),
            )
            conn.commit()

        logger.info(
            "Checkpoint %s saved (project=%s, task=%s, files=%d).",
            record.checkpoint_id, project_id, task_id,
            len(workspace_hashes),
        )
        return record.checkpoint_id

    def load_last_checkpoint(
        self, project_id: str | None = None
    ) -> Checkpoint | None:
        """Load the most recent valid checkpoint for a project (or global)."""
        if project_id:
            sql = """
                SELECT * FROM checkpoints
                WHERE is_valid = 1 AND project_id = ?
                ORDER BY created_at DESC LIMIT 1
            """
            params: tuple = (project_id,)
        else:
            sql = """
                SELECT * FROM checkpoints
                WHERE is_valid = 1
                ORDER BY created_at DESC LIMIT 1
            """
            params = ()

        with get_connection(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()

        if row is None:
            return None

        try:
            state = json.loads(row["orchestrator_state"])
            hashes = json.loads(row["workspace_hashes"])
        except (json.JSONDecodeError, TypeError):
            state = {}
            hashes = {}

        return Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            orchestrator_state=state,
            workspace_hashes=hashes,
            is_valid=bool(row["is_valid"]),
            invalidated_reason=row["invalidated_reason"] or "",
        )

    def invalidate_checkpoint(
        self, checkpoint_id: str, reason: str
    ) -> None:
        """Mark a checkpoint as invalid (cannot be trusted for recovery)."""
        with get_connection(self._db_path) as conn:
            conn.execute(
                """
                UPDATE checkpoints SET is_valid = 0, invalidated_reason = ?
                WHERE checkpoint_id = ?
                """,
                (reason, checkpoint_id),
            )
            conn.commit()
        logger.warning(
            "Checkpoint %s invalidated: %s", checkpoint_id, reason
        )

    # ------------------------------------------------------------------
    # Recovery plan
    # ------------------------------------------------------------------

    def recover(self, project_id: str | None = None) -> RecoveryPlan:
        """
        Inspect the actual database state and produce a RecoveryPlan.

        Steps:
        1. Load the last valid checkpoint.
        2. Re-read task records from the DB (ground truth).
        3. Find tasks that were RUNNING (uncertain state post-crash).
        4. Cross-check workspace file hashes if available.
        5. Return plan with completed/uncertain/tainted lists.
        """
        plan = RecoveryPlan()
        checkpoint = self.load_last_checkpoint(project_id)

        if checkpoint:
            plan.last_valid_checkpoint_id = checkpoint.checkpoint_id
            logger.info(
                "Recovery: loaded checkpoint %s (project=%s).",
                checkpoint.checkpoint_id, project_id,
            )
        else:
            logger.info("Recovery: no valid checkpoint found.")

        # Query actual task states from DB
        if project_id:
            with get_connection(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT task_id, status, error FROM tasks "
                    "WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
        else:
            rows = []

        for row in rows:
            status_str = row["status"]
            tid = row["task_id"]
            if status_str == TaskStatus.COMPLETED:
                plan.completed_task_ids.append(tid)
            elif status_str == TaskStatus.RUNNING:
                # Was running when DB was last written — uncertain
                plan.uncertain_task_ids.append(tid)
                plan.issues.append(
                    f"Task {tid!r} was RUNNING at last save — status unknown."
                )
            elif status_str == TaskStatus.FAILED:
                # Already marked failed — not a recovery concern but note it
                pass

        # Verify workspace hashes if checkpoint has them
        if checkpoint and checkpoint.workspace_hashes:
            from factory.audit.evidence import hash_file
            for path_str, expected_hash in checkpoint.workspace_hashes.items():
                path = Path(path_str)
                if not path.exists():
                    plan.issues.append(
                        f"Workspace file {path_str!r} expected but missing."
                    )
                    continue
                actual_hash = hash_file(path)
                if actual_hash != expected_hash:
                    plan.tainted_task_ids.extend(
                        tid for tid in plan.uncertain_task_ids
                        if checkpoint.task_id == tid
                    )
                    plan.issues.append(
                        f"Workspace file {path_str!r} hash mismatch — "
                        f"partial write suspected."
                    )

        plan.safe_to_continue = (
            len(plan.uncertain_task_ids) == 0
            and len(plan.tainted_task_ids) == 0
        )

        if plan.safe_to_continue:
            logger.info("Recovery: safe to continue from last checkpoint.")
        else:
            logger.warning(
                "Recovery: %d uncertain task(s), %d tainted task(s). "
                "Manual resolution may be required.",
                len(plan.uncertain_task_ids),
                len(plan.tainted_task_ids),
            )

        return plan
