"""
tests/unit/test_orchestrator.py
================================
Deterministic unit tests for the B1/B2 orchestration semantics:

- bounded, deterministic retries (COR-004) via ``factory.orchestration.retry``
- retry-aware DAG unrecoverable-failure classification (COR-008)
- approval-gated pause and verified resume (COR-003)

All tests are offline and deterministic — no LLM, no Ollama, no network.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from factory.models import (
    AgentResult,
    ApprovalDecision,
    ApprovalStatus,
    RiskLevel,
    TaskRecord,
    TaskStatus,
)
from factory.orchestration.dag import TaskDAG, TaskNode
from factory.orchestration.orchestrator import Orchestrator, OrchestratorConfig
from factory.orchestration.retry import (
    FailureKind,
    classify_failure,
    extract_approval_request_id,
)
from factory.orchestration.state import OrchestratorStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _AuditRecorder:
    """Minimal audit-logger stand-in that records emitted events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, payload: dict | None = None) -> None:
        self.events.append((event_type, payload or {}))

    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def payloads(self, event_type: str) -> list[dict]:
        return [p for t, p in self.events if t == event_type]


def _result(
    *,
    success: bool = False,
    errors: list[str] | None = None,
    failure_kind: str = "",
    request_id: str = "",
    summary: str = "",
) -> AgentResult:
    return AgentResult(
        agent_id="test-agent",
        success=success,
        summary=summary,
        errors=errors or [],
        failure_kind=failure_kind,
        approval_request_id=request_id,
    )


def _build(
    executor,
    *,
    nodes: list[TaskNode] | None = None,
    audit: _AuditRecorder | None = None,
    config: OrchestratorConfig | None = None,
    broker=None,
    agent_type: str = "builder",
) -> tuple[Orchestrator, TaskDAG]:
    dag = TaskDAG()
    for node in nodes or [TaskNode(task_id="t1", name="Task 1", agent_type=agent_type)]:
        dag.add(node)
    orch = Orchestrator(
        dag,
        config=config or OrchestratorConfig(poll_interval_seconds=0.0),
        audit_logger=audit,
        permission_broker=broker,
    )
    orch.register_executor(agent_type, executor)
    return orch, dag


def _fast(config: OrchestratorConfig | None = None) -> OrchestratorConfig:
    return config or OrchestratorConfig(poll_interval_seconds=0.0)


