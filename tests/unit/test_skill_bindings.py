"""
Unit tests for project-scoped skill enablement (Phase 2c bindings).

Covers: mandatory note/actor audit fields, project-path validation,
fail-closed enable preconditions (installed/clean/approved/canonical/
integrity), project isolation, tamper rejection, project-scoped
disable that never touches the global install, and the audit payloads
(IDs/hashes/reasons only — never skill text).

All databases and install roots live under pytest tmp_path.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.audit.logger import AuditLogger, list_events
from factory.database import get_connection, initialize_database
from factory.errors import (
    IntakeIntegrityError,
    IntakePolicyError,
    IntakeStateError,
)
from factory.intake import registry
from factory.intake.archive import hash_tree
from factory.models import (
    IntakeStatus,
    ScanState,
    ToolKind,
    ToolRecord,
    ToolRevision,
)
from factory.skills.bindings import (
    disable_skill,
    enable_skill,
    list_bindings,
    list_installed_skills,
    project_key_for,
    validate_enableable,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


# ---------------------------------------------------------------------------
# Fixture helpers: fully-installed synthetic skill (no network, no scanner)
# ---------------------------------------------------------------------------

def _install_skill(
    db: Path,
    install_root: Path,
    *,
    tool_id: str = "tool_alpha",
    sha: str = SHA_A,
    name: str = "alpha-skill",
    description: str = "pytest regression report generation",
    body: str = "Use pytest to generate regression reports for CI.",
    status: IntakeStatus = IntakeStatus.INSTALLED,
) -> Path:
    """Create an installed, clean, approval-bound skill revision."""
    src = install_root.parent / f"src-{tool_id}-{sha[:6]}"
    src.mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    tree = hash_tree(src)
    dest = install_root / tool_id / sha
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, dirs_exist_ok=True)

    now = datetime.now(timezone.utc)
    registry.upsert_tool(
        ToolRecord(
            tool_id=tool_id,
            kind=ToolKind.AGENT_SKILL,
            name=name,
            source_url=f"https://github.com/demo-org/{tool_id}",
            status=status,
            install_path=str(dest),
            created_at=now,
            updated_at=now,
        ),
        db,
    )
    req_id = f"req-{tool_id}-{sha[:8]}"
    with get_connection(db) as conn:
        conn.execute(
            """INSERT INTO approvals
               (request_id, action, target, reason, risk_level,
                requested_at, status, decided_at, decided_by)
               VALUES (?, 'intake.approve', ?, 'evidence', 'low',
                       ?, 'approved', ?, 'tester')""",
            (req_id, tool_id, now.isoformat(), now.isoformat()),
        )
        conn.commit()
    registry.insert_revision(
        ToolRevision(
            tool_id=tool_id,
            revision_sha=sha,
            artifact_sha256="a" * 64,
            tree_sha256=tree,
            scan_state=ScanState.CLEAN,
            scanner_identity="skillspector(static,no-llm),version=2.12.0",
            policy_decision="pass",
            approval_request_id=req_id,
            approved_by="tester",
            approved_at=now,
            approval_artifact_sha256="a" * 64,
            approval_report_sha256="b" * 64,
            installed_at=now,
            created_at=now,
            updated_at=now,
        ),
        db,
    )
    return dest


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    db = tmp_path / "factory.db"
    initialize_database(db)
    install_root = tmp_path / "install-root"
    install_root.mkdir()
    project_a = tmp_path / "project-a"
    project_a.mkdir()
    project_b = tmp_path / "project-b"
    project_b.mkdir()
    # Point the configured install root at tmp_path so CLI handlers (which
    # use settings defaults) never touch knowledge/tools/.
    from factory.config import get_settings
    monkeypatch.setattr(
        get_settings().intake, "install_root", str(install_root)
    )
    return SimpleNamespace(
        db=db, install_root=install_root, project_a=project_a, project_b=project_b,
    )


# ---------------------------------------------------------------------------
# Enable preconditions (fail-closed)
# ---------------------------------------------------------------------------

class TestEnablePreconditions:
    def test_enable_requires_mandatory_note(self, env) -> None:
        _install_skill(env.db, env.install_root)
        for bad_note in ("", "   "):
            with pytest.raises(IntakePolicyError, match="note"):
                enable_skill(
                    "tool_alpha", SHA_A, env.project_a,
                    note=bad_note, actor="tester",
                    db_path=env.db, install_root=env.install_root,
                )
        assert list_bindings(db_path=env.db) == []

    def test_enable_rejects_missing_project_path(self, env) -> None:
        _install_skill(env.db, env.install_root)
        with pytest.raises(IntakeStateError, match="[Pp]roject path"):
            enable_skill(
                "tool_alpha", SHA_A, env.project_a / "ghost",
                note="ok", actor="tester",
                db_path=env.db, install_root=env.install_root,
            )

    def test_enable_rejects_file_as_project_path(self, env) -> None:
        _install_skill(env.db, env.install_root)
        as_file = env.project_a / "somefile.txt"
        as_file.write_text("x", encoding="utf-8")
        with pytest.raises(IntakeStateError, match="[Pp]roject path"):
            enable_skill(
                "tool_alpha", SHA_A, as_file,
                note="ok", actor="tester",
                db_path=env.db, install_root=env.install_root,
            )

    def test_enable_rejects_unknown_tool(self, env) -> None:
        with pytest.raises(IntakeStateError, match="Unknown tool"):
            enable_skill(
                "tool_ghost", SHA_A, env.project_a,
                note="ok", actor="tester",
                db_path=env.db, install_root=env.install_root,
            )

    def test_enable_rejects_non_installed_tool(self, env) -> None:
        _install_skill(env.db, env.install_root, status=IntakeStatus.SCANNED)
        with pytest.raises(IntakeStateError, match="INSTALLED"):
            enable_skill(
                "tool_alpha", SHA_A, env.project_a,
                note="ok", actor="tester",
                db_path=env.db, install_root=env.install_root,
            )

    def test_enable_rejects_wrong_revision(self, env) -> None:
        _install_skill(env.db, env.install_root)
        with pytest.raises(IntakeStateError, match="not in the registry"):
            enable_skill(
                "tool_alpha", SHA_B, env.project_a,
                note="ok", actor="tester",
                db_path=env.db, install_root=env.install_root,
            )

    def test_enable_rejects_revoked_tool(self, env) -> None:
        _install_skill(env.db, env.install_root)
        registry.set_tool_status(
            "tool_alpha", IntakeStatus.REVOKED, env.db
        )
        with pytest.raises(IntakeStateError, match="REVOKED"):
            enable_skill(
                "tool_alpha", SHA_A, env.project_a,
                note="ok", actor="tester",
                db_path=env.db, install_root=env.install_root,
            )

    def test_enable_rejects_stale_tool(self, env) -> None:
        _install_skill(env.db, env.install_root)
        registry.set_tool_status("tool_alpha", IntakeStatus.STALE, env.db)
        with pytest.raises(IntakeStateError, match="INSTALLED"):
            enable_skill(
                "tool_alpha", SHA_A, env.project_a,
                note="ok", actor="tester",
                db_path=env.db, install_root=env.install_root,
            )

    def test_enable_rejects_tampered_install(self, env) -> None:
        dest = _install_skill(env.db, env.install_root)
        # Tamper AFTER installation: rewrite a file inside the tree.
        (dest / "SKILL.md").write_text(
            "---\nname: alpha-skill\ndescription: pytest regression report "
            "generation\n---\n\ntampered content\n",
            encoding="utf-8",
        )
        with pytest.raises(IntakeIntegrityError, match="tree hash"):
            enable_skill(
                "tool_alpha", SHA_A, env.project_a,
                note="ok", actor="tester",
                db_path=env.db, install_root=env.install_root,
            )

    def test_enable_rejects_missing_skill_md(self, env) -> None:
        dest = _install_skill(env.db, env.install_root)
        (dest / "SKILL.md").unlink()
        # Tree hash now mismatches too; either guard may fire first.
        with pytest.raises((IntakeIntegrityError, IntakeStateError)):
            enable_skill(
                "tool_alpha", SHA_A, env.project_a,
                note="ok", actor="tester",
                db_path=env.db, install_root=env.install_root,
            )

    def test_enable_happy_path_creates_binding(self, env) -> None:
        _install_skill(env.db, env.install_root)
        binding = enable_skill(
            "tool_alpha", SHA_A, env.project_a,
            note="needed for CI reports", actor="ops-user",
            db_path=env.db, install_root=env.install_root,
        )
        assert binding.tool_id == "tool_alpha"
        assert binding.revision_sha == SHA_A
        assert binding.enabled is True
        assert binding.actor == "ops-user"
        assert binding.project_key == project_key_for(env.project_a)
        stored = list_bindings(binding.project_key, db_path=env.db)
        assert len(stored) == 1
        assert stored[0].note == "needed for CI reports"

    def test_enable_twice_updates_same_binding(self, env) -> None:
        _install_skill(env.db, env.install_root)
        first = enable_skill(
            "tool_alpha", SHA_A, env.project_a,
            note="first", actor="ops", db_path=env.db,
            install_root=env.install_root,
        )
        # Install a second revision and re-enable it for the same project.
        _install_skill(env.db, env.install_root, sha=SHA_B)
        second = enable_skill(
            "tool_alpha", SHA_B, env.project_a,
            note="second", actor="ops", db_path=env.db,
            install_root=env.install_root,
        )
        stored = list_bindings(first.project_key, db_path=env.db)
        assert len(stored) == 1  # one binding per (project, tool)
        assert stored[0].revision_sha == SHA_B
        assert stored[0].binding_id == first.binding_id == second.binding_id

    def test_project_key_normalization_is_stable(self, env) -> None:
        base = project_key_for(env.project_a)
        assert project_key_for(str(env.project_a)) == base
        assert project_key_for(env.project_a / "." ) == base
        assert project_key_for(env.project_a / ".." / "project-a") == base


# ---------------------------------------------------------------------------
# Project-scoped disable
# ---------------------------------------------------------------------------

class TestDisableBinding:
    def test_unbind_removes_binding_and_keeps_global_install(self, env) -> None:
        _install_skill(env.db, env.install_root)
        enable_skill(
            "tool_alpha", SHA_A, env.project_a,
            note="n", actor="ops", db_path=env.db, install_root=env.install_root,
        )
        unbound = disable_skill(
            "tool_alpha", env.project_a,
            note="not needed", actor="ops", db_path=env.db,
        )
        assert unbound.enabled is False
        assert list_bindings(project_key_for(env.project_a), db_path=env.db) == []
        # Global installation untouched: still INSTALLED with path + revision.
        tool = registry.get_tool("tool_alpha", env.db)
        assert tool.status == IntakeStatus.INSTALLED
        assert tool.install_path is not None
        rev = registry.get_revision("tool_alpha", SHA_A, env.db)
        assert rev is not None
        assert rev.scan_state == ScanState.CLEAN
        assert Path(tool.install_path).is_dir()

    def test_unbind_requires_note(self, env) -> None:
        _install_skill(env.db, env.install_root)
        enable_skill(
            "tool_alpha", SHA_A, env.project_a,
            note="n", actor="ops", db_path=env.db, install_root=env.install_root,
        )
        with pytest.raises(IntakePolicyError, match="note"):
            disable_skill(
                "tool_alpha", env.project_a, note="  ", actor="ops",
                db_path=env.db,
            )
        assert len(list_bindings(db_path=env.db)) == 1  # unchanged

    def test_unbind_without_binding_rejected(self, env) -> None:
        with pytest.raises(IntakeStateError, match="No enabled skill binding"):
            disable_skill(
                "tool_alpha", env.project_a, note="n", actor="ops",
                db_path=env.db,
            )

    def test_unbind_does_not_affect_other_project(self, env) -> None:
        _install_skill(env.db, env.install_root)
        enable_skill(
            "tool_alpha", SHA_A, env.project_a,
            note="n", actor="ops", db_path=env.db, install_root=env.install_root,
        )
        enable_skill(
            "tool_alpha", SHA_A, env.project_b,
            note="n", actor="ops", db_path=env.db, install_root=env.install_root,
        )
        disable_skill(
            "tool_alpha", env.project_a, note="n", actor="ops", db_path=env.db,
        )
        assert list_bindings(project_key_for(env.project_a), db_path=env.db) == []
        assert len(list_bindings(project_key_for(env.project_b), db_path=env.db)) == 1


# ---------------------------------------------------------------------------
# Project isolation + inspection helpers
# ---------------------------------------------------------------------------

class TestProjectIsolationAndInspection:
    def test_binding_is_invisible_to_other_project(self, env) -> None:
        _install_skill(env.db, env.install_root)
        enable_skill(
            "tool_alpha", SHA_A, env.project_a,
            note="n", actor="ops", db_path=env.db, install_root=env.install_root,
        )
        assert list_bindings(project_key_for(env.project_b), db_path=env.db) == []

    def test_list_installed_skills_only_installed_agent_skills(self, env) -> None:
        _install_skill(env.db, env.install_root)
        _install_skill(
            env.db, env.install_root,
            tool_id="tool_beta", sha=SHA_B, status=IntakeStatus.SCANNED,
        )
        installed = list_installed_skills(db_path=env.db)
        assert [t.tool_id for t in installed] == ["tool_alpha"]


# ---------------------------------------------------------------------------
# Audit payloads: IDs/hashes/reasons only — never skill text
# ---------------------------------------------------------------------------

class TestBindingAudit:
    def test_enable_and_disable_audit_without_content(self, env) -> None:
        secret_body = "SUPERUniqueSkillBody-9137 do not log me"
        _install_skill(env.db, env.install_root, body=secret_body)
        audit = AuditLogger(db_path=env.db)
        enable_skill(
            "tool_alpha", SHA_A, env.project_a,
            note="needed", actor="ops-user",
            db_path=env.db, install_root=env.install_root, audit_logger=audit,
        )
        disable_skill(
            "tool_alpha", env.project_a,
            note="rotated", actor="ops-user",
            db_path=env.db, audit_logger=audit,
        )
        events = list_events(db_path=env.db, limit=50)
        enabled = [e for e in events if e["event_type"] == "skill.binding.enabled"]
        disabled = [e for e in events if e["event_type"] == "skill.binding.disabled"]
        assert len(enabled) == 1 and len(disabled) == 1

        en_payload = enabled[0]["payload"]
        assert en_payload["project_key"] == project_key_for(env.project_a)
        assert en_payload["tool_id"] == "tool_alpha"
        assert en_payload["revision_sha"] == SHA_A
        assert en_payload["actor"] == "ops-user"
        assert en_payload["note"] == "needed"
        assert set(en_payload) == {
            "project_key", "tool_id", "revision_sha", "actor", "note",
        }
        dis_payload = disabled[0]["payload"]
        assert dis_payload["reason"] == "rotated"
        assert set(dis_payload) == {
            "project_key", "tool_id", "revision_sha", "actor", "reason",
        }
        # Skill text never appears anywhere in the audit rows.
        assert "SUPERUniqueSkillBody-9137" not in enabled[0]["payload"]
        assert "SUPERUniqueSkillBody-9137" not in disabled[0]["payload"]
        assert "SUPERUniqueSkillBody-9137" not in json.dumps(events)


# ---------------------------------------------------------------------------
# validate_enableable used directly by the provider (shared guard)
# ---------------------------------------------------------------------------

class TestValidateEnableableShared:
    def test_returns_tool_and_revision_when_valid(self, env) -> None:
        _install_skill(env.db, env.install_root)
        tool, rev = validate_enableable(
            "tool_alpha", SHA_A, db_path=env.db, install_root=env.install_root,
        )
        assert tool.tool_id == "tool_alpha"
        assert rev.revision_sha == SHA_A

    def test_unbound_rejection_reasons_are_stable(self, env) -> None:
        # validate_enableable is what the provider relies on; a missing
        # install directory is a state error (not silently allowed).
        _install_skill(env.db, env.install_root)
        tool = registry.get_tool("tool_alpha", env.db)
        shutil.rmtree(tool.install_path)
        with pytest.raises(IntakeStateError):
            validate_enableable(
                "tool_alpha", SHA_A, db_path=env.db,
                install_root=env.install_root,
            )


# ---------------------------------------------------------------------------
# CLI handler behavior (missing note / bad path / wrong revision)
# ---------------------------------------------------------------------------

class TestEnableCliHandler:
    """CLI handlers use the factory default DB; redirect it to tmp_path."""

    @pytest.fixture(autouse=True)
    def _isolate_db(self, env, monkeypatch):
        monkeypatch.setattr("factory.config.DB_PATH", env.db)
        monkeypatch.setattr("factory.database.DB_PATH", env.db)

    def _args(self, env, **overrides):
        base = dict(
            tool_id="tool_alpha", revision=SHA_A,
            project_path=str(env.project_a), note="ok", actor="cli-actor",
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_cli_enable_missing_note_exits_nonzero(self, env, capsys) -> None:
        from factory.cli.intake_cmd import cmd_enable
        _install_skill(env.db, env.install_root)
        rc = cmd_enable(self._args(env, note=""))
        captured = capsys.readouterr()
        assert rc == 1
        assert "--note" in captured.err
        assert list_bindings(db_path=env.db) == []

    def test_cli_enable_bad_project_path_exits_nonzero(self, env, capsys) -> None:
        from factory.cli.intake_cmd import cmd_enable
        _install_skill(env.db, env.install_root)
        rc = cmd_enable(self._args(env, project_path=str(env.project_a / "ghost")))
        assert rc == 1
        assert "enable failed" in capsys.readouterr().err

    def test_cli_enable_wrong_revision_exits_nonzero(self, env, capsys) -> None:
        from factory.cli.intake_cmd import cmd_enable
        _install_skill(env.db, env.install_root)
        rc = cmd_enable(self._args(env, revision=SHA_B))
        assert rc == 1
        assert "enable failed" in capsys.readouterr().err

    def test_cli_enable_non_installed_exits_nonzero(self, env, capsys) -> None:
        from factory.cli.intake_cmd import cmd_enable
        _install_skill(env.db, env.install_root, status=IntakeStatus.SCANNED)
        rc = cmd_enable(self._args(env))
        assert rc == 1
        assert "enable failed" in capsys.readouterr().err

    def test_cli_enable_unbind_roundtrip_keeps_global_install(
        self, env, capsys,
    ) -> None:
        from factory.cli.intake_cmd import cmd_enable, cmd_unbind
        _install_skill(env.db, env.install_root)
        rc = cmd_enable(self._args(env))
        assert rc == 0
        out = capsys.readouterr().out
        assert "enabled for project" in out
        assert len(list_bindings(db_path=env.db)) == 1

        rc = cmd_unbind(argparse.Namespace(
            tool_id="tool_alpha", project_path=str(env.project_a),
            note="rotated out", actor="cli-actor",
        ))
        assert rc == 0
        assert list_bindings(db_path=env.db) == []
        # Project disable never revokes or deletes the global install.
        tool = registry.get_tool("tool_alpha", env.db)
        assert tool.status == IntakeStatus.INSTALLED
        assert Path(tool.install_path).is_dir()

    def test_cli_unbind_missing_note_exits_nonzero(self, env, capsys) -> None:
        from factory.cli.intake_cmd import cmd_enable, cmd_unbind
        _install_skill(env.db, env.install_root)
        assert cmd_enable(self._args(env)) == 0
        capsys.readouterr()
        rc = cmd_unbind(argparse.Namespace(
            tool_id="tool_alpha", project_path=str(env.project_a),
            note=" ", actor="cli-actor",
        ))
        assert rc == 1
        assert "--note" in capsys.readouterr().err
        assert len(list_bindings(db_path=env.db)) == 1  # unchanged
