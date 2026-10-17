"""
factory.tools.shell
=====================
Controlled subprocess execution tool — non-shell by design (SEC-002).

Security model
--------------
* Commands are parsed by :mod:`factory.tools.command`: unquoted shell
  operators, explicit shells, and batch scripts are rejected outright;
  the string is tokenized into an argv — the OS shell is NEVER invoked
  (``shell=False``), so a model-proposed string cannot smuggle pipes,
  redirections, chaining, or substitution past the parser.
* The executable token is resolved to an absolute path after approval;
  the canonical argv string (program + every argument) is exactly what
  the RiskEngine classifies and the PermissionBroker approves.
* CWD must be within the project workspace sandbox (enforced before exec).
* HIGH/CRITICAL commands are blocked unless PermissionBroker grants approval.
* Commands run with a bounded timeout (default 60 s).
* Output is captured and hashed for the audit trail.
* Unrestricted shell access (sudo, rm -rf, destructive ops) is blocked at
  the RiskEngine classification level — CRITICAL commands require fresh
  approval.

Usage::

    tool = ShellTool(workspace_root=Path("/project"))
    result = tool.run("python -m pytest tests/")
    if result.success:
        print(result.output)

Documented limitations
----------------------
* Shell builtins (``echo``, ``dir`` as builtins) are unavailable — use
  real programs (e.g. ``python -c "print('hi')"``, ``cmd`` is rejected).
* This is not a general shell; see :mod:`factory.tools.command`.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from factory.models import RiskLevel
from factory.permissions.risk import ActionContext, RiskEngine
from factory.tools.base import BaseTool, ToolResult
from factory.tools.command import (
    CommandParseError,
    canonicalize_argv,
    parse_command,
    reject_executable_token,
    resolve_executable,
)

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
        Execute a model-proposed command string — without a shell.

        Steps:
        1. Parse: reject unquoted operators / shells / batch scripts,
           tokenize into argv (fail-closed).
        2. Classify risk on the canonical argv string.
        3. Check PermissionBroker — the approval describes the exact
           canonical operation that will execute.
        4. Resolve the executable to an absolute path, then execute with
           ``shell=False`` and CWD=workspace_root.
        5. Return ToolResult with stdout, stderr, returncode, duration.

        Parameters
        ----------
        command:
            Shell-style command string. Parsed, never shell-executed.
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

        # Workspace CWD enforcement (before parsing, as before)
        cwd = self.workspace_root
        if cwd is not None:
            cwd = cwd.resolve()
            if not cwd.exists():
                return self._fail(
                    f"Workspace root {cwd!r} does not exist.",
                    command=command,
                )

        # Phase 1-2: structural parse (operators, tokenization, shells)
        parsed = parse_command(command)
        if parsed.rejected:
            self._audit_emit("tool.shell.rejected", {
                "command": command,
                "reason": parsed.rejected_reason,
            })
            return self._fail(parsed.rejected_reason, command=command)

        return self._execute(
            parsed.argv,
            canonical=parsed.canonical,
            raw=command,
            cwd=cwd,
            task_id=task_id,
            env=env,
            capture_output=capture_output,
        )

    def run_argv(
        self,
        argv: list[str],
        *,
        task_id: str | None = None,
        env: dict[str, str] | None = None,
        capture_output: bool = True,
    ) -> ToolResult:
        """
        Execute a STRUCTURED command (executable + explicit arguments).

        Preferred API when the caller already has an argv list — no
        string parsing occurs. Arguments are passed verbatim; only the
        executable token is validated (no shells, no batch scripts).
        """
        if not argv or not all(isinstance(a, str) for a in argv):
            return self._fail("run_argv requires a non-empty list of strings.")
        if not argv[0].strip():
            return self._fail("Empty executable.", command="")

        cwd = self.workspace_root
        if cwd is not None:
            cwd = cwd.resolve()
            if not cwd.exists():
                return self._fail(
                    f"Workspace root {cwd!r} does not exist.",
                    command=canonicalize_argv(list(argv)),
                )

        try:
            reject_executable_token(argv[0])
        except CommandParseError as exc:
            self._audit_emit("tool.shell.rejected", {
                "command": canonicalize_argv(list(argv)),
                "reason": str(exc),
            })
            return self._fail(str(exc), command=canonicalize_argv(list(argv)))

        return self._execute(
            list(argv),
            canonical=canonicalize_argv(list(argv)),
            raw=canonicalize_argv(list(argv)),
            cwd=cwd,
            task_id=task_id,
            env=env,
            capture_output=capture_output,
        )

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

    # ------------------------------------------------------------------
    # Execution core (shared by run and run_argv)
    # ------------------------------------------------------------------

    def _execute(
        self,
        argv: list[str],
        *,
        canonical: str,
        raw: str,
        cwd: Path | None,
        task_id: str | None,
        env: dict[str, str] | None,
        capture_output: bool,
    ) -> ToolResult:
        """
        Classify, gate, resolve, and execute an argv without a shell.

        ``argv[0]`` may still be an unresolved token; it is resolved to
        an absolute path only AFTER the broker approves, so the approval
        record describes the human-readable operation while execution
        uses the exact resolved program.
        """
        # Risk classification on the canonical argv string
        ctx = ActionContext(category="shell", command=canonical)
        risk_level = _risk_engine.classify(ctx)

        logger.debug(
            "ShellTool: command=%r risk=%s", canonical, risk_level.value
        )

        # Permission check for anything above SAFE
        if risk_level not in (RiskLevel.SAFE, RiskLevel.LOW):
            decision = self._check_permission(
                ctx,
                action=f"shell: {canonical}",
                target=str(cwd or "."),
                reason=f"Execute shell command: {canonical!r}",
                expected_effect="Command runs in workspace and produces output",
                consequence_if_denied="Command will not be executed",
                action_category="shell",
                task_id=task_id,
            )

            if decision is not None and decision.denied:
                self._audit_emit("tool.shell.denied", {
                    "command": canonical,
                    "reason": decision.reason,
                    "risk": risk_level.value,
                })
                return self._fail(
                    f"Shell command denied by policy: {decision.reason}",
                    command=canonical,
                )

            if decision is not None and decision.requires_approval:
                req_id = (
                    decision.approval_request.request_id
                    if decision.approval_request else ""
                )
                self._audit_emit("tool.shell.approval_required", {
                    "command": canonical,
                    "risk": risk_level.value,
                    "request_id": req_id,
                })
                return self._fail_approval(
                    f"Shell command requires human approval "
                    f"(risk={risk_level.value}, request_id={req_id}): "
                    f"{canonical}",
                    command=canonical,
                    request_id=req_id,
                )

        # Resolve the executable to an absolute path (post-approval)
        try:
            executable = resolve_executable(argv[0], cwd=cwd)
        except CommandParseError as exc:
            self._audit_emit("tool.shell.rejected", {
                "command": canonical,
                "reason": str(exc),
            })
            return self._fail(str(exc), command=canonical)

        final_argv = [executable] + list(argv[1:])

        # Execute — NEVER through a shell
        start = time.monotonic()
        try:
            proc = subprocess.run(
                final_argv,
                shell=False,
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
                "command": canonical,
                "timeout_s": self.timeout,
            })
            return self._fail(
                f"Command timed out after {self.timeout}s: {canonical}",
                command=canonical,
            )
        except OSError as exc:
            duration_ms = (time.monotonic() - start) * 1000
            return self._fail(str(exc), command=canonical)

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        success = proc.returncode == 0

        self._audit_emit("tool.shell.executed", {
            "command": canonical,
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
            command=canonical,
            returncode=proc.returncode,
            duration_ms=duration_ms,
            metadata={
                "stderr": stderr,
                "risk_level": risk_level.value,
                "executable": executable,
            },
        )

        logger.info(
            "ShellTool: command=%r rc=%d duration=%.0fms",
            canonical, proc.returncode, duration_ms,
        )
        return result
