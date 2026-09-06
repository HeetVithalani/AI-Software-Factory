"""
factory.memory.project_memory
==============================
Project-isolated memory store.

Each project has its own isolated key-value memory in the ``project_memory``
table. No cross-project leakage is possible at the query level — every read
and write is scoped to the project_id.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.database import get_connection

logger = logging.getLogger(__name__)


class ProjectMemory:
    """
    Per-project isolated memory store.

    Keys are strings; values are any JSON-serializable object.

    Usage::

        mem = ProjectMemory(project_id="abc123")
        mem.set("plan_summary", {"phases": 3})
        data = mem.get("plan_summary")
    """

    def __init__(
        self,
        project_id: str,
        db_path: Path | None = None,
    ) -> None:
        from factory.config import DB_PATH
        self.project_id = project_id
        self._db_path = db_path or DB_PATH

    def set(self, key: str, value: Any, metadata: dict[str, Any] | None = None) -> None:
        """Store or update a value for the given key."""
        now = datetime.now(timezone.utc).isoformat()
        mem_id = str(uuid.uuid4())
        value_json = json.dumps(value, default=str)
        meta_json = json.dumps(metadata or {}, default=str)

        with get_connection(self._db_path) as conn:
            # Upsert: update if exists, insert if not
            existing = conn.execute(
                "SELECT mem_id FROM project_memory WHERE project_id = ? AND key = ?",
                (self.project_id, key),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE project_memory
                    SET value = ?, updated_at = ?, metadata = ?
                    WHERE project_id = ? AND key = ?
                    """,
                    (value_json, now, meta_json, self.project_id, key),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO project_memory
                        (mem_id, project_id, key, value, created_at, updated_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mem_id, self.project_id, key, value_json, now, now, meta_json),
                )
            conn.commit()
        logger.debug("ProjectMemory[%s]: set %r", self.project_id, key)

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieve a value by key, or default if not found."""
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT value FROM project_memory WHERE project_id = ? AND key = ?",
                (self.project_id, key),
            ).fetchone()

        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        with get_connection(self._db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM project_memory WHERE project_id = ? AND key = ?",
                (self.project_id, key),
            )
            conn.commit()
        return (cursor.rowcount or 0) > 0

    def keys(self) -> list[str]:
        """Return all keys for this project."""
        with get_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT key FROM project_memory WHERE project_id = ? ORDER BY key",
                (self.project_id,),
            ).fetchall()
        return [r["key"] for r in rows]

    def all(self) -> dict[str, Any]:
        """Return all key-value pairs for this project."""
        with get_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT key, value FROM project_memory WHERE project_id = ?",
                (self.project_id,),
            ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                result[row["key"]] = row["value"]
        return result

    def clear(self) -> int:
        """Delete all memory for this project. Returns count deleted."""
        with get_connection(self._db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM project_memory WHERE project_id = ?",
                (self.project_id,),
            )
            conn.commit()
        return cursor.rowcount or 0
