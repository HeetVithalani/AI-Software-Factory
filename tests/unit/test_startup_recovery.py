"""
Unit tests for startup reconstruction (roadmap 3.3) and task persistence.

Covers:
- TaskStore seeding/upsert semantics (running/completed/retry timestamps)
- recovery-required resolution (retry/skip) with actor requirement
- prepare_dag_for_execution reconciliation rules:
  * fresh run -> all pending
  * completed + evidence preserved (idempotent)
  * completed without evidence -> recovery_required
  * RUNNING/PAUSED -> recovery_required, never auto-retried
  * awaiting approval restored with its request id
  * plan identity mismatch refused
  * checkpoint hash mismatch taints the checkpoint's task
- Orchestrator persistence: rows, evidence hashes, checkpoints, retries
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.database import get_connection, initialize_database
from factory.models import AgentResult, TaskStatus
from factory.orchestration.boot import prepare_dag_for_execution
from factory.orchestration.dag import TaskDAG, TaskNode
from factory.orchestration.orchestrator import Orchestrator, OrchestratorConfig
from factory.orchestration.recovery import RecoveryManager
from factory.orchestration.task_store import TaskStore


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "factory.db"
    initialize_database(db)
    return db


def _dag() -> TaskDAG:
    dag = TaskDAG()
    dag.add(TaskNode("t1", "Build", agent_type="builder",
                     metadata={"description": "build"}))
    dag.add(TaskNode("t2", "Test", agent_type="qa",
                     depends_on=["t1"], metadata={"description": "test"}))
    return dag


def _seed(db: Path, project="p1", plan_id="plan-1", plan_hash="abc123"):
    dag = _dag()
    store = TaskStore(db)
    for node in dag.all_nodes():
        store.seed_node(project, node, plan_id=plan_id, plan_hash=plan_hash)
    return dag, store


def _insert_evidence(db: Path, task_id: str, project="p1") -> None:
    with get_connection(db) as conn:
        conn.execute(
            """INSERT INTO evidence
               (evidence_id, project_id, task_id, agent_id, kind, path,
                hash_sha256, metadata, recorded_at)
               VALUES ('e1', ?, ?, 'builder', 'file', 'out.txt',
                       'deadbeef', '{}', '2026-01-01T00:00:00+00:00')""",
            (project, task_id),
        )
        conn.commit()


def _set_status(db: Path, task_id: str, status: str,
                metadata: dict | None = None) -> None:
    with get_connection(db) as conn:
        if metadata is None:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE task_id = ?",
                (status, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status = ?, metadata = ? WHERE task_id = ?",
                (status, json.dumps(metadata), task_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# TaskStore
# ---------------------------------------------------------------------------

class TestTaskStore:
    def test_seed_creates_pending_row(self, tmp_path):
        db = _db(tmp_path)
        dag, store = _seed(db)
        rows = {r["task_id"]: r for r in store.list_rows("p1")}
        assert rows["t1"]["status"] == "pending"
        assert json.loads(rows["t1"]["metadata"])["plan_id"] == "plan-1"

    def test_seed_preserves_runtime_state(self, tmp_path):
        db = _db(tmp_path)
        dag, store = _seed(db)
        node = dag.get("t1")
        node.metadata["retry_count"] = 2
        store.upsert_node("p1", node)
        store.seed_node("p1", node, plan_id="plan-1", plan_hash="abc123")
        row = store.get_row("t1")
        assert int(row["retry_count"]) == 2

    def test_upsert_running_then_completed_timestamps(self, tmp_path):
        db = _db(tmp_path)
        dag, store = _seed(db)
        node = dag.get("t1")
        node.status = TaskStatus.RUNNING
        store.upsert_node("p1", node)
        row = store.get_row("t1")
        assert row["started_at"] and not row["completed_at"]

        node.status = TaskStatus.COMPLETED
        store.upsert_node("p1", node, result_summary="done")
        row = store.get_row("t1")
        assert row["completed_at"]
        assert row["result_summary"] == "done"

    def test_upsert_pending_clears_completed(self, tmp_path):
        db = _db(tmp_path)
        dag, store = _seed(db)
        node = dag.get("t1")
        node.status = TaskStatus.COMPLETED
        store.upsert_node("p1", node)
        node.status = TaskStatus.PENDING
        store.upsert_node("p1", node)
        row = store.get_row("t1")
        assert row["completed_at"] is None

    def test_mark_and_resolve_retry(self, tmp_path):
        db = _db(tmp_path)
        _, store = _seed(db)
        store.mark_running_uncertain("t1", "interrupted")
        row = store.get_row("t1")
        assert row["status"] == "failed"
        meta = json.loads(row["metadata"])
        assert meta["recovery_required"] is True
        assert meta["terminal_failure"] is True

        store.resolve_recovery("t1", "retry", actor="operator", note="ok")
        row = store.get_row("t1")
        assert row["status"] == "pending"
        meta = json.loads(row["metadata"])
        assert "recovery_required" not in meta
        assert meta["recovery_resolution"]["action"] == "retry"

    def test_resolve_skip(self, tmp_path):
        db = _db(tmp_path)
        _, store = _seed(db)
        store.mark_running_uncertain("t1", "interrupted")
        store.resolve_recovery("t1", "skip", actor="operator")
        row = store.get_row("t1")
        assert row["status"] == "skipped"
        assert json.loads(row["metadata"])["terminal_failure"] is True

    def test_resolve_requires_actor_and_marks(self, tmp_path):
        db = _db(tmp_path)
        _, store = _seed(db)
        store.mark_running_uncertain("t1", "interrupted")
        with pytest.raises(ValueError):
            store.resolve_recovery("t1", "retry", actor="  ")
        with pytest.raises(ValueError):
            store.resolve_recovery("t1", "explode", actor="op")
        # A task that is not recovery-required cannot be "resolved".
        with pytest.raises(ValueError):
            store.resolve_recovery("t2", "retry", actor="op")


# ---------------------------------------------------------------------------
# Boot reconciliation
# ---------------------------------------------------------------------------

class TestStartupReconstruction:
    def test_fresh_run_has_no_recovery_requirement(self, tmp_path):
        db = _db(tmp_path)
        dag, _ = _seed(db)
        result = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is False
        assert result.issues == []
        assert all(n.status == TaskStatus.PENDING for n in dag.all_nodes())

    def test_completed_with_evidence_is_preserved(self, tmp_path):
        db = _db(tmp_path)
        dag, _ = _seed(db)
        _set_status(db, "t1", "completed")
        _insert_evidence(db, "t1")

        result = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is False
        assert "t1" in result.completed_task_ids
        assert dag.get("t1").status == TaskStatus.COMPLETED
        # Second call is idempotent.
        again = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert again.recovery_required is False

    def test_completed_without_evidence_requires_recovery(self, tmp_path):
        db = _db(tmp_path)
        dag, _ = _seed(db)
        _set_status(db, "t1", "completed")

        result = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is True
        assert "t1" in result.uncertain_task_ids
        row = TaskStore(db).get_row("t1")
        assert row["status"] == "failed"
        assert json.loads(row["metadata"])["recovery_required"] is True

    def test_running_task_is_never_auto_retried(self, tmp_path):
        db = _db(tmp_path)
        dag, _ = _seed(db)
        _set_status(db, "t1", "running")

        result = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is True
        node = dag.get("t1")
        assert node.status == TaskStatus.FAILED
        assert node.metadata["terminal_failure"] is True
        # Orchestrator's generic requeue pass must NOT pick it up.
        assert dag.is_failure_recoverable("t1") is False

    def test_running_resolution_then_reboot_is_clean(self, tmp_path):
        db = _db(tmp_path)
        dag, store = _seed(db)
        _set_status(db, "t1", "running")
        first = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert first.recovery_required is True
        store.resolve_recovery("t1", "retry", actor="operator")

        dag2 = _dag()
        second = prepare_dag_for_execution(
            dag2, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert second.recovery_required is False
        assert dag2.get("t1").status == TaskStatus.PENDING

    def test_awaiting_approval_is_restored_with_request_id(self, tmp_path):
        db = _db(tmp_path)
        dag, _ = _seed(db)
        _set_status(db, "t1", "awaiting_approval",
                    metadata={"approval_request_id": "req-1"})

        result = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is False
        assert "t1" in result.awaiting_approval_task_ids
        node = dag.get("t1")
        assert node.status == TaskStatus.AWAITING_APPROVAL
        assert node.metadata["approval_request_id"] == "req-1"

    def test_awaiting_approval_without_request_id_fails_closed(self, tmp_path):
        db = _db(tmp_path)
        dag, _ = _seed(db)
        _set_status(db, "t1", "awaiting_approval", metadata={})

        result = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is True
        assert dag.get("t1").status == TaskStatus.FAILED

    def test_plan_identity_mismatch_is_refused(self, tmp_path):
        db = _db(tmp_path)
        dag, _ = _seed(db)
        _set_status(db, "t1", "pending",
                    metadata={"plan_id": "other-plan"})

        result = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is True
        assert any("belongs to plan" in issue for issue in result.issues)

    def test_checkpoint_plan_hash_mismatch_is_refused(self, tmp_path):
        db = _db(tmp_path)
        dag, _ = _seed(db)
        rm = RecoveryManager(db)
        rm.save_checkpoint(
            {"schema_version": 1, "plan_id": "plan-1",
             "plan_hash": "different-hash"},
            project_id="p1",
        )

        result = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is True
        assert any("plan hash" in issue for issue in result.issues)

    def test_checkpoint_workspace_mismatch_taints_task(self, tmp_path):
        db = _db(tmp_path)
        dag, _ = _seed(db)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        target = workspace / "out.txt"
        target.write_text("original", encoding="utf-8")
        rm = RecoveryManager(db)
        rm.save_checkpoint(
            {"schema_version": 1, "plan_id": "plan-1",
             "plan_hash": "abc123"},
            project_id="p1",
            task_id="t1",
            workspace_paths=[target],
        )
        target.write_text("tampered", encoding="utf-8")

        result = prepare_dag_for_execution(
            dag, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is True
        assert "t1" in result.uncertain_task_ids

    def test_failed_task_keeps_retry_semantics(self, tmp_path):
        db = _db(tmp_path)
        dag, store = _seed(db)
        node = dag.get("t1")
        node.metadata["retry_count"] = 0
        node.metadata["max_retries"] = 2
        node.status = TaskStatus.FAILED
        # Persist via store without a terminal marker.
        store.upsert_node("p1", node)
        _set_status(db, "t1", "failed",
                    metadata={"retry_count": 0, "max_retries": 2})

        dag2 = _dag()
        result = prepare_dag_for_execution(
            dag2, project_id="p1", plan_id="plan-1", plan_hash="abc123",
            db_path=db,
        )
        assert result.recovery_required is False
        assert dag2.is_failure_recoverable("t1") is True


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------

def _success_result(task_id: str, evidence: list[dict] | None = None) -> AgentResult:
    return AgentResult(
        agent_id="builder", task_id=task_id, success=True,
        summary="ok", evidence=evidence or [],
    )


class TestOrchestratorPersistence:
    def test_rows_evidence_and_checkpoint_recorded(self, tmp_path):
        db = _db(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        dag = _dag()
        store = TaskStore(db)

        def executor(task):
            out = workspace / f"{task.task_id}.txt"
            out.write_text("done", encoding="utf-8")
            return _success_result(
                task.task_id,
                evidence=[{"kind": "file", "path": f"{task.task_id}.txt"}],
            )

        orch = Orchestrator(
            dag,
            config=OrchestratorConfig(poll_interval_seconds=0.0),
            project_id="p1",
            plan_id="plan-1",
            plan_hash="abc123",
            task_store=store,
            workspace_root=workspace,
            recovery_manager=RecoveryManager(db),
        )
        orch.register_executor("builder", executor)
        orch.register_executor("qa", executor)
        status = orch.run(max_steps=50)
        assert status.value in ("idle",)

        rows = {r["task_id"]: r for r in store.list_rows("p1")}
        assert rows["t1"]["status"] == "completed"
        assert rows["t2"]["status"] == "completed"
        assert rows["t1"]["completed_at"]

        with get_connection(db) as conn:
            evidence = conn.execute(
                "SELECT task_id, hash_sha256 FROM evidence ORDER BY task_id"
            ).fetchall()
            checkpoints = conn.execute(
                "SELECT COUNT(*) AS n FROM checkpoints"
            ).fetchone()
        assert {e["task_id"] for e in evidence} == {"t1", "t2"}
        assert all(len(e["hash_sha256"]) == 64 for e in evidence)
        assert int(checkpoints["n"]) >= 3  # start, per task, final

    def test_transient_failure_retries_are_persisted(self, tmp_path):
        db = _db(tmp_path)
        dag = _dag()
        store = TaskStore(db)
        calls = {"n": 0}

        def flaky(task):
            calls["n"] += 1
            if calls["n"] == 1:
                return AgentResult(
                    agent_id="builder", task_id=task.task_id,
                    success=False, errors=["temporary glitch"],
                    failure_kind="transient",
                )
            return _success_result(task.task_id)

        orch = Orchestrator(
            dag,
            config=OrchestratorConfig(poll_interval_seconds=0.0,
                                      default_max_retries=2),
            project_id="p1",
            task_store=store,
        )
        orch.register_executor("builder", flaky)
        orch.register_executor("qa", flaky)
        orch.run(max_steps=50)
        row = store.get_row("t1")
        assert row["status"] == "completed"
        assert int(row["retry_count"]) == 1

    def test_terminal_failure_is_persisted_with_marker(self, tmp_path):
        db = _db(tmp_path)
        dag = _dag()
        store = TaskStore(db)

        def failing(task):
            return AgentResult(
                agent_id="builder", task_id=task.task_id,
                success=False, errors=["boom"], failure_kind="permanent",
            )

        orch = Orchestrator(
            dag,
            config=OrchestratorConfig(poll_interval_seconds=0.0),
            project_id="p1",
            task_store=store,
        )
        orch.register_executor("builder", failing)
        orch.register_executor("qa", failing)
        orch.run(max_steps=50)
        row = store.get_row("t1")
        assert row["status"] == "failed"
        assert json.loads(row["metadata"])["terminal_failure"] is True
