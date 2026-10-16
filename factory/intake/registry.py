"""
factory.intake.registry
========================
Persistence layer for the tool-intake tables (schema v4).

All functions take an explicit ``db_path`` so tests can run against a
temporary database. Rows map 1:1 to the ``ToolRecord`` / ``ToolRevision``
models; JSON columns are (de)serialized here.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.database import get_connection, to_json
from factory.errors import IntakeStateError
from factory.models import (
    IntakeStatus,
    ScanState,
    ToolKind,
    ToolRecord,
    ToolRevision,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_value(status: IntakeStatus | str) -> str:
    """Normalize an IntakeStatus (or its plain-str form) to the DB value."""
    if isinstance(status, IntakeStatus):
        return status.value
    try:
        return IntakeStatus(status).value
    except ValueError:
        return str(status)


def assert_revocation_terminal(
    tool_id: str,
    current_status: IntakeStatus | str,
    new_status: IntakeStatus | str,
    *,
    operation: str = "status transition",
) -> None:
    """
    Central state-transition guard: REVOKED is terminal.

    No ordinary lifecycle operation may move a REVOKED tool to any other
    status. Reactivation is intentionally impossible here — it must be a
    separate, explicitly designed, auditable command requiring its own
    human decision (that command does not exist in this release).
    """
    current = _status_value(current_status)
    new = _status_value(new_status)
    if current == IntakeStatus.REVOKED.value and new != IntakeStatus.REVOKED.value:
        raise IntakeStateError(
            f"Tool {tool_id!r} is REVOKED (terminal kill-switch); {operation} "
            f"from 'revoked' to {new!r} is permanently rejected. Reactivation "
            "requires a separate auditable command (not available in this "
            "release)."
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# tool_registry
# ---------------------------------------------------------------------------

def upsert_tool(record: ToolRecord, db_path: Path | None = None) -> None:
    """Insert or update one logical tool record.

    Revocation guard: a REVOKED tool is never mutated here — neither its
    status nor its name/license/capabilities may be silently rewritten by
    rediscovery. Reactivation must go through a separate auditable command.
    """
    with get_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT status FROM tool_registry WHERE tool_id = ?",
            (record.tool_id,),
        ).fetchone()
        if existing is not None and _status_value(existing["status"]) == (
            IntakeStatus.REVOKED.value
        ):
            # Central revocation guard: a REVOKED tool is immutable — even a
            # REVOKED -> REVOKED upsert would silently rewrite name, license,
            # capabilities, or source in the conflict branch below.
            raise IntakeStateError(
                f"Tool {record.tool_id!r} is REVOKED (terminal kill-switch); "
                "upsert/rediscovery cannot modify it. Reactivation requires a "
                "separate auditable command (not available in this release)."
            )
        conn.execute(
            """
            INSERT INTO tool_registry (
                tool_id, kind, name, source_url, license_declared,
                license_source, capabilities, broker_categories,
                status, install_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tool_id) DO UPDATE SET
                name = excluded.name,
                license_declared = COALESCE(
                    excluded.license_declared, tool_registry.license_declared),
                license_source = CASE
                    WHEN excluded.license_declared IS NOT NULL
                    THEN excluded.license_source
                    ELSE tool_registry.license_source END,
                capabilities = excluded.capabilities,
                broker_categories = excluded.broker_categories,
                status = excluded.status,
                install_path = COALESCE(
                    excluded.install_path, tool_registry.install_path),
                updated_at = excluded.updated_at
            """,
            (
                record.tool_id,
                record.kind.value if isinstance(record.kind, ToolKind) else str(record.kind),
                record.name,
                record.source_url,
                record.license_declared,
                record.license_source,
                to_json(record.capabilities),
                to_json(record.broker_categories),
                record.status.value if isinstance(record.status, IntakeStatus) else str(record.status),
                record.install_path,
                record.created_at.isoformat(),
                _now_iso(),
            ),
        )
        conn.commit()


def get_tool(tool_id: str, db_path: Path | None = None) -> ToolRecord | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tool_registry WHERE tool_id = ?", (tool_id,)
        ).fetchone()
    if row is None:
        return None
    return ToolRecord(
        tool_id=row["tool_id"],
        kind=ToolKind(row["kind"]),
        name=row["name"],
        source_url=row["source_url"],
        license_declared=row["license_declared"],
        license_source=row["license_source"],
        capabilities=json.loads(row["capabilities"] or "[]"),
        broker_categories=json.loads(row["broker_categories"] or "[]"),
        status=IntakeStatus(row["status"]),
        install_path=row["install_path"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def list_tools(db_path: Path | None = None) -> list[ToolRecord]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT tool_id FROM tool_registry ORDER BY created_at"
        ).fetchall()
    tools: list[ToolRecord] = []
    for row in rows:
        rec = get_tool(row["tool_id"], db_path)
        if rec is not None:
            tools.append(rec)
    return tools


def set_tool_status(
    tool_id: str,
    status: IntakeStatus,
    db_path: Path | None = None,
    install_path: str | None = None,
) -> None:
    """Persist a lifecycle status change with central transition validation.

    REVOKED is terminal: this choke point refuses any transition that would
    move a revoked tool to another status, so no ordinary command path
    (scan/check/fetch/approve/install/disable/rediscovery) can un-revoke.
    """
    with get_connection(db_path) as conn:
        current = conn.execute(
            "SELECT status FROM tool_registry WHERE tool_id = ?",
            (tool_id,),
        ).fetchone()
        if current is not None:
            assert_revocation_terminal(
                tool_id, current["status"], status, operation="status transition"
            )
        conn.execute(
            """
            UPDATE tool_registry
               SET status = ?, install_path = COALESCE(?, install_path),
                   updated_at = ?
             WHERE tool_id = ?
            """,
            (
                status.value if isinstance(status, IntakeStatus) else str(status),
                install_path,
                _now_iso(),
                tool_id,
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# tool_revisions
# ---------------------------------------------------------------------------

def _assert_parent_tool_not_revoked(
    conn: Any, tool_id: str, operation: str
) -> None:
    """Reject revision writes whose parent tool is REVOKED.

    Runs on the caller's own connection so the status check and the
    revision write share one transaction boundary — no window in which
    the tool could be revoked between check and write. A missing parent
    row preserves the historical (non-FK-checked) contract; only an
    existing REVOKED parent blocks.
    """
    row = conn.execute(
        "SELECT status FROM tool_registry WHERE tool_id = ?", (tool_id,)
    ).fetchone()
    if row is not None and _status_value(row["status"]) == (
        IntakeStatus.REVOKED.value
    ):
        raise IntakeStateError(
            f"Tool {tool_id!r} is REVOKED (terminal kill-switch); {operation} "
            "is permanently rejected. Reactivation requires a separate "
            "auditable command (not available in this release)."
        )


def insert_revision(rev: ToolRevision, db_path: Path | None = None) -> None:
    """Insert a new revision row. Fails if (tool_id, revision_sha) exists."""
    with get_connection(db_path) as conn:
        _assert_parent_tool_not_revoked(
            conn, rev.tool_id, "inserting a new revision"
        )
        try:
            conn.execute(
                """
                INSERT INTO tool_revisions (
                    revision_id, tool_id, revision_sha, artifact_sha256,
                    tree_sha256, quarantine_path, scan_state, scan_report_path,
                    scan_report_sha256, scanner_identity, risk_score, severity,
                    recommendation, unresolved_findings, policy_decision,
                    policy_reasons, approval_request_id, approved_by, approved_at,
                    approval_artifact_sha256, approval_report_sha256,
                    installed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _revision_params(rev),
            )
        except Exception as exc:
            conn.rollback()
            raise IntakeStateError(
                f"Revision {rev.tool_id}@{rev.revision_sha} already exists "
                f"or cannot be inserted: {exc}"
            ) from exc
        conn.commit()


