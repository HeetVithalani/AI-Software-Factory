"""
factory.audit.evidence
======================
Deterministic evidence collection for the audit trail.

Evidence records attach cryptographic hashes of files/outputs to audit
events, providing verifiable proof that an agent actually produced or
inspected a specific artifact. AI confidence scores are NOT used as
evidence — only deterministic content hashes count.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from factory.database import get_connection

logger = logging.getLogger(__name__)


class EvidenceRecord(BaseModel):
    """A piece of deterministic evidence attached to an audit event."""
    evidence_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    # kind: "file", "command_output", "test_result", "scan_result"
    kind: str = "file"
    path: str = ""
    hash_sha256: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents."""
    sha = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()
    except OSError as exc:
        logger.warning("Cannot hash file %s: %s", path, exc)
        return ""


def hash_string(content: str) -> str:
    """Return the SHA-256 hex digest of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def attach_evidence(
    record: EvidenceRecord,
    db_path: Path | None = None,
) -> str:
    """
    Persist an EvidenceRecord to the database.

    Returns the evidence_id.
    """
    from factory.config import DB_PATH as _DB_PATH
    path = db_path or _DB_PATH

    with get_connection(path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO evidence (
                evidence_id, project_id, task_id, agent_id,
                kind, path, hash_sha256, metadata, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.evidence_id,
                record.project_id,
                record.task_id,
                record.agent_id,
                record.kind,
                record.path,
                record.hash_sha256,
                json.dumps(record.metadata),
                record.recorded_at.isoformat(),
            ),
        )
        conn.commit()

    logger.debug(
        "Evidence %s attached (kind=%s, path=%s, hash=%s…)",
        record.evidence_id, record.kind, record.path,
        record.hash_sha256[:12] if record.hash_sha256 else "—",
    )
    return record.evidence_id


def attach_file_evidence(
    file_path: Path,
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> EvidenceRecord:
    """
    Convenience: hash a file and attach it as evidence in one call.

    Returns the created EvidenceRecord.
    """
    record = EvidenceRecord(
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        kind="file",
        path=str(file_path),
        hash_sha256=hash_file(file_path),
        metadata=metadata or {},
    )
    attach_evidence(record, db_path)
    return record


def get_evidence_for_task(
    task_id: str,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return all evidence records for a given task_id."""
    from factory.config import DB_PATH as _DB_PATH
    _path = db_path or _DB_PATH
    with get_connection(_path) as conn:
        rows = conn.execute(
            "SELECT * FROM evidence WHERE task_id = ? ORDER BY recorded_at",
            (task_id,),
        ).fetchall()
    result = []
    for row in rows:
        entry = dict(row)
        try:
            entry["metadata"] = json.loads(entry.get("metadata", "{}"))
        except (json.JSONDecodeError, TypeError):
            pass
        result.append(entry)
    return result
