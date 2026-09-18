"""
factory.agents.documentation
=============================
Documentation agent — generates structured documentation from workspace files.

Output artifact: <workspace>/DOCS.md
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from factory.models import AgentResult, TaskRecord
from factory.agents.base import BaseAgent

logger = logging.getLogger(__name__)

_DOC_EXTENSIONS = {".py", ".js", ".ts", ".md", ".rst", ".txt"}
_MAX_FILES = 30
_MAX_FILE_PREVIEW = 2000  # chars per file in the doc


class DocumentationAgent(BaseAgent):
    """
    Generates DOCS.md from workspace source files.

    Reads source files via FilesystemTool (with permission gating).
    Produces a structured markdown document with:
    - Project overview
    - File tree
    - Per-file summaries (first N chars of each file)
    """

    agent_type = "documentation"

    def run(self, task: TaskRecord) -> AgentResult:
        self._emit("agent.documentation.started", {"task_id": task.task_id})
        fs = self._make_filesystem_tool()

        if self.workspace_root is None:
            return self._fail(task, "DocumentationAgent requires a workspace_root.")

        root = self.workspace_root.resolve()
        if not root.exists():
            return self._fail(task, f"Workspace does not exist: {root}")

        project_name = task.metadata.get("project_name") or task.name or "Project"
        description = task.metadata.get("description") or task.description or ""

        # Collect documentable files
        all_files = [
            p for p in sorted(root.rglob("*"))
            if p.is_file()
            and p.suffix.lower() in _DOC_EXTENSIONS
            and not _should_skip(p)
        ][:_MAX_FILES]

        doc_sections: list[str] = [
            f"# {project_name}",
            "",
            f"> {description}" if description else "",
            "",
            "## File Overview",
            "",
        ]
        
        # Read key project files
        files_to_read = ["main.py", "README.md", "pyproject.toml", "hello.py"]
        content = ""
        for f in files_to_read:
            try:
                res = fs.read(Path(f), task_id=task.task_id)
                if res.success:
                    content += f"--- {f} ---\n{res.output}\n\n"
            except Exception:
                pass
                
        system_prompt = (
            "You are the Documentation Agent. Your job is to write a comprehensive README.md "
            "for the project based on the provided source code.\n"
            "Include an architecture overview, setup instructions, and usage."
        )
        
        messages = [{"role": "user", "content": f"Project source files:\n{content}"}]
        
        from factory.llm.schemas.documentation import DocumentationProposal
        try:
            req = self._context.build_request(
                project_id=task.project_id or self._project_id,
                system_prompt=system_prompt,
                messages=messages,
                response_schema=DocumentationProposal,
            )
            llm_res = self._llm.complete(req)
        except Exception as exc:
            return self._fail(task, f"LLM inference failed: {exc}")
            
        if not llm_res.success:
            return self._fail(task, f"LLM failed: {llm_res.error}")
            
        try:
            proposal = DocumentationProposal.model_validate(llm_res.structured_data)
        except Exception as exc:
            return self._fail(task, f"Failed to parse LLM output: {exc}")

        write_result = fs.write(
            Path("README.md"), proposal.readme_content.encode("utf-8"), task_id=task.task_id
        )

        if not write_result.success:
            logger.warning("DocumentationAgent: could not write README.md: %s", write_result.error)

        self._emit("agent.documentation.completed", {
            "task_id": task.task_id,
        })

        return self._ok(
            task,
            summary=proposal.summary,
            outputs={
                "readme_path": str(self._artifact_path("README.md")),
            },
            evidence=[{"kind": "documentation", "path": "README.md"}],
        )


def _should_skip(path: Path) -> bool:
    """Return True for paths that should be excluded from documentation."""
    skip_dirs = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
    return any(part in skip_dirs for part in path.parts)