def _revision_params(rev: ToolRevision) -> tuple[Any, ...]:
    scan_state = (
        rev.scan_state.value
        if isinstance(rev.scan_state, ScanState)
        else str(rev.scan_state)
    )
    return (
        rev.revision_id,
        rev.tool_id,
        rev.revision_sha,
        rev.artifact_sha256,
        rev.tree_sha256,
        rev.quarantine_path,
        scan_state,
        rev.scan_report_path,
        rev.scan_report_sha256,
        rev.scanner_identity,
        rev.risk_score,
        rev.severity,
        rev.recommendation,
        rev.unresolved_findings,
        rev.policy_decision,
        to_json(rev.policy_reasons),
        rev.approval_request_id,
        rev.approved_by,
        rev.approved_at.isoformat() if rev.approved_at else None,
        rev.approval_artifact_sha256,
        rev.approval_report_sha256,
        rev.installed_at.isoformat() if rev.installed_at else None,
        rev.created_at.isoformat(),
        _now_iso(),
    )


def save_revision(rev: ToolRevision, db_path: Path | None = None) -> None:
    """Persist the full state of an existing revision row.

    Central revocation guard: revision rows of a REVOKED tool are
    immutable through this API too — even a nominally unchanged save
    rewrites ``updated_at``, so every write is rejected for a revoked
    parent (H1 follow-up).
    """
    params = _revision_params(rev)
    with get_connection(db_path) as conn:
        _assert_parent_tool_not_revoked(
            conn, rev.tool_id, "updating a revision"
        )
        cursor = conn.execute(
            """
            UPDATE tool_revisions SET
                artifact_sha256 = ?, tree_sha256 = ?, quarantine_path = ?,
                scan_state = ?, scan_report_path = ?, scan_report_sha256 = ?,
                scanner_identity = ?, risk_score = ?, severity = ?,
                recommendation = ?, unresolved_findings = ?, policy_decision = ?,
                policy_reasons = ?, approval_request_id = ?, approved_by = ?,
                approved_at = ?, approval_artifact_sha256 = ?,
                approval_report_sha256 = ?, installed_at = ?, updated_at = ?
             WHERE revision_id = ?
            """,
            (
                params[3], params[4], params[5], params[6], params[7],
                params[8], params[9], params[10], params[11], params[12],
                params[13], params[14], params[15], params[16], params[17],
                params[18], params[19], params[20], params[21], params[23],
                rev.revision_id,
            ),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise IntakeStateError(
                f"Cannot save unknown revision {rev.tool_id}@{rev.revision_sha}."
            )


def get_revision(
    tool_id: str, revision_sha: str, db_path: Path | None = None
) -> ToolRevision | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tool_revisions WHERE tool_id = ? AND revision_sha = ?",
            (tool_id, revision_sha),
        ).fetchone()
    if row is None:
        return None
    return _row_to_revision(row)


