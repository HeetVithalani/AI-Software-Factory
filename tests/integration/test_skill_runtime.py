"""
Integration tests for the project-scoped skill runtime (Phase 2c).

End-to-end flow: installed skill (fixture) -> 'factory intake enable'
CLI -> ContextBuilder wiring -> bounded, attributed, untrusted-framed
skill context appended to the LLM request. Also pins byte-compatible
behavior when no skills are enabled, fail-safe behavior when the
provider breaks, and audit records that carry IDs/hashes only.

All databases, install roots and project workspaces live under pytest
tmp_path; no network, no LLM calls, no Ollama, no machine paths.
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
from factory.context.builder import ContextBuilder
from factory.database import get_connection, initialize_database
from factory.intake import registry
from factory.intake.archive import hash_tree
from factory.models import (
    IntakeStatus,
    ScanState,
    ToolKind,
    ToolRecord,
    ToolRevision,
)
from factory.skills.bindings import enable_skill, project_key_for
from factory.skills.provider import UNTRUSTED_HEADER, SkillContextProvider
from factory.state import FactoryState

SHA_A = "a" * 40


def _install_skill(
    db: Path,
    install_root: Path,
    *,
    tool_id: str = "tool_alpha",
    sha: str = SHA_A,
    body: str = "Use pytest to generate regression reports for CI.",
) -> Path:
    src = install_root.parent / f"src-{tool_id}-{sha[:6]}"
    src.mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(
        "---\nname: alpha-skill\n"
        "description: pytest regression report generation\n---\n\n"
        f"{body}\n",
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
            name="alpha-skill",
            source_url=f"https://github.com/demo-org/{tool_id}",
            status=IntakeStatus.INSTALLED,
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
    # Redirect the factory default DB (CLI handlers + ContextPersister)
    # and the configured install root to tmp_path.
    monkeypatch.setattr("factory.config.DB_PATH", db)
    monkeypatch.setattr("factory.database.DB_PATH", db)
    from factory.config import get_settings
    monkeypatch.setattr(
        get_settings().intake, "install_root", str(install_root)
    )
    return SimpleNamespace(
        db=db, install_root=install_root, project_a=project_a,
    )


def _enable_cli(env, capsys=None) -> None:
    from factory.cli.intake_cmd import cmd_enable
    rc = cmd_enable(argparse.Namespace(
        tool_id="tool_alpha", revision=SHA_A,
        project_path=str(env.project_a),
        note="needed for CI regression reports", actor="tester",
    ))
    assert rc == 0
    if capsys is not None:
        capsys.readouterr()


class TestRunScopedInjection:
    def test_enable_then_build_request_injects_untrusted_block(
        self, env, capsys,
    ) -> None:
        _install_skill(env.db, env.install_root)
        _enable_cli(env, capsys)

        provider = SkillContextProvider(
            project_key=project_key_for(env.project_a),
            audit_logger=AuditLogger(db_path=env.db),
        )
        builder = ContextBuilder(FactoryState(), skill_provider=provider)
        request = builder.build_request(
            task_id="task-qa-1",
            system_prompt="You are the QA agent.",
            messages=[{
                "role": "user",
                "content": "Fix the pytest regression reporting module.",
            }],
            project_id="proj-1",
            agent_id="qa",
        )
        prompt = request.system_prompt
        # Policy-first framing: the agent's own prompt comes first and the
        # skill block is attributed + labeled untrusted.
        assert prompt.startswith("You are the QA agent.")
        assert UNTRUSTED_HEADER in prompt
        assert "--- skill reference: tool=tool_alpha revision=" + SHA_A in prompt
        assert "Use pytest to generate regression reports" in prompt
        assert prompt.index("You are the QA agent.") < prompt.index(UNTRUSTED_HEADER)

    def test_other_project_workspace_gets_no_injection(self, env, capsys) -> None:
        _install_skill(env.db, env.install_root)
        _enable_cli(env, capsys)
        other_project = env.project_a.parent / "other-project"
        other_project.mkdir()
        provider = SkillContextProvider(
            project_key=project_key_for(other_project),
            audit_logger=AuditLogger(db_path=env.db),
        )
        builder = ContextBuilder(FactoryState(), skill_provider=provider)
        request = builder.build_request(
            task_id="task-1",
            system_prompt="You are the QA agent.",
            messages=[{
                "role": "user",
                "content": "Fix the pytest regression reporting module.",
            }],
        )
        assert request.system_prompt == "You are the QA agent."

    def test_no_enabled_skills_is_byte_compatible(self, env) -> None:
        _install_skill(env.db, env.install_root)  # installed, never enabled
        provider = SkillContextProvider(
            project_key=project_key_for(env.project_a),
            audit_logger=AuditLogger(db_path=env.db),
        )
        builder_with = ContextBuilder(FactoryState(), skill_provider=provider)
        builder_without = ContextBuilder(FactoryState())
        kwargs = dict(
            task_id="t1",
            system_prompt="Sys prompt.",
            messages=[{"role": "user", "content": "hello"}],
            project_id="p1",
        )
        req_with = builder_with.build_request(**kwargs)
        req_without = builder_without.build_request(**kwargs)
        assert req_with.system_prompt == req_without.system_prompt
        assert req_with.system_prompt == "Sys prompt."
        assert req_with.messages == req_without.messages
        # No audit noise and no additional machinery for the zero-skill case.
        events = [
            e for e in list_events(db_path=env.db, limit=50)
            if e["event_type"] == "runtime.skill_context.selected"
        ]
        assert events == []

    def test_failing_provider_still_builds_the_request(self, env) -> None:
        class BoomProvider:
            def select(self, **kwargs):
                raise RuntimeError("provider exploded")

        builder = ContextBuilder(FactoryState(), skill_provider=BoomProvider())
        request = builder.build_request(
            task_id="t1",
            system_prompt="Sys.",
            messages=[{"role": "user", "content": "x"}],
        )
        assert request.system_prompt == "Sys."
        assert request.task_id == "t1"

    def test_retries_rebuild_bounded_context_not_unbounded_growth(
        self, env, capsys,
    ) -> None:
        # The builder retry path calls build_request repeatedly with the
        # same inputs; skill context must stay identical and bounded.
        _install_skill(env.db, env.install_root)
        _enable_cli(env, capsys)
        provider = SkillContextProvider(
            project_key=project_key_for(env.project_a),
            audit_logger=AuditLogger(db_path=env.db),
        )
        builder = ContextBuilder(FactoryState(), skill_provider=provider)
        kwargs = dict(
            task_id="task-1",
            system_prompt="You are the QA agent.",
            messages=[{
                "role": "user",
                "content": "Fix the pytest regression reporting module.",
            }],
        )
        first = builder.build_request(**kwargs).system_prompt
        second = builder.build_request(**kwargs).system_prompt
        assert first == second  # no cumulative duplication across rebuilds


class TestAuditEndToEnd:
    def test_audit_carries_ids_and_hashes_but_never_content(
        self, env, capsys,
    ) -> None:
        secret = "SECRET skill body 8812 must never be audited"
        _install_skill(env.db, env.install_root, body=secret)
        _enable_cli(env, capsys)

        provider = SkillContextProvider(
            project_key=project_key_for(env.project_a),
            audit_logger=AuditLogger(db_path=env.db),
        )
        builder = ContextBuilder(FactoryState(), skill_provider=provider)
        builder.build_request(
            task_id="task-qa-9",
            system_prompt="You are the QA agent.",
            messages=[{
                "role": "user",
                "content": "Fix the pytest regression reporting module.",
            }],
            project_id="proj-42",
            agent_id="qa",
        )

        events = list_events(db_path=env.db, limit=100)
        selected = [
            e for e in events
            if e["event_type"] == "runtime.skill_context.selected"
        ]
        assert len(selected) == 1
        payload = selected[0]["payload"]
        assert payload["project_key"] == project_key_for(env.project_a)
        assert payload["project_id"] == "proj-42"
        assert payload["task_id"] == "task-qa-9"
        assert payload["agent_id"] == "qa"
        assert payload["selected"][0]["tool_id"] == "tool_alpha"
        assert payload["selected"][0]["revision_sha"] == SHA_A
        # Skill text appears nowhere in any audit row.
        assert secret not in json.dumps(events)

    def test_enable_audit_matches_binding_without_content(
        self, env, capsys,
    ) -> None:
        secret = "SECRET skill body 9931 must never be audited"
        _install_skill(env.db, env.install_root, body=secret)
        _enable_cli(env, capsys)
        events = [
            e for e in list_events(db_path=env.db, limit=100)
            if e["event_type"] == "skill.binding.enabled"
        ]
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["tool_id"] == "tool_alpha"
        assert payload["revision_sha"] == SHA_A
        assert payload["actor"] == "tester"
        assert secret not in events[0]["payload"]


class TestGlobalInertness:
    def test_run_scoped_provider_never_mutates_registry(self, env, capsys) -> None:
        _install_skill(env.db, env.install_root)
        _enable_cli(env, capsys)
        provider = SkillContextProvider(
            project_key=project_key_for(env.project_a),
            audit_logger=AuditLogger(db_path=env.db),
        )
        builder = ContextBuilder(FactoryState(), skill_provider=provider)
        builder.build_request(
            task_id="t1",
            system_prompt="You are the QA agent.",
            messages=[{
                "role": "user",
                "content": "Fix the pytest regression reporting module.",
            }],
        )
        # The provider is strictly read-only against the registry: status
        # and revision are untouched by injection.
        tool = registry.get_tool("tool_alpha", env.db)
        assert tool.status == IntakeStatus.INSTALLED
        rev = registry.get_revision("tool_alpha", SHA_A, env.db)
        assert rev.scan_state == ScanState.CLEAN
        assert rev.approval_bound is True
