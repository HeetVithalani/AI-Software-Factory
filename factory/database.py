"""
factory.database
================
SQLite persistence layer for the AI Software Factory.

All tables are created here. Schema migrations are version-tracked via
the ``schema_version`` table. The ``get_connection()`` function returns
a new connection with WAL mode and foreign keys enabled.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from factory.config import DB_PATH

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 4


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """
    Return a new SQLite connection with:
    - Row factory (access columns by name)
    - WAL journal mode (safe concurrent reads)
    - Foreign keys enforced
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version recorded in the DB (0 if unset)."""
    try:
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row["version"]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _create_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Create the original events table (v1 schema)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            event_type  TEXT    NOT NULL,
            project_id  TEXT,
            task_id     TEXT,
            agent_id    TEXT,
            payload     TEXT    NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)"
    )


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add projects, tasks, approvals, checkpoints, knowledge_entries tables."""

    # Projects
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id      TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'intake',
            quality_profile TEXT NOT NULL DEFAULT 'balanced',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            workspace_path  TEXT NOT NULL DEFAULT '',
            metadata        TEXT NOT NULL DEFAULT '{}'
        )
    """)

    # Tasks
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id           TEXT PRIMARY KEY,
            project_id        TEXT NOT NULL,
            name              TEXT NOT NULL,
            description       TEXT NOT NULL DEFAULT '',
            agent_type        TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'pending',
            risk_level        TEXT NOT NULL DEFAULT 'safe',
            depends_on        TEXT NOT NULL DEFAULT '[]',
            created_at        TEXT NOT NULL,
            started_at        TEXT,
            completed_at      TEXT,
            error             TEXT,
            retry_count       INTEGER NOT NULL DEFAULT 0,
            max_retries       INTEGER NOT NULL DEFAULT 3,
            recovery_strategy TEXT NOT NULL DEFAULT 'retry_same',
            result_summary    TEXT NOT NULL DEFAULT '',
            metadata          TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
    )

    # Approvals
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            request_id          TEXT PRIMARY KEY,
            project_id          TEXT,
            task_id             TEXT,
            agent_id            TEXT,
            action              TEXT NOT NULL,
            target              TEXT NOT NULL,
            reason              TEXT NOT NULL,
            expected_effect     TEXT NOT NULL DEFAULT '',
            risk_level          TEXT NOT NULL,
            consequence_if_denied TEXT NOT NULL DEFAULT '',
            action_category     TEXT NOT NULL DEFAULT '',
            scope_key           TEXT NOT NULL DEFAULT '',
            requested_at        TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending',
            decided_at          TEXT,
            decided_by          TEXT NOT NULL DEFAULT 'human',
            decision_note       TEXT NOT NULL DEFAULT '',
            grants_scope_inheritance INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approvals_scope ON approvals(scope_key)"
    )

    # Checkpoints
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id       TEXT PRIMARY KEY,
            project_id          TEXT,
            task_id             TEXT,
            created_at          TEXT NOT NULL,
            orchestrator_state  TEXT NOT NULL DEFAULT '{}',
            workspace_hashes    TEXT NOT NULL DEFAULT '{}',
            is_valid            INTEGER NOT NULL DEFAULT 1,
            invalidated_reason  TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_checkpoints_project "
        "ON checkpoints(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_checkpoints_valid "
        "ON checkpoints(is_valid)"
    )

    # Knowledge entries (for global memory)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_entries (
            entry_id        TEXT PRIMARY KEY,
            category        TEXT NOT NULL DEFAULT '',
            key             TEXT NOT NULL,
            value           TEXT NOT NULL,
            provenance      TEXT NOT NULL DEFAULT '{}',
            confidence      REAL NOT NULL DEFAULT 0.0,
            evidence_hash   TEXT NOT NULL DEFAULT '',
            version         INTEGER NOT NULL DEFAULT 1,
            status          TEXT NOT NULL DEFAULT 'active',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            quarantine_reason TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_key "
        "ON knowledge_entries(category, key)"
    )

    # Project memory (isolated per project)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_memory (
            mem_id      TEXT PRIMARY KEY,
            project_id  TEXT NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            metadata    TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_projmem_key "
        "ON project_memory(project_id, key)"
    )

    # Evidence records
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id TEXT PRIMARY KEY,
            project_id  TEXT,
            task_id     TEXT,
            agent_id    TEXT,
            kind        TEXT NOT NULL DEFAULT 'file',
            path        TEXT NOT NULL DEFAULT '',
            hash_sha256 TEXT NOT NULL DEFAULT '',
            metadata    TEXT NOT NULL DEFAULT '{}',
            recorded_at TEXT NOT NULL
        )
    """)


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Add llm_calls and context_manifests tables for Phase 2."""
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_calls (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            provider            TEXT NOT NULL,
            model               TEXT NOT NULL,
            request_id          TEXT NOT NULL,
            task_id             TEXT,
            start_time          TEXT NOT NULL,
            end_time            TEXT NOT NULL,
            duration_ms         REAL NOT NULL,
            success             INTEGER NOT NULL DEFAULT 1,
            tokens_in           INTEGER NOT NULL DEFAULT 0,
            tokens_out          INTEGER NOT NULL DEFAULT 0,
            retry_count         INTEGER NOT NULL DEFAULT 0,
            raw_response        TEXT NOT NULL DEFAULT '',
            error               TEXT NOT NULL DEFAULT '',
            structured_output   TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_calls_task ON llm_calls(task_id)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS context_manifests (
            manifest_id         TEXT PRIMARY KEY,
            task_id             TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            system_prompt       TEXT NOT NULL DEFAULT '',
            messages            TEXT NOT NULL DEFAULT '[]',
            included_files      TEXT NOT NULL DEFAULT '[]',
            token_estimate      INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_context_manifests_task ON context_manifests(task_id)"
    )


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """
    Add tool-intake tables (Phase 2a): logical tool identity + immutable,
    revision-pinned records with hash-bound approval tracking.

    Approval is never stored as a bare boolean: the revision row records the
    approval request reference AND the exact artifact / scan-report hashes the
    approval was granted against, so any byte change invalidates it.
    """

    # Logical tool identity (kind + canonical source URL => stable tool_id)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_registry (
            tool_id             TEXT PRIMARY KEY,
            kind                TEXT NOT NULL,
            name                TEXT NOT NULL,
            source_url          TEXT NOT NULL,
            license_declared    TEXT,
            license_source      TEXT NOT NULL DEFAULT 'unknown',
            capabilities        TEXT NOT NULL DEFAULT '[]',
            broker_categories   TEXT NOT NULL DEFAULT '[]',
            status              TEXT NOT NULL DEFAULT 'discovered',
            install_path        TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_registry_status "
        "ON tool_registry(status)"
    )

    # Immutable revision records (exact pinned SHA + content hashes)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_revisions (
            revision_id             TEXT PRIMARY KEY,
            tool_id                 TEXT NOT NULL
                REFERENCES tool_registry(tool_id) ON DELETE CASCADE,
            revision_sha            TEXT NOT NULL,
            artifact_sha256         TEXT,
            tree_sha256             TEXT,
            quarantine_path         TEXT,
            scan_state              TEXT NOT NULL DEFAULT 'not_run',
            scan_report_path        TEXT,
            scan_report_sha256      TEXT,
            scanner_identity        TEXT,
            risk_score              INTEGER,
            severity                TEXT,
            recommendation          TEXT,
            unresolved_findings     INTEGER NOT NULL DEFAULT 0,
            policy_decision         TEXT,
            policy_reasons          TEXT NOT NULL DEFAULT '[]',
            approval_request_id     TEXT
                REFERENCES approvals(request_id) ON DELETE SET NULL,
            approved_by             TEXT,
            approved_at             TEXT,
            approval_artifact_sha256 TEXT,
            approval_report_sha256  TEXT,
            installed_at            TEXT,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL,
            UNIQUE (tool_id, revision_sha)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_revisions_tool "
        "ON tool_revisions(tool_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_revisions_sha "
        "ON tool_revisions(revision_sha)"
    )


def initialize_database(db_path: Path | None = None) -> None:
    """
    Create or migrate the factory database to the current schema version.
    Safe to call multiple times (idempotent).
    """
    path = db_path or DB_PATH
    with get_connection(path) as conn:
        _create_schema_version_table(conn)
        current = _get_schema_version(conn)
        logger.debug(
            "Database at %s: schema_version=%d, target=%d",
            path, current, CURRENT_SCHEMA_VERSION,
        )

        if current < 1:
            logger.info("Applying schema migration v1 (events table)…")
            _migrate_v1(conn)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (1,)
            )

        if current < 2:
            logger.info("Applying schema migration v2 (projects/tasks/…)…")
            _migrate_v2(conn)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (2,)
            )

        if current < 3:
            logger.info("Applying schema migration v3 (llm_calls, context_manifests)…")
            _migrate_v3(conn)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (3,)
            )

        if current < 4:
            logger.info("Applying schema migration v4 (tool_registry, tool_revisions)…")
            _migrate_v4(conn)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (4,)
            )

        conn.commit()
        logger.debug("Database initialised at schema v%d.", CURRENT_SCHEMA_VERSION)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def fetch_one(
    sql: str,
    params: tuple = (),
    db_path: Path | None = None,
) -> sqlite3.Row | None:
    with get_connection(db_path) as conn:
        return conn.execute(sql, params).fetchone()


def fetch_all(
    sql: str,
    params: tuple = (),
    db_path: Path | None = None,
) -> list[sqlite3.Row]:
    with get_connection(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def execute_write(
    sql: str,
    params: tuple = (),
    db_path: Path | None = None,
) -> int:
    """Execute a write statement and return lastrowid."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.lastrowid or 0


def to_json(obj: object) -> str:
    """Serialize a Python object to a compact JSON string."""
    return json.dumps(obj, default=str, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Compatibility alias used by tests and early code
# ---------------------------------------------------------------------------

#: Alias for :func:`initialize_database` — both names are supported.
init_db = initialize_database


if __name__ == "__main__":
    initialize_database()
    print(f"Database initialized: {DB_PATH}")
