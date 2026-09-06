"""
factory.agents.qa
==================
QA agent — runs the automated test suite and reports results.

Output artifact: <workspace>/test_report.json
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from factory.models import AgentResult, TaskRecord
from factory.agents.base import BaseAgent

logger = logging.getLogger(__name__)


class QAAgent(BaseAgent):
    """
    Runs pytest and evaluates the test results using the LLM.
    """
    agent_type = "qa"

    def run(self, task: TaskRecord) -> AgentResult:
        self._emit("agent.qa.started", {"task_id": task.task_id})
        
        if not self._llm or not self._context:
            return self._fail(task, "LLM Router or Context Builder not provided to agent")

        testing_tool = self._make_testing_tool()
        test_path = task.metadata.get("test_path", "")
        import sys as _sys
        python_exe = task.metadata.get("python_executable", _sys.executable)
        testing_tool.python_executable = python_exe

        # Deterministic execution
        result = testing_tool.run_tests(
            test_path=test_path,
            task_id=task.task_id,
        )

        test_result_data: dict[str, Any] = result.metadata.get("test_result", {})
        raw_output = result.output[:8192]
        
        # LLM Evaluation
        system_prompt = (
            "You are the QA Agent. Evaluate the provided test execution logs.\n"
            "Identify if all tests passed. If they failed, explain why.\n"
            "Suggest missing test cases or architecture improvements."
        )
        
        messages = [{"role": "user", "content": f"Test output:\n{raw_output}"}]
        
        from factory.llm.schemas.qa import QAProposal
        try:
            req = self._context.build_request(
                project_id=task.project_id or self._project_id,
                system_prompt=system_prompt,
                messages=messages,
                response_schema=QAProposal,
            )
            llm_res = self._llm.complete(req)
        except Exception as exc:
            return self._fail(task, f"LLM inference failed: {exc}")
            
        if not llm_res.success:
            return self._fail(task, f"LLM failed: {llm_res.error}")
            
        try:
            proposal = QAProposal.model_validate_json(llm_res.structured_output)
        except Exception as exc:
            return self._fail(task, f"Failed to parse LLM output: {exc}")

        report = {
            "task_id": task.task_id,
            "project_id": task.project_id,
            "test_path": test_path,
            "success": result.success,
            "returncode": result.returncode,
            "duration_ms": round(result.duration_ms, 1),
            "test_result": test_result_data,
            "llm_evaluation": proposal.model_dump(),
        }

        fs = self._make_filesystem_tool()
        write_result = fs.write_json(
            Path("test_report.json"), report, task_id=task.task_id
        )

        if not write_result.success:
            logger.warning("QAAgent: could not write test_report.json: %s", write_result.error)

        self._emit("agent.qa.completed", {
            "task_id": task.task_id,
            "all_passed": proposal.all_passed,
        })

        if proposal.all_passed:
            return self._ok(
                task,
                summary=proposal.summary,
                outputs={
                    "test_result": test_result_data,
                    "report_path": str(self._artifact_path("test_report.json")),
                    "all_passed": True,
                },
                evidence=[{"kind": "test_result", "path": "test_report.json"}],
            )
        else:
            return AgentResult(
                agent_id=self.agent_id,
                task_id=task.task_id,
                project_id=task.project_id or self._project_id,
                success=False,
                summary=proposal.summary,
                outputs={
                    "test_result": test_result_data,
                    "report_path": str(self._artifact_path("test_report.json")),
                    "all_passed": False,
                },
                errors=["Tests failed or QA rejected the test suite."],
                evidence=[{"kind": "test_result", "path": "test_report.json"}],
            )
