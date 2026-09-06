"""
factory.audit.logger
====================
Structured audit event persistence.

Every factory action that has any significance is recorded here. Records
are immutable once written — the audit log is append-only by convention.

``record_event()`` is the low-level function (kept for backwards compat).
``AuditLogger`` is the preferred interface — it binds project/task/agent
context so callers don't have to pass it on every call.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.database import get_connection
from factory.events import FactoryEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level record_event (kept from original implementation)
# ---------------------------------------------------------------------------

def record_event(event: FactoryEvent, db_path: Path | None = None) -> int:
    """
    Persist a FactoryEvent to the events table.

    Returns the new row's integer ID.
    """
    from factory.config import DB_PATH as _DB_PATH
    path = db_path or _DB_PATH
    with get_connection(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO events (
                timestamp, event_type, project_id, task_id, agent_id, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.timestamp.isoformat(),
                event.event_type,
                event.project_id,
                event.task_id,
                event.agent_id,
                json.dumps(event.payload),
            ),
        )
        conn.commit()
        row_id = int(cursor.lastrowid)  # type: ignore[arg-type]
    logger.debug("Recorded event %s (id=%d)", event.event_type, row_id)
    return row_id


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def list_events(
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return events filtered by optional project/task/type, newest first."""
    from factory.config import DB_PATH as _DB_PATH
    path = db_path or _DB_PATH

    conditions: list[str] = []
    params: list[Any] = []

    if project_id is not None:
        conditions.append("project_id = ?")
        params.append(project_id)
    if task_id is not None:
        conditions.append("task_id = ?")
        params.append(task_id)
    if event_type is not None:
        conditions.append("event_type = ?")
        params.append(event_type)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with get_connection(path) as conn:
        rows = conn.execute(sql, params).fetchall()

    result = []
    for row in rows:
        entry = dict(row)
        try:
            entry["payload"] = json.loads(entry.get("payload", "{}"))
        except (json.JSONDecodeError, TypeError):
            pass
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# AuditLogger — bound context
# ---------------------------------------------------------------------------

class AuditLogger:
    """
    Context-bound audit logger.

    Bind project/task/agent once; emit events without repeating context.

    Usage::

        audit = AuditLogger(project_id="abc", agent_id="planner")
        audit.info("task.started", {"task_name": "plan_phase"})
        audit.error("task.failed", {"reason": "model timeout"})
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        db_path: Path | None = None,
    ) -> None:
        self.project_id = project_id
        self.task_id = task_id
        self.agent_id = agent_id
        self._db_path = db_path

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> int:
        """
        Emit an audit event.

        ``task_id`` overrides the bound task_id for this call only.
        """
        event = FactoryEvent(
            event_type=event_type,
            project_id=self.project_id,
            task_id=task_id or self.task_id,
            agent_id=self.agent_id,
            payload=payload or {},
        )
        return record_event(event, self._db_path)

    # Convenience shorthands
    def info(self, event_type: str, payload: dict[str, Any] | None = None) -> int:
        return self.emit(event_type, payload)

    def error(self, event_type: str, payload: dict[str, Any] | None = None) -> int:
        p = dict(payload or {})
        p.setdefault("level", "error")
        return self.emit(event_type, p)

    def approval(
        self, event_type: str, request_id: str, payload: dict[str, Any] | None = None
    ) -> int:
        p = dict(payload or {})
        p["request_id"] = request_id
        p.setdefault("level", "approval")
        return self.emit(event_type, p)

    def with_task(self, task_id: str) -> "AuditLogger":
        """Return a new AuditLogger with the given task_id bound."""
        return AuditLogger(
            project_id=self.project_id,
            task_id=task_id,
            agent_id=self.agent_id,
            db_path=self._db_path,
        )

    def with_agent(self, agent_id: str) -> "AuditLogger":
        """Return a new AuditLogger with the given agent_id bound."""
        return AuditLogger(
            project_id=self.project_id,
            task_id=self.task_id,
            agent_id=agent_id,
            db_path=self._db_path,
        )
