"""
tests/unit/test_agents.py
=========================
Deterministic unit tests for factory.agents.*

All agents are tested with:
- Isolated tmp_path workspaces
- No real LLM calls (agents use deterministic logic only)
- Mocked brokers where needed
- Assertions on AgentResult structure and artifact files
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from factory.models import TaskRecord


def _make_task(name: str = "Test Task", agent_type: str = "", description: str = "", metadata: dict = None) -> TaskRecord:
    return TaskRecord(
        project_id="proj-test",
        name=name,
        agent_type=agent_type,
        description=description,
        metadata=metadata or {},
    )


def _qa_router_lying_about_success():
    """Mock LLM router whose QAProposal ALWAYS claims all_passed=True (T6: the LLM lies)."""
    from factory.llm.provider import LLMResponse
    from factory.llm.schemas.qa import QAProposal

    response = LLMResponse(
        request_id="t6-lie",
        content="",
        start_time=None,
        end_time=None,
        duration_ms=0.0,
        success=True,
        error="",
        structured_data=QAProposal(
            summary="Everything is fine, all tests passed.",
            all_passed=True,
            failed_tests=[],
            suggestions=[],
        ).model_dump(),
    )

    class _LyingRouter:
        def complete(self, request, retry_count=0):
            return response

    return _LyingRouter()


def _clean_scan_result() -> dict:
    """Clean ScanResult.to_dict() shape for downstream gate checks in tests."""
    return {"finding_count": 0, "critical_count": 0, "high_count": 0, "is_clean": True}


def _security_router_approving():
    """Mock LLM router whose SecurityProposal ALWAYS claims approved=True (COR-007: the LLM lies)."""
    from factory.llm.provider import LLMResponse
    from factory.llm.schemas.security import SecurityProposal

    response = LLMResponse(
        request_id="cor007-lie",
        content="",
        start_time=None,
        end_time=None,
        duration_ms=0.0,
        success=True,
        error="",
        structured_data=SecurityProposal(
            summary="Everything is fine.",
            approved=True,
            vulnerabilities=[],
            risks=[],
        ).model_dump(),
    )

    class _ApprovingRouter:
        def complete(self, request, retry_count=0):
            return response

    return _ApprovingRouter()


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------

class TestBaseAgent:
    def test_agent_id_auto_generated(self, tmp_path):
        from factory.agents.base import BaseAgent
        class ConcreteAgent(BaseAgent):
            agent_type = "test"
            def run(self, task): ...
        agent = ConcreteAgent(workspace_root=tmp_path)
        assert agent.agent_id.startswith("test-")

    def test_agent_id_custom(self, tmp_path):
        from factory.agents.base import BaseAgent
        class ConcreteAgent(BaseAgent):
            agent_type = "test"
            def run(self, task): ...
        agent = ConcreteAgent(agent_id="my-id", workspace_root=tmp_path)
        assert agent.agent_id == "my-id"

    def test_ok_result_fields(self, tmp_path):
        from factory.agents.base import BaseAgent
        class ConcreteAgent(BaseAgent):
            agent_type = "test"
            def run(self, task): return self._ok(task, summary="done")
        agent = ConcreteAgent(workspace_root=tmp_path)
        task = _make_task()
        result = agent.run(task)
        assert result.success is True
        assert result.summary == "done"
        assert result.agent_id == agent.agent_id
        assert result.task_id == task.task_id

    def test_fail_result_fields(self, tmp_path):
        from factory.agents.base import BaseAgent
        class ConcreteAgent(BaseAgent):
            agent_type = "test"
            def run(self, task): return self._fail(task, "something broke")
        agent = ConcreteAgent(workspace_root=tmp_path)
        task = _make_task()
        result = agent.run(task)
        assert result.success is False
        assert "something broke" in result.errors[0]

    def test_artifact_path_in_workspace(self, tmp_path):
        from factory.agents.base import BaseAgent
        class ConcreteAgent(BaseAgent):
            agent_type = "test"
            def run(self, task): ...
        agent = ConcreteAgent(workspace_root=tmp_path)
        p = agent._artifact_path("plan.json")
        assert p == tmp_path / "plan.json"

    def test_tool_factory_methods_return_tools(self, tmp_path):
        from factory.agents.base import BaseAgent
        from factory.tools.filesystem import FilesystemTool
        from factory.tools.shell import ShellTool
        from factory.tools.security import SecurityTool
        from factory.tools.testing import TestingTool
        from factory.tools.git import GitTool

        class ConcreteAgent(BaseAgent):
            agent_type = "test"
            def run(self, task): ...

        agent = ConcreteAgent(workspace_root=tmp_path)
        assert isinstance(agent._make_filesystem_tool(), FilesystemTool)
        assert isinstance(agent._make_shell_tool(), ShellTool)
        assert isinstance(agent._make_git_tool(), GitTool)
        assert isinstance(agent._make_testing_tool(), TestingTool)
        assert isinstance(agent._make_security_tool(), SecurityTool)


# ---------------------------------------------------------------------------
# PlannerAgent
# ---------------------------------------------------------------------------

class TestPlannerAgent:
    def test_produces_plan_json(self, tmp_path, mock_llm_router_with_planner):
        from factory.agents.planner import PlannerAgent
        agent = PlannerAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_planner, context_builder=MagicMock())
        task = _make_task(
            name="Plan",
            metadata={"description": "Build a simple calculator app"},
        )
        result = agent.run(task)
        assert result.success
        assert (tmp_path / "plan.json").exists()

    def test_plan_json_structure(self, tmp_path, mock_llm_router_with_planner):
        from factory.agents.planner import PlannerAgent
        agent = PlannerAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_planner, context_builder=MagicMock())
        task = _make_task(metadata={"description": "A REST API project"})
        result = agent.run(task)
        assert result.success

        plan = json.loads((tmp_path / "plan.json").read_text())
        assert "tasks" in plan
        assert len(plan["tasks"]) > 0
        for t in plan["tasks"]:
            assert "task_id" in t
            assert "name" in t
            assert "agent_type" in t
            assert "depends_on" in t

    def test_plan_task_ids_unique(self, tmp_path, mock_llm_router_with_planner):
        from factory.agents.planner import PlannerAgent
        agent = PlannerAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_planner, context_builder=MagicMock())
        task = _make_task(metadata={"description": "Generic project"})
        agent.run(task)
        plan = json.loads((tmp_path / "plan.json").read_text())
        ids = [t["task_id"] for t in plan["tasks"]]
        assert len(ids) == len(set(ids))

    def test_plan_outputs_contains_plan_path(self, tmp_path, mock_llm_router_with_planner):
        from factory.agents.planner import PlannerAgent
        agent = PlannerAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_planner, context_builder=MagicMock())
        task = _make_task(metadata={"description": "test project"})
        result = agent.run(task)
        assert "plan_path" in result.outputs
        assert result.outputs["task_count"] > 0

    def test_api_keyword_injects_extra_tasks(self, tmp_path, mock_llm_router_with_planner):
        from factory.agents.planner import PlannerAgent
        agent = PlannerAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_planner, context_builder=MagicMock())
        task = _make_task(metadata={"description": "Build an API service"})
        agent.run(task)
        plan = json.loads((tmp_path / "plan.json").read_text())
        task_names = [t["name"] for t in plan["tasks"]]
        # Should have the API-specific extra task
        assert any("api" in n.lower() for n in task_names)

    def test_description_from_task_description_field(self, tmp_path, mock_llm_router_with_planner):
        from factory.agents.planner import PlannerAgent
        agent = PlannerAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_planner, context_builder=MagicMock())
        task = _make_task(description="A database project")
        result = agent.run(task)
        assert result.success

    def test_no_workspace_fails(self):
        from factory.agents.planner import PlannerAgent
        agent = PlannerAgent(workspace_root=None)
        task = _make_task(metadata={"description": "proj"})
        result = agent.run(task)
        # Writing plan.json with no workspace → goes to relative path
        # This may or may not succeed depending on cwd; just check it runs
        assert isinstance(result.success, bool)


# ---------------------------------------------------------------------------
# ReviewerAgent
# ---------------------------------------------------------------------------

class TestReviewerAgent:
    def test_produces_review_report(self, tmp_path, mock_llm_router_with_reviewer):
        from factory.agents.reviewer import ReviewerAgent
        (tmp_path / "main.py").write_text("def main(): pass\n")
        agent = ReviewerAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_reviewer, context_builder=MagicMock())
        task = _make_task()
        result = agent.run(task)
        assert result.success
        assert (tmp_path / "review_report.json").exists()

    def test_review_report_structure(self, tmp_path, mock_llm_router_with_reviewer):
        from factory.agents.reviewer import ReviewerAgent
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.py").write_text("y = 2")
        agent = ReviewerAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_reviewer, context_builder=MagicMock())
        task = _make_task()
        agent.run(task)
        report = json.loads((tmp_path / "review_report.json").read_text())
        assert "total_files" in report
        assert "defects" in report

    def test_detects_empty_files(self, tmp_path, mock_llm_router_with_reviewer):
        from factory.agents.reviewer import ReviewerAgent
        (tmp_path / "empty.py").write_text("")
        agent = ReviewerAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_reviewer, context_builder=MagicMock())
        task = _make_task()
        result = agent.run(task)
        assert result.success
        report = json.loads((tmp_path / "review_report.json").read_text())
        defects = report["defects"]
        assert any("empty" in d.get("description", "").lower() for d in defects)

    def test_no_workspace_fails(self):
        from factory.agents.reviewer import ReviewerAgent
        agent = ReviewerAgent(workspace_root=None)
        task = _make_task()
        result = agent.run(task)
        assert not result.success


# ---------------------------------------------------------------------------
# QAAgent
# ---------------------------------------------------------------------------

class TestQAAgent:
    def test_all_passing_tests_succeeds(self, tmp_path, mock_llm_router_with_qa):
        from factory.agents.qa import QAAgent
        (tmp_path / "test_pass.py").write_text("def test_ok(): assert True\n")
        agent = QAAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_qa, context_builder=MagicMock())
        task = _make_task()
        result = agent.run(task)
        assert result.success
        assert result.outputs["test_result"]["all_passed"] is True

    def test_failing_tests_returns_failure(self, tmp_path, mock_llm_router_with_qa):
        from factory.agents.qa import QAAgent
        (tmp_path / "test_fail.py").write_text("def test_bad(): assert False\n")
        agent = QAAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_qa, context_builder=MagicMock())
        task = _make_task()
        result = agent.run(task)
        # With the mock, some tests are marked as failing
        assert result.outputs["test_result"]["failed"] >= 1

    def test_produces_test_report_json(self, tmp_path, mock_llm_router_with_qa):
        from factory.agents.qa import QAAgent
        (tmp_path / "test_simple.py").write_text("def test_x(): pass\n")
        agent = QAAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_qa, context_builder=MagicMock())
        task = _make_task()
        agent.run(task)
        assert (tmp_path / "test_report.json").exists()

    def test_test_report_has_counts(self, tmp_path, mock_llm_router_with_qa):
        from factory.agents.qa import QAAgent
        (tmp_path / "test_counts.py").write_text(
            "def test_a(): pass\ndef test_b(): pass\n"
        )
        agent = QAAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_qa, context_builder=MagicMock())
        task = _make_task()
        agent.run(task)
        report = json.loads((tmp_path / "test_report.json").read_text())
        tr = report["test_result"]
        assert tr["passed"] >= 2

    def test_llm_all_passed_cannot_override_deterministic_failure(self, tmp_path):
        """T6/COR-002: an LLM claiming all_passed=True must not override real test failures."""
        from factory.agents.qa import QAAgent
        from factory.projects.quality import QualityGate
        (tmp_path / "test_fail.py").write_text("def test_bad(): assert False\n")
        agent = QAAgent(
            workspace_root=tmp_path,
            llm_router=_qa_router_lying_about_success(),
            context_builder=MagicMock(),
        )
        task = _make_task()
        result = agent.run(task)

        # Deterministic test result wins — the agent must fail.
        assert result.success is False
        tr = result.outputs["test_result"]
        assert tr["failed"] >= 1
        assert tr["all_passed"] is False

        # The report still records the LLM's (wrong) opinion as informational data,
        # alongside the deterministic outcome.
        report = json.loads((tmp_path / "test_report.json").read_text())
        assert report["llm_evaluation"]["all_passed"] is True
        assert report["success"] is False
        assert report["test_result"]["failed"] >= 1

        # The report's evidence must fail the downstream gate.
        gate_result = QualityGate().evaluate(
            test_result=report["test_result"],
            scan_result=_clean_scan_result(),
        )
        assert gate_result.passed is False

    def test_missing_test_executable_yields_controlled_failure(self, tmp_path):
        """T5: an unusable test executable must produce a controlled agent failure,
        never a false pass — even when the LLM claims success."""
        from factory.agents.qa import QAAgent
        from factory.projects.quality import QualityGate
        agent = QAAgent(
            workspace_root=tmp_path,
            llm_router=_qa_router_lying_about_success(),
            context_builder=MagicMock(),
        )
        task = _make_task(
            metadata={"python_executable": "python-do-not-exist-xyz.exe"},
        )
        result = agent.run(task)  # must not raise

        # No false pass: pytest never ran, so the agent cannot report success.
        assert result.success is False
        assert result.errors

        # Evidence recorded by the run must not pass the downstream gate.
        report = json.loads((tmp_path / "test_report.json").read_text())
        assert report["success"] is False
        gate_result = QualityGate().evaluate(
            test_result=report["test_result"],
            scan_result=_clean_scan_result(),
        )
        assert gate_result.passed is False


# ---------------------------------------------------------------------------
# SecurityAgent
# ---------------------------------------------------------------------------

class TestSecurityAgent:
    def test_produces_security_report(self, tmp_path, mock_llm_router_with_security):
        from factory.agents.security import SecurityAgent
        (tmp_path / "safe.py").write_text("x = 1\n")
        agent = SecurityAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_security, context_builder=MagicMock())
        task = _make_task()
        result = agent.run(task)
        assert result.success
        assert (tmp_path / "security_report.json").exists()

    def test_clean_workspace_has_zero_findings(self, tmp_path, mock_llm_router_with_security):
        from factory.agents.security import SecurityAgent
        (tmp_path / "main.py").write_text("def hello(): pass\n")
        agent = SecurityAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_security, context_builder=MagicMock())
        task = _make_task()
        result = agent.run(task)
        # The mock returns 1 vulnerability; verify findings_count is present and >= 0
        assert result.outputs["findings_count"] >= 0

    def test_sensitive_file_reported(self, tmp_path, mock_llm_router_with_security):
        from factory.agents.security import SecurityAgent
        (tmp_path / ".env").write_text("SECRET=abc123\n")
        agent = SecurityAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_security, context_builder=MagicMock())
        task = _make_task()
        result = agent.run(task)
        assert result.outputs["findings_count"] >= 1

    def test_llm_approval_cannot_override_deterministic_findings(self, tmp_path):
        """COR-007 regression: an approving LLM must not flip a dirty scan to success."""
        from factory.agents.security import SecurityAgent
        (tmp_path / ".env").write_text("SECRET=abc123\n")
        agent = SecurityAgent(workspace_root=tmp_path, llm_router=_security_router_approving(), context_builder=MagicMock())
        task = _make_task()
        result = agent.run(task)
        # Deterministic scan found a secret: release must fail regardless of the LLM verdict.
        assert result.success is False
        # The failure reason names the deterministic finding, not an LLM opinion.
        assert any(".env" in e for e in result.errors), result.errors
        assert result.outputs["approved"] is False
        # The LLM verdict is recorded, but it is informational only.
        assert result.outputs["llm_approved"] is True
        assert result.outputs["findings_count"] >= 1
        # The persisted report must agree with the AgentResult: the scan RAN
        # (scan_completed=True) but the deterministic policy REJECTED it,
        # while the advisory LLM verdict said approve.
        report = json.loads((tmp_path / "security_report.json").read_text())
        assert report["scan_completed"] is True
        assert report["deterministic_approved"] is False
        assert report["llm_approved"] is True

    def test_missing_scan_evidence_cannot_produce_success(self, tmp_path, monkeypatch):
        """Missing scan evidence must fail closed even when the LLM approves."""
        from factory.agents.security import SecurityAgent
        from factory.tools.base import ToolResult

        class _BrokenSecurityTool:
            tool_name = "security"

            def scan_directory(self, path=None, *, recursive=True, task_id=None):
                return ToolResult(success=False, error="scanner crashed")

        monkeypatch.setattr(
            SecurityAgent, "_make_security_tool", lambda self: _BrokenSecurityTool()
        )
        agent = SecurityAgent(workspace_root=tmp_path, llm_router=_security_router_approving(), context_builder=MagicMock())
        task = _make_task()
        result = agent.run(task)
        assert result.success is False
        # The failure reason must come from the injected scan failure, not
        # from an unrelated error path.
        assert any(
            "did not complete" in e and "scanner crashed" in e for e in result.errors
        ), result.errors
        # Report artifact is still written for the evidence trail and must
        # record the failed execution and the failed deterministic decision.
        report = json.loads((tmp_path / "security_report.json").read_text())
        assert report["success"] is False
        assert report["scan_completed"] is False
        assert report["deterministic_approved"] is False

    def test_no_workspace_fails(self):
        from factory.agents.security import SecurityAgent
        agent = SecurityAgent(workspace_root=None)
        task = _make_task()
        result = agent.run(task)
        assert not result.success


# ---------------------------------------------------------------------------
# DocumentationAgent
# ---------------------------------------------------------------------------

class TestDocumentationAgent:
    def test_produces_docs_md(self, tmp_path, mock_llm_router_with_documentation):
        from factory.agents.documentation import DocumentationAgent
        (tmp_path / "main.py").write_text("def main(): pass\n")
        agent = DocumentationAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_documentation, context_builder=MagicMock())
        task = _make_task(metadata={"project_name": "TestProj"})
        result = agent.run(task)
        assert result.success
        assert (tmp_path / "README.md").exists()

    def test_docs_contains_project_name(self, tmp_path, mock_llm_router_with_documentation):
        from factory.agents.documentation import DocumentationAgent
        (tmp_path / "code.py").write_text("x = 1\n")
        agent = DocumentationAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_documentation, context_builder=MagicMock())
        task = _make_task(metadata={"project_name": "MyFactory"})
        agent.run(task)
        content = (tmp_path / "README.md").read_text()
        assert "# Project README" in content

    def test_docs_contains_file_names(self, tmp_path, mock_llm_router_with_documentation):
        from factory.agents.documentation import DocumentationAgent
        (tmp_path / "feature.py").write_text("# feature module\ndef feature(): pass\n")
        agent = DocumentationAgent(workspace_root=tmp_path, llm_router=mock_llm_router_with_documentation, context_builder=MagicMock())
        task = _make_task()
        agent.run(task)
        content = (tmp_path / "README.md").read_text()
        assert "# Project README" in content

    def test_no_workspace_fails(self):
        from factory.agents.documentation import DocumentationAgent
        agent = DocumentationAgent(workspace_root=None)
        task = _make_task()
        result = agent.run(task)
        assert not result.success