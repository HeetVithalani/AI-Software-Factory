"""
Unit tests for the plan lifecycle CLI and inspection commands.

These tests exercise the real CLI entry point (``factory.cli.main.main``)
against an isolated database (via the ``FACTORY_DB_PATH`` override plus
attribute patching) and a throwaway workspace. No model is ever called:
execution-path tests only assert the fail-closed refusals that happen
before any agent or LLM is constructed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.database import get_connection, initialize_database
from factory.orchestration.task_store import TaskStore
from factory.planning.plan_store import PlanStore

SPEC = """# Demo CLI Project

## Goal
Provide a small greeting helper.

## Functional Requirements
- greet(name) returns a greeting string
- greet rejects empty names

## Non-functional Requirements
- Python 3.11+ compatible

## Acceptance Criteria
- pytest passes with at least one greeting test.

## Testing
- Run pytest.

## Security
- No user input is written to disk.
"""


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolated DB + workspace + spec for CLI tests."""
    db = tmp_path / "factory.db"
    initialize_database(db)
    monkeypatch.setenv("FACTORY_DB_PATH", str(db))

    import factory.config as config_mod
    import factory.database as database_mod

    monkeypatch.setattr(config_mod, "DB_PATH", db, raising=False)
    monkeypatch.setattr(database_mod, "DB_PATH", db, raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC, encoding="utf-8")
    return SimpleNamespace(db=db, workspace=workspace, spec=spec)


def _run_cli(monkeypatch, argv: list[str]) -> int:
    from factory.cli.main import main

    monkeypatch.setattr(sys, "argv", ["factory", *argv])
    return main()


def _create_plan(cli_env, monkeypatch, pipeline: str = "standard") -> str:
    rc = _run_cli(monkeypatch, [
        "plan", "create", str(cli_env.workspace),
        "--spec", str(cli_env.spec),
        "--pipeline", pipeline,
    ])
    assert rc == 0
    plans = PlanStore().list()
    assert len(plans) == 1
    return plans[0].plan_id


