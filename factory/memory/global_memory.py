"""
factory.memory.global_memory
==============================
Global knowledge store — sanitized, versioned, provenance-tracked.

Global knowledge:
- Must be sanitized before storage (no project secrets)
- Retains provenance (who, what, when, why)
- Supports versioning, deprecation, quarantine
- Supports confidence scoring backed by deterministic evidence
- Requires human approval for high-impact entries
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.database import get_connection
from factory.memory.provenance import ProvenanceRecord
from factory.memory.sanitization import KnowledgeSanitizer

logger = logging.getLogger(__name__)

_SANITIZER = KnowledgeSanitizer()


class GlobalMemory:
    """
    Cross-project global knowledge store.

    Entries are stored in the ``knowledge_entries`` table with:
    - unique (category, key) composite key
    - provenance JSON
    - evidence hash (SHA-256)
    - version counter
    - status: active | deprecated | quarantined

    Usage::

        gm = GlobalMemory()
        gm.store(
            category="patterns",
            key="use_type_hints",
            value="Always use type hints in Python >= 3.10",
            provenance=ProvenanceRecord(source_type="human", confidence_basis="style guide"),
        )
    """

    # Status values
    STATUS_ACTIVE = "active"
    STATUS_DEPRECATED = "deprecated"
    STATUS_QUARANTINED = "quarantined"

    def __init__(self, db_path: Path | None = None) -> None:
        from factory.config import DB_PATH
        self._db_path = db_path or DB_PATH

    def store(
        self,
        category: str,
        key: str,
        value: Any,
        *,
        provenance: ProvenanceRecord | None = None,
        confidence: float = 0.5,
        evidence_hash: str = "",
        project_id: str | None = None,
        skip_sanitization: bool = False,
    ) -> str:
        """
        Store or update a global knowledge entry.

        Sanitization is run before storage. Returns the entry_id.
        Raises ValueError if sanitization fails.
        """
        # Sanitize
        if not skip_sanitization:
            result = _SANITIZER.sanitize(key, value, project_id=project_id)
            if not result.is_clean:
                raise ValueError(
                    f"Knowledge entry {key!r} failed sanitization: "
                    + "; ".join(result.violations)
                )

        prov_dict = provenance.to_dict() if provenance else {}
        now = datetime.now(timezone.utc).isoformat()
        value_json = json.dumps(value, default=str)
        entry_id = str(uuid.uuid4())

        with get_connection(self._db_path) as conn:
            existing = conn.execute(
                "SELECT entry_id, version FROM knowledge_entries "
                "WHERE category = ? AND key = ?",
                (category, key),
            ).fetchone()

            if existing:
                new_version = (existing["version"] or 1) + 1
                conn.execute(
                    """
                    UPDATE knowledge_entries
                    SET value=?, provenance=?, confidence=?, evidence_hash=?,
                        version=?, status=?, updated_at=?
                    WHERE category=? AND key=?
                    """,
                    (
                        value_json,
                        json.dumps(prov_dict),
                        confidence,
                        evidence_hash,
                        new_version,
                        self.STATUS_ACTIVE,
                        now,
                        category,
                        key,
                    ),
                )
                entry_id = existing["entry_id"]
            else:
                conn.execute(
                    """
                    INSERT INTO knowledge_entries
                        (entry_id, category, key, value, provenance, confidence,
                         evidence_hash, version, status, created_at, updated_at,
                         quarantine_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id, category, key, value_json,
                        json.dumps(prov_dict), confidence,
                        evidence_hash, 1, self.STATUS_ACTIVE,
                        now, now, "",
                    ),
                )
            conn.commit()

        logger.info("GlobalMemory: stored %s/%s (v%s).", category, key,
                    "new" if not existing else "updated")
        return entry_id

    def get(self, category: str, key: str) -> Any | None:
        """Retrieve a value. Returns None if missing or quarantined."""
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT value, status FROM knowledge_entries "
                "WHERE category = ? AND key = ? AND status = ?",
                (category, key, self.STATUS_ACTIVE),
            ).fetchone()

        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def quarantine(self, category: str, key: str, reason: str) -> None:
        """Move an entry to quarantine status."""
        now = datetime.now(timezone.utc).isoformat()
        with get_connection(self._db_path) as conn:
            conn.execute(
                """
                UPDATE knowledge_entries
                SET status = ?, quarantine_reason = ?, updated_at = ?
                WHERE category = ? AND key = ?
                """,
                (self.STATUS_QUARANTINED, reason, now, category, key),
            )
            conn.commit()
        logger.warning(
            "GlobalMemory: quarantined %s/%s — %s", category, key, reason
        )

    def deprecate(self, category: str, key: str) -> None:
        """Deprecate an entry (no longer preferred but not quarantined)."""
        now = datetime.now(timezone.utc).isoformat()
        with get_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE knowledge_entries SET status=?, updated_at=? "
                "WHERE category=? AND key=?",
                (self.STATUS_DEPRECATED, now, category, key),
            )
            conn.commit()
        logger.info("GlobalMemory: deprecated %s/%s.", category, key)

    def list_entries(
        self,
        category: str | None = None,
        *,
        status: str = "active",
    ) -> list[dict[str, Any]]:
        """List knowledge entries, optionally filtered by category and status."""
        conditions = ["status = ?"]
        params: list[Any] = [status]
        if category:
            conditions.append("category = ?")
            params.append(category)

        where = "WHERE " + " AND ".join(conditions)
        sql = f"SELECT * FROM knowledge_entries {where} ORDER BY category, key"

        with get_connection(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()

        result = []
        for row in rows:
            entry = dict(row)
            for field in ("value", "provenance"):
                try:
                    entry[field] = json.loads(entry.get(field, "null") or "null")
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(entry)
        return result
