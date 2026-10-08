"""
factory.agents.security
========================
Security agent — orchestrates workspace security scanning.

Output artifact: <workspace>/security_report.json
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from factory.models import AgentResult, TaskRecord
from factory.agents.base import BaseAgent

logger = logging.getLogger(__name__)


class SecurityAgent(BaseAgent):
    """
    Runs a full security scan of the workspace using SecurityTool.

    Produces security_report.json with all findings.
    Returns success=True even with findings (findings are informational
    Runs security scanning tools and uses the LLM to evaluate the findings.
    """
    agent_type = "security"

    def run(self, task: TaskRecord) -> AgentResult:
        self._emit("agent.security.started", {"task_id": task.task_id})
        
        if not self._llm or not self._context:
            return self._fail(task, "LLM Router or Context Builder not provided to agent")

        security_tool = self._make_security_tool()
        scan_target = task.metadata.get("scan_target", ".")

        # Deterministic scan
        result = security_tool.scan_directory(
            path=Path(scan_target),
            task_id=task.task_id,
        )

        scan_result_data = result.metadata.get("scan_result", {})
        raw_output = result.output[:8192]
        
        # LLM Evaluation
        system_prompt = (
            "You are the Security Agent. Evaluate the provided security scan logs.\n"
            "Identify if there are any critical vulnerabilities or secrets exposed.\n"
            "If so, set approved to false and list vulnerabilities. Otherwise set approved to true."
        )
        
        messages = [{"role": "user", "content": f"Security Scan Output:\n{raw_output}"}]
        
        from factory.llm.schemas.security import SecurityProposal
        try:
            req = self._context.build_request(
                project_id=task.project_id or self._project_id,
                system_prompt=system_prompt,
                messages=messages,
                response_schema=SecurityProposal,
            )
            llm_res = self._llm.complete(req)
        except Exception as exc:
            return self._fail(task, f"LLM inference failed: {exc}")
            
        if not llm_res.success:
            return self._fail(task, f"LLM failed: {llm_res.error}")
            
        try:
            proposal = SecurityProposal.model_validate(llm_res.structured_data)
        except Exception as exc:
            return self._fail(task, f"Failed to parse LLM output: {exc}")

        report = {
            "task_id": task.task_id,
            "project_id": task.project_id,
            "scan_target": scan_target,
            "success": result.success,
            "returncode": result.returncode,
            "scan_result": scan_result_data,
            "llm_evaluation": proposal.model_dump(),
        }

        fs = self._make_filesystem_tool()
        write_result = fs.write_json(
            Path("security_report.json"), report, task_id=task.task_id
        )

        if not write_result.success:
            logger.warning("SecurityAgent: could not write security_report.json: %s", write_result.error)

        self._emit("agent.security.completed", {
            "task_id": task.task_id,
            "approved": proposal.approved,
        })

        if proposal.approved:
            return self._ok(
                task,
                summary=proposal.summary,
                outputs={
                    "scan_result": scan_result_data,
                    "report_path": str(self._artifact_path("security_report.json")),
                    "approved": True,
                    "findings_count": len(proposal.vulnerabilities),
                },
                evidence=[{"kind": "security_report", "path": "security_report.json"}],
            )
        else:
            vuln_strings = [v.description for v in proposal.vulnerabilities] if proposal.vulnerabilities else []
            return AgentResult(
                agent_id=self.agent_id,
                task_id=task.task_id,
                project_id=task.project_id or self._project_id,
                success=False,
                summary=proposal.summary,
                outputs={
                    "scan_result": scan_result_data,
                    "report_path": str(self._artifact_path("security_report.json")),
                    "approved": False,
                    "findings_count": len(proposal.vulnerabilities),
                },
                errors=vuln_strings or ["Security scan rejected by LLM."],
                evidence=[{"kind": "security_report", "path": "security_report.json"}],
            )
