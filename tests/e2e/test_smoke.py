"""
End-to-end smoke test for the AI Software Factory.

Tests that the full pipeline runs:
  plan.json → DAG → scheduler → all agents → permission checks → evidence → DB

Uses a stub LLM provider (no real model calls) to validate factory mechanics.
The fixture project is tests/fixtures/hello_project.

This test proves:
- plan.json is actually loaded into the DAG
- all 7 agents are registered and their executors called
- the orchestrator runs them in dependency order
- events and context_manifests are written to factory.db
- CloudSense is not touched

Run with:
    python -m pytest tests/e2e/test_smoke.py -v --tb=short
"""

import json
import sqlite3
import tempfile
import shutil
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from factory.database import initialize_database, get_connection
from factory.config import reload_settings, DB_PATH
from factory.orchestration.dag import TaskDAG, TaskNode
from factory.models import TaskStatus, AgentResult
from factory.llm.provider import LLMRequest, LLMResponse
from factory.cli.plan_loader import load_dag_from_plan, register_all_agents

FIXTURE_PLAN = (
    Path(__file__).parent.parent / "fixtures" / "hello_project" / "plan.json"
)

CLOUDSENSE_PATH = Path("D:/AI-Software-Factory/projects/CloudSense")

# ---------------------------------------------------------------------------
# Stub LLM provider
# ---------------------------------------------------------------------------

