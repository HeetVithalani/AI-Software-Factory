"""
Unit tests for the schema v4 migration — fresh-database initialization,
v3 → v4 upgrade with data preservation, idempotency, and table/index shape.

All databases live in pytest temp dirs; the repository's factory.db is
never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from factory.database import (
    CURRENT_SCHEMA_VERSION,
    _create_schema_version_table,
    _migrate_v1,
    _migrate_v2,
    _migrate_v3,
    _migrate_v4,
    get_connection,
    initialize_database,
    to_json,
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _versions(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT version FROM schema_version ORDER BY id").fetchall()
    return [r["version"] for r in rows]


def _make_v3_db(db_path: Path) -> None:
    """Simulate a database that has been fully migrated to schema v3."""
    conn = get_connection(db_path)
    _create_schema_version_table(conn)
    conn.commit()
    _migrate_v1(conn)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    _migrate_v2(conn)
    conn.execute("INSERT INTO schema_version (version) VALUES (2)")
    _migrate_v3(conn)
    conn.execute("INSERT INTO schema_version (version) VALUES (3)")
    conn.commit()
    conn.close()


def _make_v4_db(db_path: Path) -> None:
    """Simulate a database that has been fully migrated to schema v4."""
    conn = get_connection(db_path)
    _create_schema_version_table(conn)
    conn.commit()
    _migrate_v1(conn)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    _migrate_v2(conn)
    conn.execute("INSERT INTO schema_version (version) VALUES (2)")
    _migrate_v3(conn)
    conn.execute("INSERT INTO schema_version (version) VALUES (3)")
    _migrate_v4(conn)
    conn.execute("INSERT INTO schema_version (version) VALUES (4)")
    conn.commit()
    conn.close()


class TestFreshDatabase:
    def test_fresh_db_reaches_v5(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        initialize_database(db)
        with get_connection(db) as conn:
            assert _versions(conn) == [1, 2, 3, 4, 5]
            tables = _tables(conn)
            assert {
                "tool_registry", "tool_revisions", "skill_bindings",
            } <= tables

    def test_current_version_constant(self) -> None:
        assert CURRENT_SCHEMA_VERSION == 5

    def test_v4_tables_have_expected_columns(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        initialize_database(db)
        with get_connection(db) as conn:
            reg_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(tool_registry)"
            ).fetchall()}
            assert {
                "tool_id", "kind", "name", "source_url", "license_declared",
                "license_source", "capabilities", "broker_categories",
                "status", "install_path", "created_at", "updated_at",
            } <= reg_cols

            rev_cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(tool_revisions)"
            ).fetchall()}
            assert {
                "revision_id", "tool_id", "revision_sha", "artifact_sha256",
                "tree_sha256", "quarantine_path", "scan_state",
                "scan_report_path", "scan_report_sha256", "scanner_identity",
                "risk_score", "severity", "recommendation",
                "unresolved_findings", "policy_decision", "policy_reasons",
                "approval_request_id", "approved_by", "approved_at",
                "approval_artifact_sha256", "approval_report_sha256",
                "installed_at", "created_at", "updated_at",
            } <= rev_cols

    def test_unique_constraint_on_tool_and_revision(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        initialize_database(db)
        with get_connection(db) as conn:
            conn.execute(
                """INSERT INTO tool_registry
                   (tool_id, kind, name, source_url, created_at, updated_at)
                   VALUES ('t1', 'agent_skill', 'n', 'u', 'now', 'now')"""
            )
            conn.execute(
                """INSERT INTO tool_revisions
                   (revision_id, tool_id, revision_sha, created_at, updated_at)
                   VALUES ('r1', 't1', 'sha', 'now', 'now')"""
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO tool_revisions
                       (revision_id, tool_id, revision_sha, created_at, updated_at)
                       VALUES ('r2', 't1', 'sha', 'now', 'now')"""
                )
            conn.rollback()

    def test_revision_foreign_key_enforced(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        initialize_database(db)
        with get_connection(db) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO tool_revisions
                       (revision_id, tool_id, revision_sha, created_at, updated_at)
                       VALUES ('r1', 'ghost', 'sha', 'now', 'now')"""
                )
            conn.rollback()

    def test_indexes_created(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        initialize_database(db)
        with get_connection(db) as conn:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
            assert {
                "idx_tool_registry_status",
                "idx_tool_revisions_tool",
                "idx_tool_revisions_sha",
                "idx_skill_bindings_project",
                "idx_skill_bindings_tool",
            } <= names

    def test_v5_skill_bindings_columns(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        initialize_database(db)
        with get_connection(db) as conn:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(skill_bindings)"
            ).fetchall()}
            assert {
                "binding_id", "project_key", "tool_id", "revision_sha",
                "note", "actor", "enabled", "created_at", "updated_at",
            } <= cols

    def test_v5_unique_project_tool_constraint(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        initialize_database(db)
        with get_connection(db) as conn:
            conn.execute(
                """INSERT INTO tool_registry
                   (tool_id, kind, name, source_url, created_at, updated_at)
                   VALUES ('t1', 'agent_skill', 'n', 'u', 'now', 'now')"""
            )
            conn.execute(
                """INSERT INTO skill_bindings
                   (binding_id, project_key, tool_id, revision_sha, note,
                    actor, enabled, created_at, updated_at)
                   VALUES ('b1', 'proj', 't1', 'sha', 'n', 'a', 1, 't', 't')"""
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO skill_bindings
                       (binding_id, project_key, tool_id, revision_sha, note,
                        actor, enabled, created_at, updated_at)
                       VALUES ('b2', 'proj', 't1', 'sha2', 'n', 'a', 1,
                               't', 't')"""
                )
            conn.rollback()

    def test_v5_binding_foreign_key_enforced(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        initialize_database(db)
        with get_connection(db) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO skill_bindings
                       (binding_id, project_key, tool_id, revision_sha, note,
                        actor, enabled, created_at, updated_at)
                       VALUES ('b1', 'proj', 'ghost', 'sha', 'n', 'a', 1,
                               't', 't')"""
                )
            conn.rollback()


class TestV3ToV4Upgrade:
    def test_upgrade_preserves_existing_data(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        _make_v3_db(db)

        # Seed pre-existing v3 rows.
        with get_connection(db) as conn:
            conn.execute(
                """INSERT INTO projects
                   (project_id, name, status, created_at, updated_at, metadata)
                   VALUES ('p1', 'keepme', 'intake', 't', 't', '{}')"""
            )
            conn.execute(
                """INSERT INTO events (event_type, timestamp, payload)
                   VALUES ('old.event', 't', ?)""",
                (to_json({"kept": True}),),
            )
            conn.commit()

        initialize_database(db)

        with get_connection(db) as conn:
            assert _versions(conn) == [1, 2, 3, 4, 5]
            assert {"tool_registry", "tool_revisions"} <= _tables(conn)
            project = conn.execute(
                "SELECT name FROM projects WHERE project_id='p1'"
            ).fetchone()
            assert project is not None and project["name"] == "keepme"
            event = conn.execute(
                "SELECT payload FROM events WHERE event_type='old.event'"
            ).fetchone()
            assert event is not None and "kept" in event["payload"]

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        initialize_database(db)
        initialize_database(db)  # second run must be a no-op
        with get_connection(db) as conn:
            assert _versions(conn) == [1, 2, 3, 4, 5]
            assert _tables(conn).issuperset(
                {"tool_registry", "tool_revisions", "skill_bindings"}
            )


class TestV4ToV5Upgrade:
    def test_upgrade_preserves_v4_data(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        _make_v4_db(db)

        # Seed pre-existing v4 rows.
        with get_connection(db) as conn:
            conn.execute(
                """INSERT INTO tool_registry
                   (tool_id, kind, name, source_url, status, created_at,
                    updated_at)
                   VALUES ('t_keep', 'agent_skill', 'keepme', 'u',
                           'installed', 't', 't')"""
            )
            conn.execute(
                """INSERT INTO tool_revisions
                   (revision_id, tool_id, revision_sha, scan_state,
                    created_at, updated_at)
                   VALUES ('r_keep', 't_keep', 'sha1', 'clean', 't', 't')"""
            )
            conn.execute(
                """INSERT INTO events (event_type, timestamp, payload)
                   VALUES ('old.v4.event', 't', ?)""",
                (to_json({"kept": True}),),
            )
            conn.commit()

        initialize_database(db)

        with get_connection(db) as conn:
            assert _versions(conn) == [1, 2, 3, 4, 5]
            assert "skill_bindings" in _tables(conn)
            tool = conn.execute(
                "SELECT name FROM tool_registry WHERE tool_id='t_keep'"
            ).fetchone()
            assert tool is not None and tool["name"] == "keepme"
            rev = conn.execute(
                "SELECT scan_state FROM tool_revisions "
                "WHERE revision_id='r_keep'"
            ).fetchone()
            assert rev is not None and rev["scan_state"] == "clean"
            event = conn.execute(
                "SELECT payload FROM events WHERE event_type='old.v4.event'"
            ).fetchone()
            assert event is not None and "kept" in event["payload"]

    def test_v4_to_v5_migration_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "factory.db"
        _make_v4_db(db)
        initialize_database(db)
        initialize_database(db)  # second run must be a no-op
        with get_connection(db) as conn:
            assert _versions(conn) == [1, 2, 3, 4, 5]


class TestNonNegotiablePolicy:
    def test_tool_install_approval_is_non_negotiable(self) -> None:
        from factory.permissions.policies import NON_NEGOTIABLE, PolicySet
        assert "require_approval_tool_install" in NON_NEGOTIABLE
        assert PolicySet().require_approval_tool_install is True
