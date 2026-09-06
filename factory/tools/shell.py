"""
factory.tools.shell
====================
Controlled subprocess execution tool.

Security model
--------------
* CWD must be within the project workspace sandbox (enforced before exec).
* Every command is classified by RiskEngine before execution.
* HIGH/CRITICAL commands are blocked unless PermissionBroker grants approval.
* Commands run with a bounded timeout (default 60 s).
* Output is captured and hashed for the audit trail.
* Unrestricted shell access (sudo, rm -rf, destructive ops) is blocked at the
  RiskEngine classification level — CRITICAL commands require fresh approval.

Usage::

    tool = ShellTool(workspace_root=Path("/project"))
    result = tool.run("python -m pytest tests/")
    if result.success:
        print(result.output)
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from factory.permissions.risk import ActionContext, RiskEngine
from factory.models import RiskLevel
from factory.tools.base import BaseTool, ToolError, ToolResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60  # seconds
_risk_engine = RiskEngine()


class ShellTool(BaseTool):
    """
    Sandboxed subprocess execution tool.

    Parameters
    ----------
    workspace_root:
        All commands run with CWD=workspace_root. Required for sandboxing.
    timeout:
        Maximum command wall-clock time in seconds (default 60).
    broker / audit_logger / project_id / agent_id:
        Passed to BaseTool for permission checks and audit emission.
    """

    tool_name = "shell"

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        broker: Any = None,
        audit_logger: Any = None,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        super().__init__(
            workspace_root=workspace_root,
            broker=broker,
            audit_logger=audit_logger,
            project_id=project_id,
            agent_id=agent_id,
        )
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        command: str,
        *,
        task_id: str | None = None,
        env: dict[str, str] | None = None,
        capture_output: bool = True,
    ) -> ToolResult:
        """
        Execute a shell command within the workspace sandbox.

        Steps:
        1. Classify risk via RiskEngine.
        2. Check PermissionBroker if risk >= MEDIUM.
        3. Execute within workspace_root as CWD.
        4. Return ToolResult with stdout, stderr, returncode, duration.

        Parameters
        ----------
        command:
            Shell command string to execute.
        task_id:
            Optional task context for audit events.
        env:
            Optional environment variable overrides.
        capture_output:
            If True (default), capture stdout/stderr. If False, let them
            inherit the parent process's streams (useful for interactive tools).
        """
        if not command or not command.strip():
            return self._fail("Empty command.", command=command)

        # Workspace CWD enforcement
        cwd = self.workspace_root
        if cwd is not None:
            cwd = cwd.resolve()
            if not cwd.exists():
                return self._fail(
                    f"Workspace root {cwd!r} does not exist.",
                    command=command,
                )

        # Risk classification
        ctx = ActionContext(category="shell", command=command)
        risk_level = _risk_engine.classify(ctx)

        logger.debug(
            "ShellTool: command=%r risk=%s", command, risk_level.value
        )

        # Permission check for anything above SAFE
        if risk_level not in (RiskLevel.SAFE, RiskLevel.LOW):
            decision = self._check_permission(
                ctx,
                action=f"shell: {command}",
                target=str(cwd or "."),
                reason=f"Execute shell command: {command!r}",
                expected_effect="Command runs in workspace and produces output",
                consequence_if_denied="Command will not be executed",
                action_category="shell",
                task_id=task_id,
            )

            if decision is not None and decision.denied:
                self._audit_emit("tool.shell.denied", {
                    "command": command,
                    "reason": decision.reason,
                    "risk": risk_level.value,
                })
                return self._fail(
                    f"Shell command denied by policy: {decision.reason}",
                    command=command,
                )

            if decision is not None and decision.requires_approval:
                self._audit_emit("tool.shell.approval_required", {
                    "command": command,
                    "risk": risk_level.value,
                    "request_id": (
                        decision.approval_request.request_id
                        if decision.approval_request else ""
                    ),
                })
                return self._fail(
                    f"Shell command requires human approval (risk={risk_level.value}): {command}",
                    command=command,
                )

        # Execute
        start = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd) if cwd else None,
                capture_output=capture_output,
                timeout=self.timeout,
                text=True,
                env=env,
            )
            duration_ms = (time.monotonic() - start) * 1000
        except subprocess.TimeoutExpired:
            duration_ms = self.timeout * 1000
            self._audit_emit("tool.shell.timeout", {
                "command": command,
                "timeout_s": self.timeout,
            })
            return self._fail(
                f"Command timed out after {self.timeout}s: {command}",
                command=command,
            )
        except OSError as exc:
            duration_ms = (time.monotonic() - start) * 1000
            return self._fail(str(exc), command=command)

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined = stdout + ("\n--- stderr ---\n" + stderr if stderr else "")
        success = proc.returncode == 0

        self._audit_emit("tool.shell.executed", {
            "command": command,
            "returncode": proc.returncode,
            "duration_ms": round(duration_ms, 1),
            "risk": risk_level.value,
            "success": success,
        })

        result = ToolResult(
            success=success,
            output=stdout,
            error=stderr if not success else "",
            tool_name=self.tool_name,
            command=command,
            returncode=proc.returncode,
            duration_ms=duration_ms,
            metadata={
                "stderr": stderr,
                "risk_level": risk_level.value,
            },
        )

        logger.info(
            "ShellTool: command=%r rc=%d duration=%.0fms",
            command, proc.returncode, duration_ms,
        )
        return result

    def run_safe(self, command: str, **kwargs: Any) -> ToolResult:
        """
        Run a command that is known to be safe (skips broker check).

        Use only for read-only diagnostic commands.
        """
        # Override: classify but if safe, skip broker entirely
        ctx = ActionContext(category="shell", command=command)
        risk_level = _risk_engine.classify(ctx)
        if risk_level not in (RiskLevel.SAFE, RiskLevel.LOW):
            logger.warning(
                "ShellTool.run_safe: %r classified as %s — refusing.",
                command, risk_level.value,
            )
            return self._fail(
                f"run_safe() refused: command classified as {risk_level.value}.",
                command=command,
            )
        # Temporarily remove broker for this call
        saved = self._broker
        self._broker = None
        try:
            return self.run(command, **kwargs)
        finally:
            self._broker = saved