def list_revisions(tool_id: str, db_path: Path | None = None) -> list[ToolRevision]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tool_revisions WHERE tool_id = ? ORDER BY created_at",
            (tool_id,),
        ).fetchall()
    return [_row_to_revision(r) for r in rows]


def _row_to_revision(row: Any) -> ToolRevision:
    return ToolRevision(
        revision_id=row["revision_id"],
        tool_id=row["tool_id"],
        revision_sha=row["revision_sha"],
        artifact_sha256=row["artifact_sha256"],
        tree_sha256=row["tree_sha256"],
        quarantine_path=row["quarantine_path"],
        scan_state=ScanState(row["scan_state"]),
        scan_report_path=row["scan_report_path"],
        scan_report_sha256=row["scan_report_sha256"],
        scanner_identity=row["scanner_identity"],
        risk_score=row["risk_score"],
        severity=row["severity"],
        recommendation=row["recommendation"],
        unresolved_findings=row["unresolved_findings"],
        policy_decision=row["policy_decision"],
        policy_reasons=json.loads(row["policy_reasons"] or "[]"),
        approval_request_id=row["approval_request_id"],
        approved_by=row["approved_by"],
        approved_at=_parse_dt(row["approved_at"]),
        approval_artifact_sha256=row["approval_artifact_sha256"],
        approval_report_sha256=row["approval_report_sha256"],
        installed_at=_parse_dt(row["installed_at"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )
