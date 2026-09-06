"""
factory.tools.base
==================
Shared base types for all factory tools.

Every tool returns a ToolResult — a structured value object that carries the
outcome, evidence hash, and metadata. Tools never raise on expected failures;
they capture the error inside ToolResult.success=False.

ToolError (from factory.errors) is raised only for programming errors —
wrong arguments, sandbox violations, etc. — not for tool execution failures.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.errors import ToolError  # canonical definition in factory.errors

logger = logging.getLogger(__name__)

__all__ = ["ToolError", "ToolResult", "BaseTool"]


@dataclass
class ToolResult:
    """
    Structured result returned by every factory tool.

    Attributes
    ----------
    success:
        True if the operation completed without error.
    output:
        Primary textual output (stdout for commands, content for reads).
    error:
        Error message if success=False.
    tool_name:
        Name of the tool that produced this result.
    command:
        The command or operation string (for shell/git tools).
    duration_ms:
        Wall-clock duration of the operation in milliseconds.
    returncode:
        Process return code for subprocess-based tools (None for others).
    evidence_hash:
        SHA-256 hash of the primary output, for audit trail attachment.
    metadata:
        Supplementary structured data (test results, scan findings, etc.).
    produced_at:
        UTC timestamp of result production.
    """
    success: bool
    output: str = ""
    error: str = ""
    tool_name: str = ""
    command: str = ""
    duration_ms: float = 0.0
    returncode: int | None = None
    evidence_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    produced_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        # Auto-compute evidence hash if output is present and hash not set
        if self.output and not self.evidence_hash:
            self.evidence_hash = hashlib.sha256(
                self.output.encode("utf-8", errors="replace")
            ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return {
            "success": self.success,
            "output": self.output[:4096] if self.output else "",  # truncate for logs
            "error": self.error,
            "tool_name": self.tool_name,
            "command": self.command,
            "duration_ms": round(self.duration_ms, 2),
            "returncode": self.returncode,
            "evidence_hash": self.evidence_hash,
            "metadata": self.metadata,
            "produced_at": self.produced_at.isoformat(),
        }


class BaseTool:
    """
    Abstract base for all factory tools.

    Subclasses must set ``tool_name`` and call ``_check_permission()``
    before executing any side-effecting operation.

    The broker, audit_logger, and workspace_root are all optional —
    if absent, permission checks are skipped (useful for read-only ops
    and testing).
    """

    tool_name: str = "base"

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        broker: Any = None,
        audit_logger: Any = None,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self._broker = broker
        self._audit = audit_logger
        self._project_id = project_id
        self._agent_id = agent_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sandbox_path(self, path: Path) -> Path:
        """
        Resolve path and verify it is within the workspace sandbox.

        Relative paths are resolved relative to workspace_root.
        Raises ToolError if the resolved path escapes the workspace.
        """
        if self.workspace_root is None:
            return path.resolve()
        root = self.workspace_root.resolve()
        # Resolve relative paths against workspace_root, not cwd
        if not path.is_absolute():
            resolved = (root / path).resolve()
        else:
            resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ToolError(
                f"Path {path!r} is outside the workspace sandbox {root!r}.",
                tool=self.tool_name,  # type: ignore[call-arg]
            )
        return resolved

    def _check_permission(
        self,
        ctx: Any,
        *,
        action: str,
        target: str,
        reason: str,
        expected_effect: str,
        consequence_if_denied: str,
        action_category: str = "",
        task_id: str | None = None,
    ) -> "Any":  # returns PermissionDecision or None
        """
        Run a broker permission check.

        Returns the PermissionDecision, or None if no broker is configured.
        """
        if self._broker is None:
            return None
        try:
            from factory.permissions.broker import PermissionDecision
            decision = self._broker.check(
                ctx,
                action=action,
                target=target,
                reason=reason,
                expected_effect=expected_effect,
                consequence_if_denied=consequence_if_denied,
                action_category=action_category,
                task_id=task_id,
                project_id=self._project_id,
                agent_id=self._agent_id,
            )
            return decision
        except Exception as exc:
            logger.warning(
                "%s: permission check error: %s", self.tool_name, exc
            )
            return None

    def _audit_emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Emit an audit event without hard-failing."""
        if self._audit is None:
            return
        try:
            self._audit.emit(event_type, payload)
        except Exception as exc:
            logger.warning("Audit emit failed: %s", exc)

    def _fail(self, error: str, *, command: str = "") -> ToolResult:
        """Convenience: return a failed ToolResult."""
        return ToolResult(
            success=False,
            error=error,
            tool_name=self.tool_name,
            command=command,
        )

    def _ok(
        self,
        output: str = "",
        *,
        command: str = "",
        returncode: int = 0,
        duration_ms: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Convenience: return a successful ToolResult."""
        return ToolResult(
            success=True,
            output=output,
            tool_name=self.tool_name,
            command=command,
            returncode=returncode,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
