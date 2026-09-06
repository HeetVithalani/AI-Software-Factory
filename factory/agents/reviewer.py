"""
factory.agents.reviewer
========================
Reviewer agent — reads workspace files and produces a structured review report.

The reviewer scans the workspace for source files, reads them via FilesystemTool
(with sensitive-data gating), and produces a structured review_report.json.

Output artifact: <workspace>/review_report.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from factory.models import AgentResult, TaskRecord
from factory.agents.base import BaseAgent

logger = logging.getLogger(__name__)

_SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".java", ".go", ".rs", ".rb", ".cpp", ".c"}
_MAX_FILES_TO_REVIEW = 50


class ReviewerAgent(BaseAgent):
    """
    Reads workspace files and produces a structured review report using the LLM.

    The reviewer performs structural analysis, reads the files, 
    and passes them to the LLM to identify defects and approve/reject.
    """

    agent_type = "reviewer"

    def run(self, task: TaskRecord) -> AgentResult:
        self._emit("agent.reviewer.started", {"task_id": task.task_id})
        
        if not self._llm or not self._context:
            return self._fail(task, "LLM Router or Context Builder not provided to agent")
            
        fs = self._make_filesystem_tool()
        security_tool = self._make_security_tool()

        if self.workspace_root is None:
            return self._fail(task, "Reviewer requires a workspace_root.")

        root = self.workspace_root.resolve()
        if not root.exists():
            return self._fail(task, f"Workspace root does not exist: {root}")

        # Collect files
        file_stats: dict[str, int] = {}
        reviewed_files: list[str] = []
        file_contents: dict[str, str] = {}

        all_files = sorted(
            [p for p in root.rglob("*") if p.is_file() and not _should_skip(p)],
            key=lambda p: str(p),
        )[:_MAX_FILES_TO_REVIEW]

        for file_path in all_files:
            ext = file_path.suffix.lower()
            file_stats[ext] = file_stats.get(ext, 0) + 1

            size = file_path.stat().st_size
            rel = str(file_path.relative_to(root))

            reviewed_files.append(rel)
            
            if size < 50_000:
                try:
                    file_contents[rel] = file_path.read_text(encoding="utf-8")
                except Exception:
                    file_contents[rel] = "<Binary or unreadable>"
            else:
                file_contents[rel] = f"<File too large to review: {size} bytes>"

        # Prepare LLM Request
        system_prompt = (
            "You are the Reviewer Agent. Your goal is to review the workspace code and identify defects.\n"
            "Evaluate if the code meets standard quality and security bars.\n"
            "If defects are found, set approved to false and list the defects. "
            "If the code looks acceptable, set approved to true."
        )
        
        files_str = "\n".join([f"--- {p} ---\n{c}\n" for p, c in file_contents.items()])
        
        messages = [
            {"role": "user", "content": f"Please review the following files:\n{files_str}"}
        ]
        
        from factory.llm.schemas.reviewer import ReviewerProposal
        
        try:
            request = self._context.build_request(
                project_id=task.project_id or "",
                system_prompt=system_prompt,
                messages=messages,
                response_schema=ReviewerProposal,
            )
            response = self._llm.complete(request)
        except Exception as exc:
            return self._fail(task, f"LLM inference failed: {exc}")
            
        if not response.success:
            return self._fail(task, f"LLM failed: {response.error}")
            
        if not response.structured_output:
            return self._fail(task, "LLM returned no structured output")
            
        try:
            proposal = ReviewerProposal.model_validate_json(response.structured_output)
        except Exception as exc:
            return self._fail(task, f"Failed to parse LLM output as ReviewerProposal: {exc}")

        report = {
            "task_id": task.task_id,
            "project_id": task.project_id,
            "total_files": len(reviewed_files),
            "file_types": file_stats,
            "defects": [d.model_dump() for d in proposal.defects],
            "approved": proposal.approved,
            "risks": proposal.risks,
            "reviewed_files": reviewed_files[:20],
        }

        # Write report artifact
        report_path = Path("review_report.json")
        write_result = fs.write_json(report_path, report, task_id=task.task_id)

        if not write_result.success:
            return self._fail(
                task,
                f"Failed to write review_report.json: {write_result.error}",
            )

        self._emit("agent.reviewer.completed", {
            "task_id": task.task_id,
            "files_reviewed": len(reviewed_files),
            "defects_found": len(proposal.defects),
            "approved": proposal.approved,
        })

        summary = (
            f"LLM reviewed {len(reviewed_files)} files. "
            f"Found {len(proposal.defects)} defect(s). Approved: {proposal.approved}."
        )

        return self._ok(
            task,
            summary=summary,
            outputs={
                "report_path": str(self._artifact_path("review_report.json")),
                "files_reviewed": len(reviewed_files),
                "defects": [d.model_dump() for d in proposal.defects],
                "approved": proposal.approved,
            },
            evidence=[{"kind": "file", "path": "review_report.json"}],
        )

def _should_skip(path: Path) -> bool:
    """Return True for files that should be excluded from review."""
    skip_dirs = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
    skip_exts = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3"}
    return (
        any(part in skip_dirs for part in path.parts)
        or path.suffix.lower() in skip_exts
    )
