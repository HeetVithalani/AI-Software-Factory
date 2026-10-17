"""
Unit tests for bounded repair (REPAIRABLE failures) and the deterministic
final audit / release gate.

Repair semantics verified:
- allow_repair=False -> immediate terminal failure (no silent retry)
- repair cycle re-runs the builder with bounded failure context
- max_repair_cycles caps the loop; exhaustion yields a terminal failure
- completed downstream verification is invalidated when the builder reruns
- parked approvals and human skips are never reset by repair
- repair counts and context survive persistence

Audit semantics verified:
- every check uses persisted facts (approved plan, rows, evidence hashes)
- tampered plans, missing evidence, changed files, uncertain state fail
- legacy unpersisted runs pass their executable checks but are never
  release eligible (limitations recorded)
- AuditorAgent fails closed on any failed check
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.database import get_connection, initialize_database
from factory.models import AgentResult, TaskStatus
from factory.orchestration.dag import TaskDAG, TaskNode
from factory.orchestration.orchestrator import Orchestrator, OrchestratorConfig
from factory.orchestration.task_store import TaskStore
from factory.planning.plan_store import PlanStore, canonical_plan_hash
from factory.validation.final_audit import run_final_audit


def _task(agent_type: str, task_id: str, **metadata) -> TaskNode:
    return TaskNode(
        task_id=task_id, name=task_id, agent_type=agent_type,
        metadata={"description": task_id, **metadata},
    )


def _ok(task_id: str, agent_id: str = "stub") -> AgentResult:
    return AgentResult(agent_id=agent_id, task_id=task_id, success=True,
                       summary="ok")


def _repairable_failure(task_id: str, text: str = "2 tests failed") -> AgentResult:
    return AgentResult(
        agent_id="qa", task_id=task_id, success=False,
        errors=[f"Deterministic test run failed: {text}"],
        failure_kind="repairable",
    )


def _repair_dag(allow_repair=True, max_cycles=1, with_reviewer=False) -> TaskDAG:
    dag = TaskDAG()
    dag.add(_task("builder", "build-1", max_retries=2))
    if with_reviewer:
        dag.add(TaskNode(
            task_id="review-1", name="review-1", agent_type="reviewer",
            depends_on=["build-1"], metadata={"description": "review"},
        ))
    dag.add(TaskNode(
        task_id="test-1", name="test-1", agent_type="qa",
        depends_on=["build-1"],
        metadata={
            "description": "test", "allow_repair": allow_repair,
            "max_repair_cycles": max_cycles,
        },
    ))
    return dag


class TestBoundedRepair:
    def test_repair_reruns_builder_and_qa_then_succeeds(self):
        dag = _repair_dag()
        calls = {"build": 0, "test": 0}
        repair_contexts: list[str] = []

        def builder(task):
            calls["build"] += 1
            repair_contexts.append(str(task.metadata.get("repair_context", "")))
            return _ok(task.task_id, "builder")

        def qa(task):
            calls["test"] += 1
            if calls["test"] == 1:
                return _repairable_failure(task.task_id)
            return _ok(task.task_id, "qa")

        orch = Orchestrator(
            dag,
            config=OrchestratorConfig(poll_interval_seconds=0.0),
        )
        orch.register_executor("builder", builder)
        orch.register_executor("qa", qa)
        orch.run(max_steps=50)

        assert dag.get("test-1").status.value == "completed"
        assert calls == {"build": 2, "test": 2}
        assert dag.get("test-1").metadata["repair_attempts"] == 1
        assert repair_contexts[0] == ""
        assert "2 tests failed" in repair_contexts[1]
        assert "test_report.json" not in repair_contexts[1]

    def test_repair_disabled_is_terminal_immediately(self):
        dag = _repair_dag(allow_repair=False)
        calls = {"build": 0, "test": 0}

        def builder(task):
            calls["build"] += 1
            return _ok(task.task_id, "builder")

        def qa(task):
            calls["test"] += 1
            return _repairable_failure(task.task_id)

        orch = Orchestrator(
            dag, config=OrchestratorConfig(poll_interval_seconds=0.0),
        )
        orch.register_executor("builder", builder)
        orch.register_executor("qa", qa)
        orch.run(max_steps=50)

        node = dag.get("test-1")
        assert node.status.value == "failed"
        assert node.metadata["terminal_failure"] is True
        assert calls == {"build": 1, "test": 1}
        assert dag.is_failure_recoverable("test-1") is False

    def test_repair_budget_is_bounded(self):
        dag = _repair_dag(max_cycles=1)
        calls = {"build": 0, "test": 0}

        def builder(task):
            calls["build"] += 1
            return _ok(task.task_id, "builder")

        def qa(task):
            calls["test"] += 1
            return _repairable_failure(task.task_id)

        orch = Orchestrator(
            dag, config=OrchestratorConfig(poll_interval_seconds=0.0),
        )
        orch.register_executor("builder", builder)
        orch.register_executor("qa", qa)
        orch.run(max_steps=50)

        node = dag.get("test-1")
        assert node.status.value == "failed"
        assert node.metadata["repair_attempts"] == 1
        assert node.metadata["terminal_failure"] is True
        # First attempt + exactly one repair cycle.
        assert calls == {"build": 2, "test": 2}
        assert "repair_context" in dag.get("build-1").metadata

    def test_repair_invalidates_completed_downstream_work(self):
        dag = _repair_dag(with_reviewer=True)
        calls = {"build": 0, "review": 0, "test": 0}

        def builder(task):
            calls["build"] += 1
            return _ok(task.task_id, "builder")

        def reviewer(task):
            calls["review"] += 1
            return _ok(task.task_id, "reviewer")

        def qa(task):
            calls["test"] += 1
            if calls["test"] == 1:
                return _repairable_failure(task.task_id)
            return _ok(task.task_id, "qa")

        orch = Orchestrator(
            dag, config=OrchestratorConfig(poll_interval_seconds=0.0),
        )
        orch.register_executor("builder", builder)
        orch.register_executor("reviewer", reviewer)
        orch.register_executor("qa", qa)
        orch.run(max_steps=50)

        # The reviewer ran again because its verification was invalidated
        # by the repair cycle.
        assert calls["review"] == 2
        assert dag.get("review-1").status.value == "completed"

    def test_repair_never_resets_human_skips(self):
        dag = _repair_dag()
        dag.add(TaskNode(
            task_id="docs-1", name="docs-1", agent_type="documentation",
            depends_on=["build-1", "test-1"], metadata={"description": "docs"},
        ))
        calls = {"build": 0, "test": 0}

        def builder(task):
            calls["build"] += 1
            return _ok(task.task_id, "builder")

        def qa(task):
            calls["test"] += 1
            if calls["test"] == 1:
                # A human skip recorded before the verification failure.
                # docs-1 depends on test-1, so it is not in this step's
                # ready batch and cannot race the dispatcher.
                dag.update_status("docs-1", TaskStatus.SKIPPED)
                dag.get("docs-1").metadata["terminal_failure"] = True
                return _repairable_failure(task.task_id)
            return _ok(task.task_id, "qa")

        orch = Orchestrator(
            dag, config=OrchestratorConfig(poll_interval_seconds=0.0),
        )
        orch.register_executor("builder", builder)
        orch.register_executor("qa", qa)
        orch.run(max_steps=50)

        # docs-1 is a dependent of build-1: the repair invalidation pass
        # must preserve the human skip rather than re-queue it.
        assert dag.get("docs-1").status == TaskStatus.SKIPPED
        assert dag.get("docs-1").metadata["terminal_failure"] is True
        assert dag.get("test-1").status.value == "completed"

    def test_qa_transient_failure_still_uses_retry_budget(self):
        dag = _repair_dag()
        calls = {"test": 0}

        def builder(task):
            return _ok(task.task_id, "builder")

        def qa(task):
            calls["test"] += 1
            if calls["test"] == 1:
                return AgentResult(
                    agent_id="qa", task_id=task.task_id, success=False,
                    errors=["timeout while running pytest"],
                    failure_kind="transient",
                )
            return _ok(task.task_id, "qa")

        orch = Orchestrator(
            dag, config=OrchestratorConfig(
                poll_interval_seconds=0.0, default_max_retries=2,
            ),
        )
        orch.register_executor("builder", builder)
        orch.register_executor("qa", qa)
        orch.run(max_steps=50)
        assert dag.get("test-1").status.value == "completed"
        assert dag.get("test-1").metadata.get("retry_count") == 1
        assert "repair_attempts" not in dag.get("test-1").metadata


class TestPlanIdentityPropagation:
    def test_dispatched_agents_receive_plan_identity(self):
        dag = TaskDAG()
        dag.add(_task("builder", "build-1"))
        captured: dict = {}

        def builder(task):
            captured.update(task.metadata)
            return _ok(task.task_id, "builder")

        orch = Orchestrator(
            dag,
            config=OrchestratorConfig(poll_interval_seconds=0.0),
            plan_id="plan-abc",
            plan_hash="f" * 64,
        )
        orch.register_executor("builder", builder)
        orch.run(max_steps=10)

        assert captured["plan_id"] == "plan-abc"
        assert captured["plan_hash"] == "f" * 64
        # Persisted on the node so recovery/audit see the same identity.
        assert dag.get("build-1").metadata["plan_id"] == "plan-abc"

    def test_explicit_task_metadata_wins_over_run_identity(self):
        dag = TaskDAG()
        dag.add(_task("builder", "build-1", plan_id="task-level"))
        captured: dict = {}

        def builder(task):
            captured.update(task.metadata)
            return _ok(task.task_id, "builder")

        orch = Orchestrator(
            dag,
            config=OrchestratorConfig(poll_interval_seconds=0.0),
            plan_id="run-level",
        )
        orch.register_executor("builder", builder)
        orch.run(max_steps=10)
        assert captured["plan_id"] == "task-level"


# ---------------------------------------------------------------------------
# Final audit
# ---------------------------------------------------------------------------

_MINIMAL_PLAN = {
    "project_id": "p1",
    "pipeline": "standard",
    "profile": "balanced",
    "release_eligible": True,
    "requirements": [
        {"requirement_id": "R1", "kind": "functional", "text": "do it"}
    ],
    "tasks": [
        {
            "task_id": "t1", "name": "Build", "agent_type": "builder",
            "description": "build", "depends_on": [],
            "requirement_ids": ["R1"], "acceptance_criteria": ["works"],
            "metadata": {"required_tools": ["filesystem"]},
        },
        {
            "task_id": "t2", "name": "Test", "agent_type": "qa",
            "description": "test", "depends_on": ["t1"],
            "requirement_ids": ["R1"], "acceptance_criteria": ["passes"],
            "metadata": {
                "required_tools": ["testing"],
                "required_checks": ["pytest"],
            },
        },
        {
            "task_id": "t3", "name": "Security", "agent_type": "security",
            "description": "scan", "depends_on": ["t1"],
            "requirement_ids": ["R1"], "acceptance_criteria": ["clean"],
            "metadata": {"required_tools": ["security"]},
        },
        {
            "task_id": "t4", "name": "Audit", "agent_type": "auditor",
            "description": "audit", "depends_on": ["t2", "t3"],
            "requirement_ids": ["R1"], "acceptance_criteria": ["passes"],
            "metadata": {"required_tools": ["filesystem"]},
        },
    ],
}


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    db = tmp_path / "factory.db"
    initialize_database(db)
    monkeypatch.setenv("FACTORY_DB_PATH", str(db))
    import factory.config as config_mod
    import factory.database as database_mod
    monkeypatch.setattr(config_mod, "DB_PATH", db, raising=False)
    monkeypatch.setattr(database_mod, "DB_PATH", db, raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    return {"db": db, "workspace": workspace}


def _seed_completed_rows(
    project_id: str, workspace: Path, *, with_evidence: bool = True
) -> None:
    from factory.audit.evidence import attach_file_evidence

    store = TaskStore()
    for task_id, agent in (("t1", "builder"), ("t2", "qa"), ("t3", "security")):
        node = _task(agent, task_id)
        store.seed_node(project_id, node)
        node.status = TaskStatus.COMPLETED
        store.upsert_node(project_id, node)
        if with_evidence:
            artifact = workspace / f"{task_id}.txt"
            artifact.write_text(f"evidence for {task_id}", encoding="utf-8")
            attach_file_evidence(
                artifact,
                project_id=project_id,
                task_id=task_id,
                agent_id=agent,
                metadata={"kind": "file"},
            )


def _approved_plan(project_id: str):
    store = PlanStore()
    version = store.create(project_id, _MINIMAL_PLAN)
    store.decide(
        version.plan_id, "approved", actor="tester",
        expected_hash=version.plan_hash,
    )
    return store.load(version.plan_id)


class TestFinalAuditChecks:
    def test_clean_persisted_run_is_release_eligible(self, audit_env):
        plan = _approved_plan("p1")
        _seed_completed_rows("p1", audit_env["workspace"])
        result = run_final_audit(
            project_id="p1",
            task_rows=TaskStore().list_rows("p1"),
            plan=plan.plan,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            pipeline="standard",
            workspace_root=audit_env["workspace"],
        )
        assert result.passed is True
        assert result.release_eligible is True
        assert result.limitations == []

    def test_missing_evidence_fails(self, audit_env):
        plan = _approved_plan("p1")
        _seed_completed_rows("p1", audit_env["workspace"], with_evidence=False)
        result = run_final_audit(
            project_id="p1",
            task_rows=TaskStore().list_rows("p1"),
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            pipeline="standard",
            workspace_root=audit_env["workspace"],
        )
        assert result.passed is False
        check = next(c for c in result.failed_checks()
                     if c.name == "evidence_present")
        assert "t1" in check.detail

    def test_changed_workspace_file_fails_integrity(self, audit_env):
        plan = _approved_plan("p1")
        _seed_completed_rows("p1", audit_env["workspace"])
        (audit_env["workspace"] / "t1.txt").write_text(
            "tampered", encoding="utf-8"
        )
        result = run_final_audit(
            project_id="p1",
            task_rows=TaskStore().list_rows("p1"),
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            pipeline="standard",
            workspace_root=audit_env["workspace"],
        )
        assert result.passed is False
        check = next(c for c in result.failed_checks()
                     if c.name == "evidence_integrity")
        assert "t1.txt" in check.detail

    def test_tampered_plan_fails(self, audit_env):
        plan = _approved_plan("p1")
        _seed_completed_rows("p1", audit_env["workspace"])
        with get_connection() as conn:
            conn.execute(
                "UPDATE plans SET plan_json = ? WHERE plan_id = ?",
                (json.dumps({"project_id": "p1", "tasks": []}),
                 plan.plan_id),
            )
            conn.commit()
        result = run_final_audit(
            project_id="p1",
            task_rows=TaskStore().list_rows("p1"),
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            pipeline="standard",
            workspace_root=audit_env["workspace"],
        )
        assert result.passed is False
        failed = {c.name for c in result.failed_checks()}
        assert "plan_approval" in failed

    def test_uncertain_state_fails(self, audit_env):
        plan = _approved_plan("p1")
        _seed_completed_rows("p1", audit_env["workspace"])
        store = TaskStore()
        store.mark_running_uncertain("t2", "interrupted")
        result = run_final_audit(
            project_id="p1",
            task_rows=store.list_rows("p1"),
            plan_id=plan.plan_id,
            pipeline="standard",
            workspace_root=audit_env["workspace"],
        )
        assert result.passed is False
        assert {c.name for c in result.failed_checks()} >= {
            "tasks_completed", "no_uncertain_state",
        }

    def test_smoke_pipeline_never_release_eligible(self, audit_env):
        result = run_final_audit(
            project_id="p1",
            task_rows=[],
            pipeline="smoke",
            workspace_root=audit_env["workspace"],
        )
        assert result.passed is False
        assert not result.release_eligible

    def test_legacy_run_passes_executable_checks_but_is_not_release_eligible(
        self, audit_env
    ):
        result = run_final_audit(
            project_id="legacy",
            task_rows=[],
            pipeline="",
            workspace_root=None,
        )
        assert result.passed is True  # nothing executable failed
        assert result.release_eligible is False  # but nothing was verified
        assert len(result.limitations) >= 2

    def test_auditor_itself_is_excluded_from_completion_check(self, audit_env):
        plan = _approved_plan("p1")
        _seed_completed_rows("p1", audit_env["workspace"])
        store = TaskStore()
        auditor_node = _task("auditor", "t4")
        store.seed_node("p1", auditor_node)
        auditor_node.status = TaskStatus.RUNNING
        store.upsert_node("p1", auditor_node)
        result = run_final_audit(
            project_id="p1",
            task_rows=store.list_rows("p1"),
            exclude_task_id="t4",
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            pipeline="standard",
            workspace_root=audit_env["workspace"],
        )
        assert result.passed is True


class TestAuditorAgent:
    def _task_record(self, plan_id: str, plan_hash: str):
        from factory.models import TaskRecord
        return TaskRecord(
            project_id="p1", name="audit", agent_type="auditor",
            description="audit",
            metadata={
                "plan_id": plan_id, "plan_hash": plan_hash,
                "pipeline": "standard",
            },
        )

    def test_auditor_passes_on_clean_state(self, audit_env):
        from factory.agents.auditor import AuditorAgent

        plan = _approved_plan("p1")
        _seed_completed_rows("p1", audit_env["workspace"])

        agent = AuditorAgent(workspace_root=audit_env["workspace"])
        result = agent.run(self._task_record(plan.plan_id, plan.plan_hash))
        assert result.success is True
        assert result.outputs["release_eligible"] is True
        assert (audit_env["workspace"] / "audit_report.json").is_file()

    def test_auditor_fails_closed_on_tampering(self, audit_env):
        from factory.agents.auditor import AuditorAgent

        plan = _approved_plan("p1")
        _seed_completed_rows("p1", audit_env["workspace"])
        (audit_env["workspace"] / "t2.txt").write_text(
            "changed", encoding="utf-8"
        )

        agent = AuditorAgent(workspace_root=audit_env["workspace"])
        result = agent.run(self._task_record(plan.plan_id, plan.plan_hash))
        assert result.success is False
        assert result.failure_kind == "permanent"
        assert "evidence_integrity" in "\n".join(result.errors)
        # The report is still written for the human reviewing the failure.
        assert (audit_env["workspace"] / "audit_report.json").is_file()
        report = json.loads(
            (audit_env["workspace"] / "audit_report.json").read_text(
                encoding="utf-8"
            )
        )
        assert report["release_eligible"] is False