class TestPlanLifecycleCli:
    def test_create_persists_brief_research_and_pending_plan(
        self, cli_env, monkeypatch
    ):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)
        assert plan.status == "pending"
        assert plan.version == 1
        assert plan.pipeline == "standard"
        assert plan.brief_id
        assert plan.research_id
        assert len(plan.plan_hash) == 64

        with get_connection() as conn:
            briefs = conn.execute(
                "SELECT COUNT(*) AS n FROM project_briefs"
            ).fetchone()
            research = conn.execute(
                "SELECT COUNT(*) AS n FROM research_records"
            ).fetchone()
        assert int(briefs["n"]) == 1
        assert int(research["n"]) == 1

    def test_second_create_creates_new_version_and_supersedes(
        self, cli_env, monkeypatch
    ):
        first = _create_plan(cli_env, monkeypatch)
        rc = _run_cli(monkeypatch, [
            "plan", "create", str(cli_env.workspace),
            "--spec", str(cli_env.spec),
        ])
        assert rc == 0
        plans = PlanStore().list()
        assert len(plans) == 2
        assert plans[0].version == 2
        assert plans[0].status == "pending"
        assert PlanStore().load(first).status == "superseded"

    def test_show_prints_hash_and_approval_hint(self, cli_env, monkeypatch, capsys):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)
        rc = _run_cli(monkeypatch, ["plan", "show", plan_id])
        assert rc == 0
        out = capsys.readouterr().out
        assert plan.plan_hash in out
        assert "factory plan approve" in out

    def test_show_json_round_trips(self, cli_env, monkeypatch, capsys):
        plan_id = _create_plan(cli_env, monkeypatch)
        rc = _run_cli(monkeypatch, ["plan", "show", plan_id, "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        payload, _end = json.JSONDecoder().raw_decode(out[out.index("{"):])
        assert payload["plan_id"] == plan_id

    def test_approve_requires_correct_hash(self, cli_env, monkeypatch):
        plan_id = _create_plan(cli_env, monkeypatch)
        rc = _run_cli(monkeypatch, [
            "plan", "approve", plan_id, "--hash", "0" * 64,
            "--by", "tester",
        ])
        assert rc == 1
        assert PlanStore().load(plan_id).status == "pending"

    def test_approve_with_exact_hash_enables_execution(
        self, cli_env, monkeypatch
    ):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)
        rc = _run_cli(monkeypatch, [
            "plan", "approve", plan_id, "--hash", plan.plan_hash,
            "--by", "tester", "--note", "reviewed",
        ])
        assert rc == 0
        approved = PlanStore().require_approved(plan_id)
        assert approved.status == "approved"

    def test_rejected_plan_never_executes(self, cli_env, monkeypatch):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)
        rc = _run_cli(monkeypatch, [
            "plan", "reject", plan_id, "--hash", plan.plan_hash,
            "--by", "tester", "--note", "wrong scope",
        ])
        assert rc == 0
        rc = _run_cli(monkeypatch, ["run", "--plan-id", plan_id])
        assert rc == 1

    def test_run_refuses_pending_plan(self, cli_env, monkeypatch):
        plan_id = _create_plan(cli_env, monkeypatch)
        rc = _run_cli(monkeypatch, ["run", "--plan-id", plan_id])
        assert rc == 1

    def test_unknown_plan_refused(self, cli_env, monkeypatch):
        rc = _run_cli(monkeypatch, ["run", "--plan-id", "does-not-exist"])
        assert rc == 1

    def test_smoke_pipeline_is_not_release_eligible(self, cli_env, monkeypatch):
        plan_id = _create_plan(cli_env, monkeypatch, pipeline="smoke")
        plan = PlanStore().load(plan_id)
        assert plan.pipeline == "smoke"
        assert plan.plan["release_eligible"] is False
        assert len(plan.plan["tasks"]) == 1


class TestExecutionRefusals:
    def test_uncertain_running_task_refuses_run_and_marks_recovery(
        self, cli_env, monkeypatch
    ):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)
        rc = _run_cli(monkeypatch, [
            "plan", "approve", plan_id, "--hash", plan.plan_hash,
            "--by", "tester",
        ])
        assert rc == 0

        task_id = plan.plan["tasks"][0]["task_id"]
        store = TaskStore()
        # Simulate a run that died while this task was executing.
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO tasks
                   (task_id, project_id, name, description, agent_type,
                    status, risk_level, depends_on, created_at,
                    retry_count, max_retries, recovery_strategy,
                    result_summary, metadata)
                   VALUES (?, ?, 'Build', 'build', 'builder', 'running',
                           'safe', '[]', '2026-01-01T00:00:00+00:00',
                           0, 3, 'retry_same', '', '{}')""",
                (task_id, plan.project_id),
            )
            conn.commit()

        rc = _run_cli(monkeypatch, ["run", "--plan-id", plan_id])
        assert rc == 2  # refused, recovery required

        row = store.get_row(task_id)
        assert row["status"] == "failed"
        meta = json.loads(row["metadata"])
        assert meta["recovery_required"] is True
        assert meta["terminal_failure"] is True

    def test_recovery_status_and_resolve_retry(self, cli_env, monkeypatch, capsys):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)
        task_id = plan.plan["tasks"][0]["task_id"]
        store = TaskStore()
        from factory.orchestration.dag import TaskNode
        store.seed_node(
            plan.project_id,
            TaskNode(task_id=task_id, name="Build", agent_type="builder",
                     metadata={"description": "build"}),
            plan_id=plan_id,
            plan_hash=plan.plan_hash,
        )
        store.mark_running_uncertain(task_id, "interrupted mid-write")

        rc = _run_cli(monkeypatch, [
            "recovery", "status", "--project", plan.project_id,
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert task_id in out
        assert "interrupted mid-write" in out

        rc = _run_cli(monkeypatch, [
            "recovery", "resolve", task_id, "--action", "retry",
            "--by", "tester", "--note", "looked at the workspace",
        ])
        assert rc == 0
        row = store.get_row(task_id)
        assert row["status"] == "pending"
        assert "recovery_required" not in json.loads(row["metadata"])

    def test_resolve_requires_recovery_marker(self, cli_env, monkeypatch):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)
        task_id = plan.plan["tasks"][0]["task_id"]
        from factory.orchestration.dag import TaskNode
        TaskStore().seed_node(
            plan.project_id,
            TaskNode(task_id=task_id, name="Build", agent_type="builder",
                     metadata={"description": "build"}),
            plan_id=plan_id,
            plan_hash=plan.plan_hash,
        )
        rc = _run_cli(monkeypatch, [
            "recovery", "resolve", task_id, "--action", "retry",
            "--by", "tester",
        ])
        assert rc == 1

    def test_resolve_requires_actor_flag(self, cli_env, monkeypatch):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)
        task_id = plan.plan["tasks"][0]["task_id"]
        with pytest.raises(SystemExit):
            _run_cli(monkeypatch, [
                "recovery", "resolve", task_id, "--action", "retry",
            ])


class TestInspectCli:
    def test_tasks_and_evidence_and_audit(self, cli_env, monkeypatch, capsys):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)

        rc = _run_cli(monkeypatch, ["tasks", "--project", plan.project_id])
        assert rc == 0

        rc = _run_cli(monkeypatch, ["evidence", "--project", plan.project_id])
        assert rc == 0

        rc = _run_cli(monkeypatch, [
            "audit", "--project", plan.project_id, "--type", "plan.",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "plan.created" in out

    def test_no_projects_is_informative(self, cli_env, monkeypatch, capsys):
        rc = _run_cli(monkeypatch, ["tasks"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "No projects" in out


class TestApprovedPlanExecutionWiring:
    """
    Guards the wiring between the CLI execution path and the Orchestrator.

    Regression: ``run_approved_plan`` once passed the booted ``FactoryState``
    where the Orchestrator requires the ``StateManager``; the live CLI run
    died immediately with ``'FactoryState' object has no attribute
    'check_emergency_stop_file'``. A stubbed orchestrator keeps this test
    model-free while asserting the exact contract.
    """

    def test_run_wires_state_manager_and_agent_kwargs(self, cli_env, monkeypatch):
        plan_id = _create_plan(cli_env, monkeypatch)
        plan = PlanStore().load(plan_id)
        rc = _run_cli(monkeypatch, [
            "plan", "approve", plan_id, "--hash", plan.plan_hash,
            "--by", "tester",
        ])
        assert rc == 0

        captured: dict = {}

        class _StubOrchestrator:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self):
                class _Status:
                    value = "idle"
                return _Status()

        import factory.cli.plan_exec as plan_exec

        def _stub_register(orch, **kwargs):
            captured["agent_kwargs"] = kwargs
            return ["builder"]

        monkeypatch.setattr(plan_exec, "Orchestrator", _StubOrchestrator)
        monkeypatch.setattr(plan_exec, "register_all_agents", _stub_register)

        rc = _run_cli(monkeypatch, [
            "run", "--plan-id", plan_id,
            "--model", "ollama:qwen2.5-coder:7b-agent",
        ])
        assert rc == 0

        # The orchestrator must receive the state MANAGER, not a booted state.
        sm = captured["state_manager"]
        assert hasattr(sm, "check_emergency_stop_file")
        assert hasattr(sm, "boot_state")

        assert captured["plan_id"] == plan_id
        assert captured["plan_hash"] == plan.plan_hash
        assert captured["project_id"] == plan.project_id

        # Per-run model override reaches the single context resolution point.
        agent_kwargs = captured["agent_kwargs"]
        assert (
            agent_kwargs["context_builder"].default_model
            == "ollama:qwen2.5-coder:7b-agent"
        )


class TestProjectIntakeHelpers:
    def test_find_by_workspace_and_update_profile(self, cli_env, monkeypatch):
        _create_plan(cli_env, monkeypatch)
        from factory.models import QualityProfile
        from factory.projects.intake import ProjectIntake

        intake = ProjectIntake()
        project = intake.find_by_workspace(cli_env.workspace)
        assert project is not None
        assert intake.latest_project().project_id == project.project_id

        intake.update_profile(project.project_id, QualityProfile.FAST)
        reloaded = intake.load(project.project_id)
        value = getattr(reloaded.quality_profile, "value", reloaded.quality_profile)
        assert value == "fast"