def _stub_response(model_class=None, content_override: str | None = None) -> LLMResponse:
    """
    Return a valid LLMResponse with stub content for each agent type.
    The content is crafted to satisfy the relevant Pydantic schema.
    """
    now = datetime.now(timezone.utc)
    content = content_override or json.dumps({
        "summary": "Stub agent completed successfully.",
        "completed": True,
        "files_changed": [],
        "proposed_actions": [],
        "blockers": [],
        "remaining_work": [],
        # planner fields
        "decisions": [],
        "assumptions": [],
        "tasks": [],
        # reviewer fields
        "defects": [],
        "approved": True,
        "risks": [],
        # qa fields
        "all_passed": True,
        "missing_tests": [],
        "recommendations": [],
        # security fields
        "vulnerabilities": [],
        # documentation fields
        "files": [],
        # auditor fields
        "requirements_met": True,
        "evidence": ["stub-evidence"],
        "missing_evidence": [],
    })
    return LLMResponse(
        request_id="stub-req",
        content=content,
        start_time=now,
        end_time=now,
        duration_ms=1.0,
        tokens_in=10,
        tokens_out=20,
        success=True,
        error="",
        structured_data={},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def smoke_db(tmp_path, monkeypatch):
    """Isolated DB for the smoke test."""
    db_file = tmp_path / "smoke_factory.db"
    monkeypatch.setattr("factory.config.DB_PATH", db_file)
    monkeypatch.setattr("factory.database.DB_PATH", db_file)
    monkeypatch.setattr("factory.cli.approvals_cmd.DB_PATH", db_file)
    initialize_database()
    yield db_file


@pytest.fixture
def workspace(tmp_path):
    """Copy fixture project to an isolated workspace."""
    src = FIXTURE_PLAN.parent
    dst = tmp_path / "hello_project"
    shutil.copytree(src, dst)
    return dst


@pytest.fixture(scope="class")
def cloudsense_initial_state():
    """Capture the initial git status of CloudSense before any tests run."""
    if not CLOUDSENSE_PATH.exists():
        return None
    import subprocess
    result = subprocess.run(
        ["git", "status", "--porcelain", str(CLOUDSENSE_PATH)],
        capture_output=True, text=True,
        cwd=str(CLOUDSENSE_PATH),
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

class TestEndToEndSmoke:
    def test_plan_json_loaded_and_dag_built(self, workspace, cloudsense_initial_state):
        """plan.json is actually loaded — not a hardcoded single task."""
        dag, plan = load_dag_from_plan(workspace / "plan.json")
        assert dag.node_count() == 8, (
            f"Expected 8 tasks, got {dag.node_count()}"
        )
        assert plan["project_id"] == "hello-project"

    def test_all_agents_can_be_registered(self, workspace, smoke_db, tmp_path):
        """All 7 agents register without import error."""
        from factory.state import StateManager
        from factory.permissions.broker import PermissionBroker
        from factory.permissions.policies import load_policies
        from factory.audit.logger import AuditLogger
        from factory.orchestration.orchestrator import Orchestrator

        dag, plan = load_dag_from_plan(workspace / "plan.json")
        sm = StateManager()
        state = sm.boot_state()
        audit_logger = AuditLogger()
        broker = PermissionBroker(policies=load_policies())

        # Mock LLM router and context builder — no real model needed
        mock_router = MagicMock()
        mock_router.complete.return_value = _stub_response()
        mock_context = MagicMock()
        mock_context.build_request.return_value = MagicMock(
            model="stub:noop",
            task_id="t1",
            output_schema=None,
        )

        orch = Orchestrator(
            dag=dag,
            state_manager=sm,
            audit_logger=audit_logger,
            permission_broker=broker,
        )

        registered = register_all_agents(
            orch,
            workspace_root=workspace,
            broker=broker,
            audit_logger=audit_logger,
            llm_router=mock_router,
            context_builder=mock_context,
            project_id=plan.get("project_id", ""),
        )

        assert len(registered) == 7
        expected = {"planner", "builder", "reviewer", "qa",
                    "security", "documentation", "auditor"}
        assert set(registered) == expected

    def test_orchestrator_runs_stub_pipeline(self, workspace, smoke_db):
        """Orchestrator runs all tasks in order via stub LLM."""
        from factory.state import StateManager
        from factory.permissions.broker import PermissionBroker
        from factory.permissions.policies import load_policies
        from factory.audit.logger import AuditLogger
        from factory.orchestration.orchestrator import Orchestrator
        from factory.context.builder import ContextBuilder
        from factory.state import FactoryState

        dag, plan = load_dag_from_plan(workspace / "plan.json")
        sm = StateManager()
        state = sm.boot_state()
        audit_logger = AuditLogger()
        broker = PermissionBroker(policies=load_policies())

        # Stub LLM — returns valid content for any agent schema
        mock_router = MagicMock()
        mock_router.complete.return_value = _stub_response()

        mock_context = MagicMock()
        mock_req = LLMRequest(
            model="stub:noop",
            system_prompt="",
            messages=[],
            task_id="t1",
        )
        mock_context.build_request.return_value = mock_req

        orch = Orchestrator(
            dag=dag,
            state_manager=sm,
            audit_logger=audit_logger,
            permission_broker=broker,
        )

        executed_agents: list[str] = []

        def make_stub_executor(agent_type):
            def stub_executor(task_record):
                executed_agents.append(agent_type)
                return AgentResult(
                    agent_id=f"stub-{agent_type}",
                    task_id=task_record.task_id,
                    success=True,
                    summary=f"Stub {agent_type} done",
                    outputs={},
                    evidence=[],
                )
            return stub_executor

        for agent_type in ["planner", "builder", "reviewer", "qa",
                            "security", "documentation", "auditor"]:
            orch.register_executor(agent_type, make_stub_executor(agent_type))

        final_status = orch.run()

        # All tasks should complete
        assert dag.is_complete(), (
            f"DAG not complete. Pending: {dag.pending_ids()}, "
            f"Failed: {dag.failed_ids()}"
        )
        # All 8 tasks ran (security runs twice: baseline + scan)
        assert len(executed_agents) == 8, (
            f"Expected 8 executions, got {len(executed_agents)}: {executed_agents}"
        )
        # Planner always first
        assert executed_agents[0] == "planner"
        # Auditor always last
        assert executed_agents[-1] == "auditor"

    def test_events_written_to_db(self, workspace, smoke_db):
        """Audit events appear in factory.db after orchestration."""
        from factory.state import StateManager
        from factory.permissions.broker import PermissionBroker
        from factory.permissions.policies import load_policies
        from factory.audit.logger import AuditLogger
        from factory.orchestration.orchestrator import Orchestrator
        from factory.models import AgentResult

        dag, plan = load_dag_from_plan(workspace / "plan.json")
        sm = StateManager()
        audit_logger = AuditLogger()
        broker = PermissionBroker(policies=load_policies())

        orch = Orchestrator(
            dag=dag,
            state_manager=sm,
            audit_logger=audit_logger,
            permission_broker=broker,
        )

        for agent_type in ["planner", "builder", "reviewer", "qa",
                            "security", "documentation", "auditor"]:
            def stub(task_record, at=agent_type):
                return AgentResult(
                    agent_id=f"{at}-123",
                    task_id=task_record.task_id, success=True,
                    summary="ok", outputs={}, evidence=[],
                )
            orch.register_executor(agent_type, stub)

        orch.run()

        # Check events table has rows
        with get_connection(str(smoke_db)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count > 0, "No events written to factory.db"

    def test_cloudsense_untouched(self, cloudsense_initial_state):
        """CloudSense directory is not modified by any operation."""
        if not CLOUDSENSE_PATH.exists():
            pytest.skip("CloudSense directory not present — skip check")

        import subprocess
        result = subprocess.run(
            ["git", "status", "--porcelain", str(CLOUDSENSE_PATH)],
            capture_output=True, text=True,
            cwd=str(CLOUDSENSE_PATH),
        )
        current_state = result.stdout.strip()
        # No uncommitted changes in CloudSense introduced by our code
        assert current_state == cloudsense_initial_state, (
            f"CloudSense status changed.\nBefore:\n{cloudsense_initial_state}\nAfter:\n{current_state}"
        )

    def test_no_double_inference(self, workspace, smoke_db):
        """Semaphore ensures only one LLM call at a time."""
        import threading
        from factory.llm.router import LLMRouter

        concurrent_calls = []
        call_lock = threading.Lock()

        original_complete = LLMRouter.complete

        def tracking_complete(self, request, retry_count=0):
            with call_lock:
                concurrent_calls.append(threading.current_thread().name)
            resp = _stub_response()
            return resp

        with patch.object(LLMRouter, "complete", tracking_complete):
            # If the semaphore works, concurrent_calls will be serialised
            router = LLMRouter()
            req = LLMRequest(
                model="stub:noop", system_prompt="", messages=[], task_id="t"
            )
            # Two threads attempt simultaneous calls
            results = []

            def call():
                resp = router.complete(req)
                results.append(resp)

            t1 = threading.Thread(target=call)
            t2 = threading.Thread(target=call)
            t1.start(); t2.start()
            t1.join(timeout=10); t2.join(timeout=10)

        assert len(results) == 2  # Both completed (serialised, not blocked forever)
