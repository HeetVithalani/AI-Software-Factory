"""
factory.agents.base
====================
Abstract base class for all factory agents.

Design principles
-----------------
* Each agent receives a TaskRecord and returns an AgentResult.
* Agents communicate with each other exclusively through artifact files
  written to the project workspace — NOT through in-memory object passing
  or giant free-form transcripts.
* Every agent has access to a tool bundle and an audit logger.
* The permission broker is available for any tool that needs it.
* Agents are stateless between runs — they read their inputs from artifacts
  and write their outputs to artifacts.
"""

from __future__ import annotations

import abc
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.models import AgentResult, TaskRecord

logger = logging.getLogger(__name__)


class BaseAgent(abc.ABC):
    """
    Abstract base for all factory agents.

    Subclasses implement ``run(task: TaskRecord) -> AgentResult``.

    Parameters
    ----------
    agent_id:
        Unique identifier for this agent instance. Auto-generated if not given.
    workspace_root:
        Root directory of the project workspace. All file I/O goes here.
    broker:
        PermissionBroker instance for permission checks.
    audit_logger:
        AuditLogger bound to the project/agent context.
    project_id:
        Current project ID (for audit context).
    """

    agent_type: str = "base"

    def __init__(
        self,
        *,
        agent_id: str | None = None,
        workspace_root: Path | None = None,
        broker: Any = None,
        audit_logger: Any = None,
        project_id: str | None = None,
        llm_router: Any = None,
        context_builder: Any = None,
    ) -> None:
        self.agent_id = agent_id or f"{self.agent_type}-{uuid.uuid4().hex[:8]}"
        self.workspace_root = workspace_root
        self._broker = broker
        self._audit = audit_logger
        self._project_id = project_id
        self._llm = llm_router
        self._context = context_builder

    @abc.abstractmethod
    def run(self, task: TaskRecord) -> AgentResult:
        """
        Execute the agent for the given task.

        Must return an AgentResult. Must NOT raise — capture errors in
        AgentResult.success=False and AgentResult.errors.
        """

    # ------------------------------------------------------------------
    # Tool factory helpers
    # ------------------------------------------------------------------

    def _make_filesystem_tool(self) -> Any:
        from factory.tools.filesystem import FilesystemTool
        return FilesystemTool(
            workspace_root=self.workspace_root,
            broker=self._broker,
            audit_logger=self._audit,
            project_id=self._project_id,
            agent_id=self.agent_id,
        )

    def _make_shell_tool(self) -> Any:
        from factory.tools.shell import ShellTool
        return ShellTool(
            workspace_root=self.workspace_root,
            broker=self._broker,
            audit_logger=self._audit,
            project_id=self._project_id,
            agent_id=self.agent_id,
        )

    def _make_git_tool(self) -> Any:
        from factory.tools.git import GitTool
        return GitTool(
            workspace_root=self.workspace_root,
            broker=self._broker,
            audit_logger=self._audit,
            project_id=self._project_id,
            agent_id=self.agent_id,
        )

    def _make_testing_tool(self) -> Any:
        from factory.tools.testing import TestingTool
        return TestingTool(
            workspace_root=self.workspace_root,
            broker=self._broker,
            audit_logger=self._audit,
            project_id=self._project_id,
            agent_id=self.agent_id,
        )

    def _make_security_tool(self) -> Any:
        from factory.tools.security import SecurityTool
        return SecurityTool(
            workspace_root=self.workspace_root,
            broker=self._broker,
            audit_logger=self._audit,
            project_id=self._project_id,
            agent_id=self.agent_id,
        )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit an audit event bound to this agent."""
        if self._audit is None:
            return
        try:
            self._audit.emit(event_type, {**payload, "agent_id": self.agent_id})
        except Exception as exc:
            logger.warning("Agent audit emit failed: %s", exc)

    def _ok(
        self,
        task: TaskRecord,
        summary: str = "",
        outputs: dict[str, Any] | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        """Return a successful AgentResult."""
        return AgentResult(
            agent_id=self.agent_id,
            task_id=task.task_id,
            project_id=task.project_id or self._project_id,
            success=True,
            summary=summary,
            outputs=outputs or {},
            evidence=evidence or [],
        )

    def _fail(
        self,
        task: TaskRecord,
        error: str,
        errors: list[str] | None = None,
    ) -> AgentResult:
        """Return a failed AgentResult."""
        all_errors = [error] + (errors or [])
        logger.error(
            "Agent %s failed for task %s: %s",
            self.agent_id, task.task_id, error,
        )
        return AgentResult(
            agent_id=self.agent_id,
            task_id=task.task_id,
            project_id=task.project_id or self._project_id,
            success=False,
            summary=f"Agent {self.agent_type} failed: {error}",
            errors=all_errors,
        )

    def _awaiting_approval(
        self,
        task: TaskRecord,
        error: str,
        *,
        request_id: str = "",
    ) -> AgentResult:
        """
        Return a result that PAUSES on a human approval (COR-003).

        This is not an ordinary failure: success=False with
        failure_kind="awaiting_approval" tells the orchestrator to park
        the task in AWAITING_APPROVAL — no retry budget is consumed and
        the task is requeued only after the approval is granted AND its
        operation fingerprint re-verifies.
        """
        logger.info(
            "Agent %s awaiting human approval for task %s (request_id=%s): %s",
            self.agent_id, task.task_id, request_id, error,
        )
        return AgentResult(
            agent_id=self.agent_id,
            task_id=task.task_id,
            project_id=task.project_id or self._project_id,
            success=False,
            summary=(
                f"Agent {self.agent_type} awaiting human approval"
                + (f" (request_id={request_id})" if request_id else "")
            ),
            errors=[error],
            failure_kind="awaiting_approval",
            approval_request_id=request_id,
        )

    def _fail_from_tool(self, task: TaskRecord, tool_result: Any) -> AgentResult:
        """
        Convert a failed ToolResult into an AgentResult, preserving the
        awaiting-approval signal (COR-003) when the tool is paused on a
        pending human decision.
        """
        if getattr(tool_result, "requires_human_approval", False):
            return self._awaiting_approval(
                task,
                tool_result.error or "Tool requires human approval",
                request_id=getattr(tool_result, "approval_request_id", ""),
            )
        return self._fail(task, tool_result.error or "Tool failed")

    def _artifact_path(self, filename: str) -> Path:
        """Return the path for an artifact file in the workspace."""
        if self.workspace_root is None:
            return Path(filename)
        return self.workspace_root / filename

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self.agent_id!r})"