@pytest.fixture
def approvals_db(tmp_path: Path) -> Path:
    """Fresh DB with the approvals/events schema (mirrors test_permissions)."""
    db_path = tmp_path / "orch-approvals.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            request_id            TEXT PRIMARY KEY,
            project_id            TEXT,
            task_id               TEXT,
            agent_id              TEXT,
            action                TEXT NOT NULL,
            target                TEXT NOT NULL,
            reason                TEXT NOT NULL,
            expected_effect       TEXT NOT NULL DEFAULT '',
            risk_level            TEXT NOT NULL,
            consequence_if_denied TEXT NOT NULL DEFAULT '',
            action_category       TEXT NOT NULL DEFAULT '',
            scope_key             TEXT NOT NULL DEFAULT '',
            operation_fingerprint TEXT,
            requested_at          TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'pending',
            decided_at            TEXT,
            decided_by            TEXT NOT NULL DEFAULT 'human',
            decision_note         TEXT NOT NULL DEFAULT '',
            grants_scope_inheritance INTEGER NOT NULL DEFAULT 0
        )
    """)
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
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def approval_manager(approvals_db: Path):
    from factory.permissions.approvals import ApprovalManager
    return ApprovalManager(db_path=approvals_db)


@pytest.fixture
def approval_broker(approvals_db: Path):
    from factory.permissions.broker import PermissionBroker
    return PermissionBroker(db_path=approvals_db)


def _request_approval(manager, *, action: str = "git: commit test.txt"):
    return manager.request_approval(
        action=action,
        target="workspace",
        reason="test needs approval",
        expected_effect="file changes committed",
        risk_level=RiskLevel.MEDIUM,
        consequence_if_denied="nothing happens",
        action_category="git.commit",
    )


def _decide(manager, request_id: str, status: ApprovalStatus) -> None:
    manager.record_decision(
        ApprovalDecision(
            request_id=request_id,
            status=status,
            decided_by="tester",
            decision_note="unit test decision",
        )
    )


# ===========================================================================
# 1. Deterministic failure classification (COR-004)
# ===========================================================================

class TestClassifyFailure:
    def test_structured_transient_wins_over_text(self):
        r = _result(errors=["something odd"], failure_kind="transient")
        assert classify_failure(r) == FailureKind.TRANSIENT

    def test_structured_awaiting_wins(self):
        r = _result(errors=["boom"], failure_kind="awaiting_approval",
                    request_id="r-1")
        assert classify_failure(r) == FailureKind.AWAITING_APPROVAL

    def test_invalid_structured_kind_falls_back_to_text(self):
        r = _result(errors=["connection reset by peer"], failure_kind="weird")
        assert classify_failure(r) == FailureKind.TRANSIENT

    @pytest.mark.parametrize("msg", [
        "operation timed out after 30s",
        "Timeout waiting for response",
        "Connection reset by peer",
        "Connection aborted by peer",
        "rate limit exceeded, retry later",
        "HTTP 429 Too Many Requests",
        "HTTP 503 service unavailable",
        "upstream returned 502 Bad Gateway",
        "temporarily unavailable, try again",
        "network unreachable during fetch",
    ])
    def test_known_transient_signatures(self, msg):
        assert classify_failure(_result(errors=[msg])) == FailureKind.TRANSIENT

    def test_connection_refused_is_not_transient(self):
        # Local-first design: refused means the service is not running —
        # a retry cannot fix it (deliberate, documented).
        r = _result(errors=["Connection refused by Ollama at localhost:11434"])
        assert classify_failure(r) == FailureKind.UNKNOWN

    @pytest.mark.parametrize("msg", [
        "No executor registered for agent_type='x'",
        "Exceeded max turns (15)",
        "Shell command denied by policy: dangerous",
        "path outside the workspace sandbox",
    ])
    def test_known_permanent_signatures(self, msg):
        assert classify_failure(_result(errors=[msg])) == FailureKind.PERMANENT

    def test_unknown_failure_is_unknown(self):
        r = _result(errors=["something exploded in a novel way"])
        assert classify_failure(r) == FailureKind.UNKNOWN

    def test_summary_used_when_no_errors(self):
        r = _result(errors=[], summary="rate limit hit (429)")
        assert classify_failure(r) == FailureKind.TRANSIENT

    def test_approval_text_is_awaiting_fallback(self):
        r = _result(
            errors=["Shell command requires human approval "
                    "(risk=high, request_id=abc-123): rm -rf x"]
        )
        assert classify_failure(r) == FailureKind.AWAITING_APPROVAL

    def test_approval_wins_over_transient_text(self):
        r = _result(
            errors=["requires human approval (request_id=x-1); "
                    "also timed out waiting"]
        )
        assert classify_failure(r) == FailureKind.AWAITING_APPROVAL

    def test_successful_result_classifies_unknown(self):
        # Contract: only failed results are classified.
        r = _result(success=True, errors=["weird text"])
        assert classify_failure(r) == FailureKind.UNKNOWN


class TestExtractApprovalRequestId:
    def test_extracts_uuid(self):
        rid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        text = f"blocked (request_id={rid}) because reasons"
        assert extract_approval_request_id(text) == rid

    def test_missing_returns_empty(self):
        assert extract_approval_request_id("no id here") == ""

    def test_empty_text_returns_empty(self):
        assert extract_approval_request_id("") == ""


# ===========================================================================
# 2. Bounded retries (COR-004)
# ===========================================================================

class TestBoundedRetries:
    def test_transient_failure_retries_then_succeeds(self):
        calls = {"n": 0}

        def executor(task: TaskRecord) -> AgentResult:
            calls["n"] += 1
            if calls["n"] < 3:
                return _result(errors=["Connection reset by peer"])
            return _result(success=True, summary="recovered")

        audit = _AuditRecorder()
        orch, dag = _build(executor, audit=audit, config=_fast())
        status = orch.run(max_steps=100)

        assert status == OrchestratorStatus.IDLE
        assert calls["n"] == 3
        assert dag._nodes["t1"].status == TaskStatus.COMPLETED
        retries = audit.payloads("task.retry")
        assert [p["attempt"] for p in retries] == [1, 2]
        assert all(p["max_retries"] == 3 for p in retries)
        assert all(p["failure_kind"] == "transient" for p in retries)

    def test_transient_failure_exhausts_budget(self):
        calls = {"n": 0}

        def executor(task: TaskRecord) -> AgentResult:
            calls["n"] += 1
            return _result(errors=["operation timed out"])

        audit = _AuditRecorder()
        orch, dag = _build(executor, audit=audit, config=_fast())
        status = orch.run(max_steps=100)

        # 1 initial attempt + 3 retries, then terminal failure.
        assert calls["n"] == 4
        node = dag._nodes["t1"]
        assert node.status == TaskStatus.FAILED
        assert node.metadata["retry_count"] == 3
        assert node.metadata["max_retries"] == 3
        assert node.metadata["terminal_failure"] is True
        assert len(audit.payloads("task.retry")) == 3
        failed = audit.payloads("task.failed")
        assert len(failed) == 1
        assert failed[0]["retry_exhausted"] is True
        assert failed[0]["retries_used"] == 3
        assert status in (OrchestratorStatus.IDLE, OrchestratorStatus.STOPPED)

    def test_unknown_failure_fails_immediately(self):
        calls = {"n": 0}

        def executor(task: TaskRecord) -> AgentResult:
            calls["n"] += 1
            return _result(errors=["something exploded in a novel way"])

        audit = _AuditRecorder()
        orch, dag = _build(executor, audit=audit, config=_fast())
        orch.run(max_steps=100)

        assert calls["n"] == 1  # fail-closed: unknown failures earn no retry
        assert dag._nodes["t1"].status == TaskStatus.FAILED
        assert audit.payloads("task.retry") == []
        failed = audit.payloads("task.failed")
        assert failed[0]["failure_kind"] == "unknown"
        assert failed[0]["retry_exhausted"] is False

    def test_permanent_failure_fails_immediately(self):
        calls = {"n": 0}

        def executor(task: TaskRecord) -> AgentResult:
            calls["n"] += 1
            return _result(errors=["Shell command denied by policy: nope"])

        audit = _AuditRecorder()
        orch, dag = _build(executor, audit=audit, config=_fast())
        orch.run(max_steps=100)

        assert calls["n"] == 1
        assert dag._nodes["t1"].status == TaskStatus.FAILED
        assert audit.payloads("task.failed")[0]["failure_kind"] == "permanent"

    def test_no_executor_registered_fails_immediately(self):
        dag = TaskDAG()
        dag.add(TaskNode(task_id="t1", name="T", agent_type="unregistered"))
        audit = _AuditRecorder()
        orch = Orchestrator(
            dag,
            config=_fast(),
            audit_logger=audit,
        )
        orch.run(max_steps=100)

        assert dag._nodes["t1"].status == TaskStatus.FAILED
        assert audit.payloads("task.retry") == []
        assert audit.payloads("task.failed")[0]["failure_kind"] == "permanent"

    def test_node_declared_budget_overrides_default(self):
        calls = {"n": 0}

        def executor(task: TaskRecord) -> AgentResult:
            calls["n"] += 1
            return _result(errors=["timed out waiting"])

        node = TaskNode(
            task_id="t1", name="T", agent_type="builder",
            metadata={"max_retries": 1},
        )
        orch, dag = _build(executor, nodes=[node], config=_fast())
        orch.run(max_steps=100)

        assert calls["n"] == 2  # initial + 1 declared retry
        assert dag._nodes["t1"].status == TaskStatus.FAILED

    def test_zero_budget_disables_retries(self):
        calls = {"n": 0}

        def executor(task: TaskRecord) -> AgentResult:
            calls["n"] += 1
            return _result(errors=["connection reset by peer"])

        orch, dag = _build(
            executor,
            config=OrchestratorConfig(
                poll_interval_seconds=0.0, default_max_retries=0
            ),
        )
        orch.run(max_steps=100)
        assert calls["n"] == 1
        assert dag._nodes["t1"].status == TaskStatus.FAILED

    def test_retry_backoff_is_configurable(self):
        def executor(task: TaskRecord) -> AgentResult:
            return _result(errors=["timed out"])

        node = TaskNode(
            task_id="t1", name="T", agent_type="builder",
            metadata={"max_retries": 1},
        )
        config = OrchestratorConfig(
            poll_interval_seconds=0.0,
            retry_backoff_seconds=0.3,
        )
        orch, dag = _build(executor, nodes=[node], config=config)
        started = time.monotonic()
        orch.run(max_steps=100)
        elapsed = time.monotonic() - started

        assert dag._nodes["t1"].status == TaskStatus.FAILED
        # Threshold well below the 0.3s backoff (timer jitter margin) but
        # far above the ~ms runtime without a backoff.
        assert elapsed >= 0.15

    def test_escalate_human_strategy_never_retries(self):
        calls = {"n": 0}

        def executor(task: TaskRecord) -> AgentResult:
            calls["n"] += 1
            return _result(errors=["connection reset by peer"])

        node = TaskNode(
            task_id="t1", name="T", agent_type="builder",
            metadata={"recovery_strategy": "escalate_human"},
        )
        audit = _AuditRecorder()
        orch, dag = _build(executor, nodes=[node], audit=audit, config=_fast())
        orch.run(max_steps=100)

        assert calls["n"] == 1
        node = dag._nodes["t1"]
        assert node.status == TaskStatus.FAILED
        assert node.metadata["terminal_failure"] is True
        assert audit.payloads("task.escalated")
        assert audit.payloads("task.retry") == []

    def test_skip_strategy_marks_skipped(self):
        def executor(task: TaskRecord) -> AgentResult:
            return _result(errors=["bad input"])

        node = TaskNode(
            task_id="t1", name="T", agent_type="builder",
            metadata={"recovery_strategy": "skip"},
        )
        audit = _AuditRecorder()
        orch, dag = _build(executor, nodes=[node], audit=audit, config=_fast())
        orch.run(max_steps=100)

        assert dag._nodes["t1"].status == TaskStatus.SKIPPED
        assert audit.payloads("task.skipped")

    def test_skipped_dependency_stops_cleanly(self):
        def executor(task: TaskRecord) -> AgentResult:
            return _result(errors=["bad input"])

        nodes = [
            TaskNode(
                task_id="t1", name="T1", agent_type="builder",
                metadata={"recovery_strategy": "skip"},
            ),
            TaskNode(task_id="t2", name="T2", agent_type="builder",
                     depends_on=["t1"]),
        ]
        audit = _AuditRecorder()
        orch, dag = _build(executor, nodes=nodes, audit=audit, config=_fast())
        status = orch.run(max_steps=100)

        # t1 skipped → t2 blocked forever → surfaced as unrecoverable
        # STOP instead of a silent spin.
        assert status == OrchestratorStatus.STOPPED
        assert dag._nodes["t1"].status == TaskStatus.SKIPPED
        assert dag._nodes["t2"].status == TaskStatus.PENDING


# ===========================================================================
# 3. Retry-aware DAG classification (COR-008)
# ===========================================================================

class TestRetryAwareDag:
    @staticmethod
    def _two_nodes(failed_metadata: dict | None = None) -> TaskDAG:
        dag = TaskDAG()
        dag.add(TaskNode(task_id="a", name="A", metadata=failed_metadata or {}))
        dag.add(TaskNode(task_id="b", name="B", depends_on=["a"]))
        dag.update_status("a", TaskStatus.FAILED)
        return dag

    def test_bare_failed_mark_is_terminal(self):
        # Historical behaviour: no declared retry budget → terminal.
        dag = self._two_nodes()
        assert dag.is_failure_recoverable("a") is False
        assert dag.has_unrecoverable_failure() is True

    def test_failed_with_budget_is_not_yet_unrecoverable(self):
        dag = self._two_nodes({"max_retries": 2, "retry_count": 0})
        assert dag.is_failure_recoverable("a") is True
        assert dag.has_unrecoverable_failure() is False

    def test_failed_exhausted_budget_is_unrecoverable(self):
        dag = self._two_nodes({"max_retries": 2, "retry_count": 2})
        assert dag.is_failure_recoverable("a") is False
        assert dag.has_unrecoverable_failure() is True

    def test_terminal_marker_beats_budget(self):
        dag = self._two_nodes({
            "max_retries": 5, "retry_count": 0, "terminal_failure": True,
        })
        assert dag.is_failure_recoverable("a") is False
        assert dag.has_unrecoverable_failure() is True

    def test_escalate_human_is_terminal(self):
        dag = self._two_nodes({
            "max_retries": 3, "retry_count": 0,
            "recovery_strategy": "escalate_human",
        })
        assert dag.is_failure_recoverable("a") is False
        assert dag.has_unrecoverable_failure() is True

    def test_malformed_metadata_fails_closed(self):
        dag = self._two_nodes({"max_retries": "lots"})
        assert dag.is_failure_recoverable("a") is False
        assert dag.has_unrecoverable_failure() is True

    def test_skipped_dependency_is_unrecoverable(self):
        dag = TaskDAG()
        dag.add(TaskNode(task_id="a", name="A"))
        dag.add(TaskNode(task_id="b", name="B", depends_on=["a"]))
        dag.update_status("a", TaskStatus.SKIPPED)
        assert dag.has_unrecoverable_failure() is True

    def test_no_failures_means_no_trigger(self):
        dag = TaskDAG()
        dag.add(TaskNode(task_id="a", name="A"))
        assert dag.has_unrecoverable_failure() is False

    def test_is_failure_recoverable_unknown_id(self):
        dag = TaskDAG()
        assert dag.is_failure_recoverable("missing") is False

    def test_awaiting_approval_not_terminal_for_completion(self):
        dag = TaskDAG()
        dag.add(TaskNode(task_id="a", name="A"))
        dag.update_status("a", TaskStatus.AWAITING_APPROVAL)
        assert dag.is_complete() is False
        assert dag.has_unrecoverable_failure() is False
        assert dag.awaiting_approval_ids() == {"a"}

    def test_orchestrator_requeues_external_failed_with_budget(self):
        calls = {"n": 0}

        def executor(task: TaskRecord) -> AgentResult:
            calls["n"] += 1
            return _result(success=True, summary="ok")

        nodes = [
            TaskNode(task_id="a", name="A", agent_type="builder",
                     metadata={"max_retries": 2, "retry_count": 0}),
            TaskNode(task_id="b", name="B", agent_type="builder",
                     depends_on=["a"]),
        ]
        nodes[0].status = TaskStatus.FAILED  # externally marked failure
        audit = _AuditRecorder()
        orch, dag = _build(executor, nodes=nodes, audit=audit, config=_fast())
        status = orch.run(max_steps=100)

        assert status == OrchestratorStatus.IDLE
        assert dag._nodes["a"].status == TaskStatus.COMPLETED
        assert dag._nodes["b"].status == TaskStatus.COMPLETED
        assert calls["n"] == 2
        assert audit.payloads("task.retry_requeued")

    def test_orchestrator_stops_on_terminal_external_failure(self):
        def executor(task: TaskRecord) -> AgentResult:
            return _result(success=True, summary="ok")

        nodes = [
            TaskNode(task_id="a", name="A", agent_type="builder"),
            TaskNode(task_id="b", name="B", agent_type="builder",
                     depends_on=["a"]),
        ]
        nodes[0].status = TaskStatus.FAILED  # bare mark → terminal
        orch, dag = _build(executor, nodes=nodes, config=_fast())
        status = orch.run(max_steps=100)

        assert status == OrchestratorStatus.STOPPED
        assert dag._nodes["b"].status == TaskStatus.PENDING


# ===========================================================================
# 4. Approval pause + verified resume (COR-003)
# ===========================================================================

class TestAwaitingApprovalPauseResume:
    def test_awaiting_result_parks_task_without_failing(self):
        def executor(task: TaskRecord) -> AgentResult:
            return _result(
                errors=["requires human approval (request_id=req-1)"],
                failure_kind="awaiting_approval",
                request_id="req-1",
            )

        audit = _AuditRecorder()
        orch, dag = _build(executor, audit=audit, config=_fast())
        status = orch.run(max_steps=100)

        node = dag._nodes["t1"]
        assert status == OrchestratorStatus.AWAITING_APPROVAL
        assert node.status == TaskStatus.AWAITING_APPROVAL  # NOT failed
        assert node.metadata["approval_request_id"] == "req-1"
        assert "awaiting_since" in node.metadata
        assert audit.payloads("task.awaiting_approval")
        assert audit.payloads("task.failed") == []
        assert audit.payloads("task.retry") == []  # no budget consumed
        assert orch.snapshot()["dag"]["awaiting_approval"] == 1

    def test_awaiting_without_broker_stays_parked(self):
        def executor(task: TaskRecord) -> AgentResult:
            return _result(
                errors=["requires human approval"],
                failure_kind="awaiting_approval",
                request_id="req-1",
            )

        orch, dag = _build(executor, config=_fast())
        status = orch.run(max_steps=50)
        assert status == OrchestratorStatus.AWAITING_APPROVAL
        assert dag._nodes["t1"].status == TaskStatus.AWAITING_APPROVAL

    def test_error_text_fallback_extracts_request_id(self):
        rid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

        def executor(task: TaskRecord) -> AgentResult:
            # Producer did NOT set failure_kind — orchestrator must
            # still parse the approval pause out of the error text.
            return _result(
                errors=[f"Shell command requires human approval "
                        f"(risk=high, request_id={rid}): something"]
            )

        orch, dag = _build(executor, config=_fast())
        status = orch.run(max_steps=50)
        node = dag._nodes["t1"]
        assert status == OrchestratorStatus.AWAITING_APPROVAL
        assert node.status == TaskStatus.AWAITING_APPROVAL
        assert node.metadata["approval_request_id"] == rid

    def test_awaiting_without_request_id_becomes_failed(self):
        def executor(task: TaskRecord) -> AgentResult:
            return _result(
                errors=["requires human approval but no id recorded"],
                failure_kind="awaiting_approval",
            )

        audit = _AuditRecorder()
        orch, dag = _build(executor, audit=audit, config=_fast())
        orch.run(max_steps=100)

        node = dag._nodes["t1"]
        assert node.status == TaskStatus.FAILED
        assert node.metadata["terminal_failure"] is True
        assert audit.payloads("task.awaiting_unresolvable")

    def test_other_work_completes_while_one_task_awaits(self):
        def executor(task: TaskRecord) -> AgentResult:
            if task.task_id == "t1":
                return _result(
                    errors=["requires human approval (request_id=req-9)"],
                    failure_kind="awaiting_approval",
                    request_id="req-9",
                )
            return _result(success=True, summary="fine")

        nodes = [
            TaskNode(task_id="t1", name="T1", agent_type="builder"),
            TaskNode(task_id="t2", name="T2", agent_type="builder"),
        ]
        orch, dag = _build(executor, nodes=nodes, config=_fast())
        status = orch.run(max_steps=100)

        assert status == OrchestratorStatus.AWAITING_APPROVAL
        assert dag._nodes["t1"].status == TaskStatus.AWAITING_APPROVAL
        assert dag._nodes["t2"].status == TaskStatus.COMPLETED

    def test_granted_approval_requeues_and_resumes(
        self, approval_broker, approval_manager
    ):
        req = _request_approval(approval_manager)
        calls = {"n": 0}

        def executor(task: TaskRecord) -> AgentResult:
            calls["n"] += 1
            if calls["n"] == 1:
                return _result(
                    errors=[f"Git mutation requires human approval "
                            f"(request_id={req.request_id})"],
                    failure_kind="awaiting_approval",
                    request_id=req.request_id,
                )
            return _result(success=True, summary="committed")

        audit = _AuditRecorder()
        orch, dag = _build(
            executor, audit=audit, config=_fast(), broker=approval_broker
        )

        # Pass 1: task parks on the PENDING approval.
        status = orch.run(max_steps=100)
        assert status == OrchestratorStatus.AWAITING_APPROVAL
        assert dag._nodes["t1"].status == TaskStatus.AWAITING_APPROVAL
        assert calls["n"] == 1

        # Human grants the approval in the DB.
        _decide(approval_manager, req.request_id, ApprovalStatus.APPROVED)

        # Pass 2: re-verified requeue → task resumes and completes.
        status = orch.run(max_steps=100)
        assert status == OrchestratorStatus.IDLE
        assert dag._nodes["t1"].status == TaskStatus.COMPLETED
        assert calls["n"] == 2
        assert audit.payloads("task.approval_granted_requeued")
        requeue = audit.payloads("task.approval_granted_requeued")[0]
        assert requeue["approval_request_id"] == req.request_id

    def test_pending_approval_never_requeues(self, approval_broker, approval_manager):
        req = _request_approval(approval_manager)  # left PENDING

        def executor(task: TaskRecord) -> AgentResult:
            return _result(
                errors=[f"requires human approval (request_id={req.request_id})"],
                failure_kind="awaiting_approval",
                request_id=req.request_id,
            )

        orch, dag = _build(
            executor, config=_fast(), broker=approval_broker
        )
        status = orch.run(max_steps=50)
        assert status == OrchestratorStatus.AWAITING_APPROVAL
        assert dag._nodes["t1"].status == TaskStatus.AWAITING_APPROVAL

    def test_denied_approval_terminates_task(self, approval_broker, approval_manager):
        req = _request_approval(approval_manager)
        _decide(approval_manager, req.request_id, ApprovalStatus.DENIED)

        def executor(task: TaskRecord) -> AgentResult:
            return _result(
                errors=[f"requires human approval (request_id={req.request_id})"],
                failure_kind="awaiting_approval",
                request_id=req.request_id,
            )

        audit = _AuditRecorder()
        orch, dag = _build(
            executor, audit=audit, config=_fast(), broker=approval_broker
        )
        status = orch.run(max_steps=100)

        assert dag._nodes["t1"].status == TaskStatus.FAILED
        assert dag._nodes["t1"].metadata["terminal_failure"] is True
        assert audit.payloads("task.approval_denied")
        assert status in (OrchestratorStatus.IDLE, OrchestratorStatus.STOPPED)

    def test_legacy_null_fingerprint_never_resumes(
        self, approvals_db, approval_broker, approval_manager
    ):
        req = _request_approval(approval_manager)
        _decide(approval_manager, req.request_id, ApprovalStatus.APPROVED)
        conn = sqlite3.connect(str(approvals_db))
        conn.execute(
            "UPDATE approvals SET operation_fingerprint = NULL WHERE request_id = ?",
            (req.request_id,),
        )
        conn.commit()
        conn.close()

        def executor(task: TaskRecord) -> AgentResult:
            return _result(
                errors=[f"requires human approval (request_id={req.request_id})"],
                failure_kind="awaiting_approval",
                request_id=req.request_id,
            )

        orch, dag = _build(
            executor, config=_fast(), broker=approval_broker
        )
        status = orch.run(max_steps=50)
        assert status == OrchestratorStatus.AWAITING_APPROVAL
        assert dag._nodes["t1"].status == TaskStatus.AWAITING_APPROVAL

    def test_tampered_fingerprint_never_resumes(
        self, approvals_db, approval_broker, approval_manager
    ):
        req = _request_approval(approval_manager)
        _decide(approval_manager, req.request_id, ApprovalStatus.APPROVED)
        conn = sqlite3.connect(str(approvals_db))
        conn.execute(
            "UPDATE approvals SET operation_fingerprint = ? WHERE request_id = ?",
            ("00001111222233334444555566667777", req.request_id),
        )
        conn.commit()
        conn.close()

        def executor(task: TaskRecord) -> AgentResult:
            return _result(
                errors=[f"requires human approval (request_id={req.request_id})"],
                failure_kind="awaiting_approval",
                request_id=req.request_id,
            )

        orch, dag = _build(
            executor, config=_fast(), broker=approval_broker
        )
        status = orch.run(max_steps=50)
        assert status == OrchestratorStatus.AWAITING_APPROVAL
        assert dag._nodes["t1"].status == TaskStatus.AWAITING_APPROVAL


class TestApprovalState:
    def test_no_broker_is_unverified(self, tmp_path):
        dag = TaskDAG()
        orch = Orchestrator(dag, config=_fast())
        assert orch._approval_state("req-1") == "unverified"

    def test_broker_without_manager_is_unverified(self):
        dag = TaskDAG()
        orch = Orchestrator(dag, config=_fast(), permission_broker=object())
        assert orch._approval_state("req-1") == "unverified"

    def test_missing_row(self, approval_broker):
        dag = TaskDAG()
        orch = Orchestrator(dag, config=_fast(), permission_broker=approval_broker)
        assert orch._approval_state("no-such-id") == "missing"

    def test_pending_row(self, approval_broker, approval_manager):
        req = _request_approval(approval_manager)
        dag = TaskDAG()
        orch = Orchestrator(dag, config=_fast(), permission_broker=approval_broker)
        assert orch._approval_state(req.request_id) == "pending"

    def test_approved_row_with_verified_fingerprint(
        self, approval_broker, approval_manager
    ):
        req = _request_approval(approval_manager)
        _decide(approval_manager, req.request_id, ApprovalStatus.APPROVED)
        dag = TaskDAG()
        orch = Orchestrator(dag, config=_fast(), permission_broker=approval_broker)
        assert orch._approval_state(req.request_id) == "approved"

    def test_denied_row(self, approval_broker, approval_manager):
        req = _request_approval(approval_manager)
        _decide(approval_manager, req.request_id, ApprovalStatus.DENIED)
        dag = TaskDAG()
        orch = Orchestrator(dag, config=_fast(), permission_broker=approval_broker)
        assert orch._approval_state(req.request_id) == "denied"
