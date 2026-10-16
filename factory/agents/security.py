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

    Success is decided by the deterministic scan alone (COR-007): the scan
    must complete, its evidence must be well-formed, and it must report zero
    critical/high findings (the QualityGate ``max_security_findings = 0``
    rule). The LLM evaluation (approved flag, vulnerabilities) is recorded
    in the report as informational evidence and never flips the decision
    in either direction.
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
                agent_id=self.agent_type,
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

        # ------------------------------------------------------------------
        # COR-007: the deterministic scan decides success; the LLM verdict is
        # informational only (same principle as the COR-002 fix in qa.py).
        # The rule mirrors QualityGate's default (max_security_findings = 0):
        # the scan must complete, its evidence must be well-formed, and it
        # must report zero critical/high findings. An approving LLM cannot
        # override a dirty scan, and a rejecting LLM cannot fail a clean one.
        #
        # The decision is computed BEFORE the report is written so that the
        # persisted report, the AgentResult and the emitted event always
        # agree on it.
        # ------------------------------------------------------------------
        critical = scan_result_data.get("critical_count")
        high = scan_result_data.get("high_count")
        evidence_present = (
            isinstance(critical, int)
            and isinstance(high, int)
            and critical >= 0
            and high >= 0
        )

        if not result.success:
            deterministic_pass = False
            decision_errors = [
                "Security scan did not complete: "
                + (result.error or "no scan evidence produced.")
            ]
        elif not evidence_present:
            deterministic_pass = False
            decision_errors = [
                "Malformed security-scan evidence: critical_count/high_count "
                "missing from the scan result."
            ]
        elif critical + high > 0:
            deterministic_pass = False
            high_findings = [
                f
                for f in (scan_result_data.get("findings") or [])
                if isinstance(f, dict)
                and f.get("severity") in ("high", "critical")
            ]
            decision_errors = [
                f"{f.get('path', '?')}: "
                + ("; ".join(f.get("reasons") or []) or "sensitive data")
                + f" [{f.get('severity')}]"
                for f in high_findings
            ] or ["Security scan reported critical/high findings."]
        else:
            deterministic_pass = True
            decision_errors = []

        findings_count = scan_result_data.get("finding_count", 0)

        report = {
            "task_id": task.task_id,
            "project_id": task.project_id,
            "scan_target": scan_target,
            # Legacy field, kept for compatibility: historically the scanner
            # execution status. It is True whenever the deterministic scan
            # RAN — even when blocking findings were discovered. Use
            # `deterministic_approved` for the release decision and
            # `scan_completed` for the execution status.
            "success": result.success,
            "scan_completed": result.success,
            "deterministic_approved": deterministic_pass,
            "llm_approved": proposal.approved,
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
            "approved": deterministic_pass,
            "llm_approved": proposal.approved,
        })

        outputs = {
            "scan_result": scan_result_data,
            "report_path": str(self._artifact_path("security_report.json")),
            "approved": deterministic_pass,
            "llm_approved": proposal.approved,
            "findings_count": findings_count,
        }
        evidence = [{"kind": "security_report", "path": "security_report.json"}]

        if deterministic_pass:
            return self._ok(
                task,
                summary=proposal.summary,
                outputs=outputs,
                evidence=evidence,
            )
        return AgentResult(
            agent_id=self.agent_id,
            task_id=task.task_id,
            project_id=task.project_id or self._project_id,
            success=False,
            summary=proposal.summary,
            outputs=outputs,
            errors=decision_errors,
            evidence=evidence,
        )
