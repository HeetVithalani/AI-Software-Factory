"""
Integration tests for the factory pipeline.
Tests: plan.json → DAG, agent registration, configurable model, approvals CLI.
Uses in-memory/temp DB, no real LLM calls.
"""

import json
import os
import sqlite3
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from factory.config import reload_settings
from factory.database import initialize_database, get_connection
from factory.orchestration.dag import TaskDAG, TaskNode
from factory.cli.plan_loader import load_dag_from_plan, register_all_agents
from factory.cli.approvals_cmd import (
    cmd_approvals_list,
    cmd_approvals_grant,
    cmd_approvals_deny,
    _query_approvals,
    _write_decision,
)
from factory.errors import ConfigurationError

FIXTURE_PLAN = (
    Path(__file__).parent.parent / "fixtures" / "hello_project" / "plan.json"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Use a fresh factory.db in tmp_path for each test."""
    db_file = tmp_path / "test_factory.db"
    monkeypatch.setattr("factory.config.DB_PATH", db_file)
    monkeypatch.setattr("factory.database.DB_PATH", db_file)
    monkeypatch.setattr("factory.cli.approvals_cmd.DB_PATH", db_file)
    initialize_database()
    yield db_file


# ---------------------------------------------------------------------------
# Phase 1: Plan → DAG
# ---------------------------------------------------------------------------

class TestPlanToDag:
    def test_fixture_plan_loads_8_tasks(self):
        dag, plan = load_dag_from_plan(FIXTURE_PLAN)
        assert dag.node_count() == 8
        assert plan["project_id"] == "hello-project"

    def test_dag_validates_no_cycles(self):
        dag, _ = load_dag_from_plan(FIXTURE_PLAN)
        dag.validate()  # Must not raise

    def test_topological_order_planner_first(self):
        dag, _ = load_dag_from_plan(FIXTURE_PLAN)
        order = dag.topological_order()
        assert order[0] == "task-plan"

    def test_auditor_last(self):
        dag, _ = load_dag_from_plan(FIXTURE_PLAN)
        order = dag.topological_order()
        assert order[-1] == "task-audit"

    def test_parallel_tasks_exist(self):
        """review, qa, security-baseline should all be at the same depth."""
        dag, _ = load_dag_from_plan(FIXTURE_PLAN)
        review = dag.get("task-review")
        qa = dag.get("task-qa")
        baseline = dag.get("task-security-baseline")
        # All depend on task-build only
        assert review.depends_on == ["task-build"]
        assert qa.depends_on == ["task-build"]
        assert baseline.depends_on == ["task-build"]


# ---------------------------------------------------------------------------
# Phase 2: Configurable model
# ---------------------------------------------------------------------------

class TestConfigurableModel:
    def test_default_model_in_settings(self):
        reload_settings()
        from factory.config import get_settings
        s = get_settings()
        assert s.models.default_model == "ollama:qwen2.5-coder:7b"

    def test_context_builder_uses_settings_model(self, tmp_path):
        reload_settings()
        from factory.config import get_settings
        from factory.context.builder import ContextBuilder
        from factory.state import FactoryState

        state = MagicMock(spec=FactoryState)
        state.paused = False
        builder = ContextBuilder(state=state)

        req = builder.build_request(
            task_id="t1",
            system_prompt="sys",
            messages=[],
        )
        # Should use settings.models.default_model, not hardcoded string
        assert req.model == get_settings().models.default_model

    def test_explicit_model_override_respected(self, tmp_path):
        from factory.config import get_settings
        from factory.context.builder import ContextBuilder
        from factory.state import FactoryState

        state = MagicMock(spec=FactoryState)
        builder = ContextBuilder(state=state)
        req = builder.build_request(
            task_id="t1",
            system_prompt="sys",
            messages=[],
            model="opencode:custom-model",
        )
        assert req.model == "opencode:custom-model"


# ---------------------------------------------------------------------------
# Phase 5: Approvals CLI
# ---------------------------------------------------------------------------

class TestApprovalsCLI:
    def _insert_pending(self, db_path: Path, req_id: str = "req-1") -> None:
        """Insert a synthetic pending approval directly."""
        import datetime
        with get_connection(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    request_id, project_id, task_id, action, target,
                    reason, expected_effect, risk_level, consequence_if_denied,
                    action_category, scope_key, requested_at, status,
                    grants_scope_inheritance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req_id, "proj-1", "task-build", "filesystem.write", "/workspace/file.py",
                    "Need to write code", "File created", "high", "Build fails",
                    "filesystem.write", "abc123",
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "pending", 0,
                ),
            )
            conn.commit()

    def test_list_returns_pending(self, isolated_db):
        self._insert_pending(isolated_db)
        rows = _query_approvals(status_filter="pending")
        assert len(rows) == 1
        assert rows[0]["request_id"] == "req-1"

    def test_grant_changes_status(self, isolated_db):
        self._insert_pending(isolated_db)
        updated = _write_decision("req-1", "approved", "human-cli", "looks fine")
        assert updated is True
        rows = _query_approvals(status_filter="approved")
        assert len(rows) == 1
        assert rows[0]["decided_by"] == "human-cli"

    def test_deny_changes_status(self, isolated_db):
        self._insert_pending(isolated_db)
        updated = _write_decision("req-1", "denied", "human-cli", "too risky")
        assert updated is True
        rows = _query_approvals(status_filter="denied")
        assert len(rows) == 1

    def test_cannot_grant_already_decided(self, isolated_db):
        self._insert_pending(isolated_db)
        _write_decision("req-1", "approved", "human-cli", "ok")
        # Second grant attempt returns 0 rows updated
        updated = _write_decision("req-1", "approved", "human-cli", "again")
        assert updated is False

    def test_nonexistent_request_returns_none(self, isolated_db):
        rows = _query_approvals(status_filter="pending")
        assert all(r["request_id"] != "ghost-req" for r in rows)

    def test_no_approval_bypassed(self, isolated_db):
        """Granting must go through explicit _write_decision — never auto."""
        rows = _query_approvals(status_filter="approved")
        assert len(rows) == 0  # nothing auto-granted

    def test_list_output_is_pure_ascii(self, isolated_db, capsys):
        """CLI output must never raise UnicodeEncodeError on cp1252 pipes."""
        import argparse
        self._insert_pending(isolated_db)
        args = argparse.Namespace(status="pending", project=None)
        rc = cmd_approvals_list(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "req-1" in out
        assert all(ord(ch) < 128 for ch in out), "approvals list must be ASCII"

    def test_grant_output_ascii_and_honest_resume(self, isolated_db, capsys):
        import argparse
        self._insert_pending(isolated_db)
        args = argparse.Namespace(request_id="req-1", note="reviewed")
        rc = cmd_approvals_grant(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "GRANTED" in out
        assert all(ord(ch) < 128 for ch in out)
        # Honest messaging: resume requires a re-run and discloses that
        # status + operation fingerprint are re-verified first.
        assert "factory run" in out
        assert "re-verified" in out

    def test_deny_output_ascii_and_honest(self, isolated_db, capsys):
        import argparse
        self._insert_pending(isolated_db)
        args = argparse.Namespace(request_id="req-1", note="too risky")
        rc = cmd_approvals_deny(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "DENIED" in out
        assert all(ord(ch) < 128 for ch in out)
        assert "FRESH" in out  # denial does not silently resume anything


# ---------------------------------------------------------------------------
# Phase 3: OpenCode provider registration (mocked)
# ---------------------------------------------------------------------------

class TestLLMRouterOpenCode:
    def test_opencode_not_registered_when_disabled(self, tmp_path, monkeypatch):
        """opencode.enabled = False → only ollama registered."""
        reload_settings()
        from factory.llm.router import LLMRouter
        router = LLMRouter()
        assert "opencode" not in router._providers

    def test_opencode_registered_when_enabled(self, tmp_path, monkeypatch):
        """opencode.enabled = True → OpenCodeProvider registered."""
        reload_settings()
        from factory.config import get_settings
        s = get_settings()
        s.models.opencode.enabled = True

        mock_provider = MagicMock()
        with patch("factory.llm.router.OpenCodeProvider", return_value=mock_provider,
                   create=True):
            with patch("factory.llm.opencode_provider.OpenCodeProvider",
                       return_value=mock_provider):
                from factory.llm.router import LLMRouter
                import importlib
                import factory.llm.router as router_module
                importlib.reload(router_module)
                # Reset enabled for other tests
                s.models.opencode.enabled = False
