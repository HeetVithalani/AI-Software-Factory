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
import os
import stat
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
    requires_human_approval:
        True when the operation did not fail — it is PAUSED pending a
        human approval decision (COR-003). Consumers must not treat this
        as an ordinary error.
    approval_request_id:
        The pending ApprovalRequest id when requires_human_approval is
        True — lets agents/orchestrator park and later re-verify the
        exact approval before resuming.
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
    requires_human_approval: bool = False
    approval_request_id: str = ""
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
            "requires_human_approval": self.requires_human_approval,
            "approval_request_id": self.approval_request_id,
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
        Resolve path and verify it is within the workspace sandbox (SEC-001).

        Fail-closed checks, in order:

        1. **NUL byte** — never a valid path component.
        2. **Resolved containment** — the ``resolve()``'d path (which
           collapses ``..`` traversal and follows links to their final
           target) must sit inside ``workspace_root``.
        3. **Explicit case-normalised containment** — belt and braces on
           top of ``relative_to``: normcase prefix equality with a
           separator boundary, so ``/proj_evil`` can never count as
           inside ``/proj`` on a case-insensitive filesystem.
        4. **No link components below the root** — every component of the
           *original* (unresolved) requested path that lies strictly
           below the workspace root is ``lstat``'d; symbolic links and
           NTFS junctions (reparse indirections) are rejected outright,
           even when they currently point INSIDE the workspace. This
           removes the check-then-use indirection that could be
           retargeted between validation and use. Cloud-placeholder
           reparse points (e.g. Files On-Demand) are not links and are
           not rejected.

        Relative paths are resolved relative to workspace_root.
        Raises ToolError if any check fails.
        """
        if self.workspace_root is None:
            return path.resolve()

        if "\x00" in str(path):
            raise ToolError(
                f"Path {str(path)!r} contains a NUL byte.",
                tool=self.tool_name,  # type: ignore[call-arg]
            )

        root = self.workspace_root.resolve()
        # Resolve relative paths against workspace_root, not cwd
        if not path.is_absolute():
            resolved = (root / path).resolve()
            original = self.workspace_root / path
        else:
            resolved = path.resolve()
            original = path

        # -- 2. resolved containment --
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ToolError(
                f"Path {path!r} is outside the workspace sandbox {root!r}.",
                tool=self.tool_name,  # type: ignore[call-arg]
            )

        # -- 3. explicit case-normalised containment --
        root_key = os.path.normcase(str(root))
        resolved_key = os.path.normcase(str(resolved))
        if resolved_key != root_key and not resolved_key.startswith(root_key + os.sep):
            raise ToolError(
                f"Path {path!r} is outside the workspace sandbox {root!r}.",
                tool=self.tool_name,  # type: ignore[call-arg]
            )

        # -- 4. no symlink/junction components below the root --
        self._reject_link_components(path, original)

        return resolved

    def _reject_link_components(self, display: Path, original: Path) -> None:
        """
        Raise ToolError if ``original`` traverses a symlink or junction
        below the workspace root (SEC-001 component check).
        """
        root_keys = {
            os.path.normcase(os.path.abspath(str(self.workspace_root))),
            os.path.normcase(str(self.workspace_root.resolve())),
        }
        is_junction = getattr(os.path, "isjunction", None)

        current: Path | None = None
        for part in original.parts:
            current = Path(part) if current is None else current / part
            key = os.path.normcase(os.path.abspath(str(current)))
            if not any(key.startswith(rk + os.sep) for rk in root_keys):
                # At or above the workspace root — the root's own
                # placement is deliberate and not this check's concern.
                continue
            try:
                st = os.lstat(current)
            except (OSError, ValueError):
                # Component does not exist (yet) — nothing to reject;
                # anything created later is created by us, not linked.
                continue

            if stat.S_ISLNK(st.st_mode):
                raise ToolError(
                    f"Path {display!r} traverses symbolic link {current!r}: "
                    "links inside the workspace are not permitted "
                    "(SEC-001).",
                    tool=self.tool_name,  # type: ignore[call-arg]
                )
            if is_junction is not None and is_junction(current):
                raise ToolError(
                    f"Path {display!r} traverses directory junction "
                    f"{current!r}: links inside the workspace are not "
                    "permitted (SEC-001).",
                    tool=self.tool_name,  # type: ignore[call-arg]
                )

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

    def _fail_approval(
        self, error: str, *, command: str = "", request_id: str = ""
    ) -> ToolResult:
        """
        Return a failure that is specifically a PENDING human approval.

        The operation did not go wrong — it is paused for review
        (COR-003). Agents propagate the flag so the orchestrator parks
        the task in AWAITING_APPROVAL instead of burning retry budget
        or marking it failed.
        """
        result = self._fail(error, command=command)
        result.requires_human_approval = True
        result.approval_request_id = request_id
        return result

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
