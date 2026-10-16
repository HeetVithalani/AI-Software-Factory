"""
Direct registry-level regression tests for the REVOKED kill-switch on
revision writes (H1 follow-up hardening).

Revision rows of a REVOKED tool are immutable through the persistence
layer itself — not only through service-level guards: neither
``save_revision()`` (update) nor ``insert_revision()`` (new revision) may
write revision metadata for a revoked parent tool, and even a nominally
unchanged save is rejected because it would rewrite ``updated_at``.

All databases live under pytest's tmp_path; no network, no scanner, no
Ollama, no machine-specific paths.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from factory.database import initialize_database
from factory.errors import IntakeStateError
from factory.intake import registry
from factory.models import (
    IntakeStatus,
    ScanState,
    ToolKind,
    ToolRecord,
    ToolRevision,
)

SHA1 = "1" * 40
SHA2 = "2" * 40
TOOL_ID = "tool_0123456789abcdef"


def _tool() -> ToolRecord:
    now = datetime.now(timezone.utc)
    return ToolRecord(
        tool_id=TOOL_ID,
        kind=ToolKind.AGENT_SKILL,
        name="demo-skill",
        source_url="https://github.com/demo-org/demo-skill",
        status=IntakeStatus.SCANNED,
        created_at=now,
        updated_at=now,
    )


def _rev(sha: str = SHA1) -> ToolRevision:
    now = datetime.now(timezone.utc)
    return ToolRevision(
        tool_id=TOOL_ID,
        revision_sha=sha,
        artifact_sha256="a" * 64,
        tree_sha256="b" * 64,
        scan_state=ScanState.CLEAN,
        scanner_identity="skillspector(static,no-llm),version=2.12.0",
        risk_score=0,
        severity="LOW",
        recommendation="SAFE",
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "factory.db"
    initialize_database(path)
    return path


class TestRevisionWritesRevokedParent:
    def test_save_revision_rejected_and_row_unchanged(self, db: Path) -> None:
        registry.upsert_tool(_tool(), db)
        registry.insert_revision(_rev(), db)
        registry.set_tool_status(TOOL_ID, IntakeStatus.REVOKED, db)

        # Before-snapshot of every persisted field.
        snapshot = registry.get_revision(TOOL_ID, SHA1, db)
        assert snapshot is not None
        fields_before = snapshot.model_dump()

        # Attempt to rewrite status/evidence/approval/updated-at through
        # the direct registry API.
        snapshot.scan_state = ScanState.FAILED
        snapshot.scan_report_path = "hijacked/report.json"
        snapshot.scan_report_sha256 = "c" * 64
        snapshot.approved_by = "attacker"
        snapshot.policy_decision = "pass"
        snapshot.updated_at = datetime.now(timezone.utc)

        with pytest.raises(IntakeStateError, match="REVOKED"):
            registry.save_revision(snapshot, db)

        # The stored row is byte-for-byte (value-for-value) unchanged.
        after = registry.get_revision(TOOL_ID, SHA1, db)
        assert after is not None
        assert after.model_dump() == fields_before

        # Even a nominally unchanged save is rejected — it would still
        # rewrite updated_at.
        pristine = registry.get_revision(TOOL_ID, SHA1, db)
        with pytest.raises(IntakeStateError, match="REVOKED"):
            registry.save_revision(pristine, db)
        assert registry.get_revision(TOOL_ID, SHA1, db).model_dump() == (
            fields_before
        )

    def test_insert_revision_rejected_for_revoked_parent(self, db: Path) -> None:
        registry.upsert_tool(_tool(), db)
        registry.insert_revision(_rev(), db)
        registry.set_tool_status(TOOL_ID, IntakeStatus.REVOKED, db)

        with pytest.raises(IntakeStateError, match="REVOKED"):
            registry.insert_revision(_rev(SHA2), db)

        # No second revision row appeared.
        assert registry.get_revision(TOOL_ID, SHA2, db) is None
        assert len(registry.list_revisions(TOOL_ID, db)) == 1
        assert registry.get_tool(TOOL_ID, db).status == IntakeStatus.REVOKED

    def test_non_revoked_tool_saves_and_inserts_normally(self, db: Path) -> None:
        registry.upsert_tool(_tool(), db)
        registry.insert_revision(_rev(), db)

        rev = registry.get_revision(TOOL_ID, SHA1, db)
        rev.risk_score = 7
        rev.severity = "MEDIUM"
        registry.save_revision(rev, db)

        saved = registry.get_revision(TOOL_ID, SHA1, db)
        assert saved.risk_score == 7
        assert saved.severity == "MEDIUM"

        registry.insert_revision(_rev(SHA2), db)
        assert len(registry.list_revisions(TOOL_ID, db)) == 2
