"""
factory.agents.auditor
=======================
Auditor agent — reads audit log events and produces a structured audit report.

Output artifact: <workspace>/audit_report.json
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from factory.models import AgentResult, TaskRecord
from factory.agents.base import BaseAgent

logger = logging.getLogger(__name__)

_MAX_EVENTS = 500


class AuditorAgent(BaseAgent):
    """
    Reads the audit event log and evaluates evidence using the LLM.
    """

    agent_type = "auditor"

    def run(self, task: TaskRecord) -> AgentResult:
        self._emit("agent.auditor.started", {"task_id": task.task_id})
        
        if not self._llm or not self._context:
            return self._fail(task, "LLM Router or Context Builder not provided to agent")

        project_id = task.project_id or self._project_id
        events: list[dict[str, Any]] = []

        try:
            from factory.audit.logger import list_events
            events = list_events(
                project_id=project_id,
                limit=_MAX_EVENTS,
            )
        except Exception as exc:
            logger.warning("AuditorAgent: could not load events: %s", exc)
            events = []

        # Summarise by event type for context limit
        type_counts: dict[str, int] = {}
        approval_events: list[dict[str, Any]] = []
        error_events: list[dict[str, Any]] = []
        milestone_events: list[dict[str, Any]] = []

        for event in events:
            etype = event.get("event_type", "unknown")
            type_counts[etype] = type_counts.get(etype, 0) + 1
            if "approval" in etype:
                approval_events.append(event)
            elif "fail" in etype or "error" in etype or "denied" in etype:
                error_events.append(event)
            elif "completed" in etype:
                milestone_events.append(event)
                
        import json
        evidence_str = (
            f"Event Summary: {json.dumps(type_counts)}\n"
            f"Approval Events: {json.dumps(approval_events)}\n"
            f"Error Events: {json.dumps(error_events)}\n"
            f"Milestones: {json.dumps(milestone_events)}\n"
        )
        
        system_prompt = (
            "You are the Final Auditor Agent. Your goal is to evaluate the complete execution log "
            "and determine if the project requirements were met and properly verified.\n"
            "Do NOT blindly approve. You must identify specific evidence such as test successes and reviews.\n"
            "If evidence is missing or critical errors occurred, set approved to false."
        )
        
        messages = [
            {"role": "user", "content": f"Please audit the following execution evidence:\n{evidence_str}"}
        ]
        
        from factory.llm.schemas.auditor import AuditorProposal
        
        try:
            request = self._context.build_request(
                project_id=project_id,
                system_prompt=system_prompt,
                messages=messages,
                response_schema=AuditorProposal,
            )
            response = self._llm.complete(request)
        except Exception as exc:
            return self._fail(task, f"LLM inference failed: {exc}")
            
        if not response.success:
            return self._fail(task, f"LLM failed: {response.error}")
            
        if not response.structured_output:
            return self._fail(task, "LLM returned no structured output")
            
        try:
            proposal = AuditorProposal.model_validate_json(response.structured_output)
        except Exception as exc:
            return self._fail(task, f"Failed to parse LLM output as AuditorProposal: {exc}")

        report = {
            "task_id": task.task_id,
            "project_id": project_id,
            "total_events": len(events),
            "event_type_summary": type_counts,
            "approved": proposal.approved,
            "requirements_met": proposal.requirements_met,
            "evidence": proposal.evidence,
            "missing_evidence": proposal.missing_evidence,
        }

        fs = self._make_filesystem_tool()
        write_result = fs.write_json(
            Path("audit_report.json"), report, task_id=task.task_id
        )

        if not write_result.success:
            logger.warning(
                "AuditorAgent: could not write audit_report.json: %s",
                write_result.error,
            )

        self._emit("agent.auditor.completed", {
            "task_id": task.task_id,
            "event_count": len(events),
            "approved": proposal.approved,
        })

        summary = (
            f"LLM Audit complete. Approved: {proposal.approved}. "
            f"Evidence points: {len(proposal.evidence)}. "
            f"Missing evidence: {len(proposal.missing_evidence)}."
        )

        return self._ok(
            task,
            summary=summary,
            outputs={
                "report_path": str(self._artifact_path("audit_report.json")),
                "total_events": len(events),
                "approved": proposal.approved,
                "evidence": proposal.evidence,
            },
            evidence=[{"kind": "audit_report", "path": "audit_report.json"}],
        )
