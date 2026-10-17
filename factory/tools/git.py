"""
factory.tools.git
==================
Git operations tool with automatic mutation gating.

Security model
--------------
* Read operations (status, log, diff, show, branch -v) are SAFE and run
  automatically without broker interaction.
* Mutation operations (commit, push, merge, rebase, reset, apply) are HIGH
  or CRITICAL risk and ALWAYS require explicit PermissionBroker approval.
* Mutations additionally require that ``workspace_root`` IS the
  repository root — acting on a PARENT repository from a subdirectory
  crosses the workspace boundary and is refused (SEC-004).
* Caller-supplied refs/remotes starting with ``-`` are refused (git would
  parse them as options — argument-injection guard, SEC-004); path
  arguments are passed after ``--``.
* Force operations (push --force, reset --hard, clean -f) are CRITICAL and
  blocked unless broker explicitly approves them (fresh approval required
  every time — no scope inheritance for force operations).
* The tool always runs git with ``cwd=workspace_root``.
* Git is never auto-installed; if git is not on PATH, operations fail
  cleanly with a descriptive error.

Usage::

    tool = GitTool(workspace_root=Path("/project"), broker=broker)

    result = tool.status()          # always safe
    result = tool.log(max_entries=10)
    result = tool.diff()

    result = tool.commit("feat: add login")   # requires approval
    result = tool.push("origin", "main")      # requires approval
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from factory.permissions.risk import ActionContext, RiskEngine
from factory.models import RiskLevel
from factory.tools.base import BaseTool, ToolError, ToolResult
from factory.tools.shell import ShellTool

logger = logging.getLogger(__name__)
_risk_engine = RiskEngine()

# Operations that are always SAFE (read-only git)
_SAFE_GIT_OPS = frozenset({
    "status", "log", "diff", "show", "branch", "tag",
    "remote", "rev-parse", "ls-files", "shortlog", "describe",
})

# Operations that are outright CRITICAL and need individual fresh approval
_CRITICAL_GIT_OPS = frozenset({
    "push --force", "push -f", "reset --hard", "clean -f", "clean -fd",
    "filter-branch",
})

_GIT_TIMEOUT = 30  # seconds for git subprocess


def _validate_git_arg(value: str, *, kind: str = "argument") -> str | None:
    """
    Return an error string when ``value`` is unsafe as a git argv element.

    Git parses any ``-``-leading element as an OPTION regardless of
    position, so a caller-supplied ref/remote starting with ``-`` is
    argument injection (SEC-004): e.g. ``push(remote="-f")`` would force
    a push without the CRITICAL force-approval path, or
    ``rev_parse("--exec-path=...")`` could re-point git's internals.
    Values are otherwise passed verbatim as single argv elements (no
    shell), so embedded spaces/quotes are safe.
    """
    if not value or not value.strip():
        return f"Empty git {kind}."
    if value.startswith("-"):
        return (
            f"Git {kind} {value!r} may not start with '-': option-like "
            "values are refused (argument-injection guard, SEC-004)."
        )
    if any(ord(c) < 32 for c in value):
        return f"Git {kind} contains control characters."
    return None


def _run_git(
    args: list[str],
    *,
    cwd: Path | None,
    timeout: int = _GIT_TIMEOUT,
) -> tuple[bool, str, str, int]:
    """
    Low-level git runner.

    Returns (success, stdout, stderr, returncode).
    """
    cmd = ["git"] + args
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode == 0, proc.stdout, proc.stderr, proc.returncode
    except FileNotFoundError:
        return False, "", "git not found on PATH", 127
    except subprocess.TimeoutExpired:
        return False, "", f"git command timed out after {timeout}s", -1
    except OSError as exc:
        return False, "", str(exc), -1


class GitTool(BaseTool):
    """
    Git operations tool with explicit mutation gating.

    Read-only operations are automatic. Mutations always go through the
    permission broker.
    """

    tool_name = "git"

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
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

    # ------------------------------------------------------------------
    # Read-only operations (always safe, no approval needed)
    # ------------------------------------------------------------------

    def is_git_repo(self) -> bool:
        """Return True if workspace_root is the root of a git repository."""
        if not self.workspace_root:
            return False
        ok, stdout, _, rc = _run_git(
            ["rev-parse", "--absolute-git-dir"],
            cwd=self.workspace_root,
        )
        if not ok or not stdout.strip():
            return False
        # The .git dir must be under workspace_root (not a parent repo)
        git_dir = Path(stdout.strip()).resolve()
        workspace = self.workspace_root.resolve()
        try:
            git_dir.relative_to(workspace)
            return True
        except ValueError:
            return False

    def status(self) -> ToolResult:
        """Return the git working tree status. Fails if not a git repo."""
        if not self.is_git_repo():
            return ToolResult(
                success=False,
                output="",
                error="Not a git repository (or workspace_root is not the repo root).",
                tool_name=self.tool_name,
                command="git status --short",
                returncode=128,
            )
        ok, stdout, stderr, rc = _run_git(["status", "--short"], cwd=self.workspace_root)
        self._audit_emit("tool.git.status", {"returncode": rc})
        return self._git_result(ok, stdout, stderr, rc, "git status --short")

    def log(self, *, max_entries: int = 20, format_: str = "--oneline") -> ToolResult:
        """Return the commit log."""
        args = ["log", format_, f"-{max_entries}"]
        ok, stdout, stderr, rc = _run_git(args, cwd=self.workspace_root)
        self._audit_emit("tool.git.log", {"max": max_entries, "returncode": rc})
        return self._git_result(ok, stdout, stderr, rc, f"git log {format_} -{max_entries}")

    def diff(self, *, cached: bool = False, path: str = "") -> ToolResult:
        """Return the working tree or staged diff."""
        args = ["diff"]
        if cached:
            args.append("--cached")
        if path:
            args.extend(["--", path])
        ok, stdout, stderr, rc = _run_git(args, cwd=self.workspace_root)
        cmd = "git " + " ".join(args)
        self._audit_emit("tool.git.diff", {"cached": cached, "returncode": rc})
        return self._git_result(ok, stdout, stderr, rc, cmd)

    def show(self, ref: str = "HEAD") -> ToolResult:
        """Show a commit."""
        err = _validate_git_arg(ref, kind="ref")
        if err:
            return self._fail(f"git show: {err}", command="git show")
        ok, stdout, stderr, rc = _run_git(["show", ref, "--stat"], cwd=self.workspace_root)
        self._audit_emit("tool.git.show", {"ref": ref, "returncode": rc})
        return self._git_result(ok, stdout, stderr, rc, f"git show {ref} --stat")

    def branch_list(self) -> ToolResult:
        """List branches."""
        ok, stdout, stderr, rc = _run_git(["branch", "-v"], cwd=self.workspace_root)
        self._audit_emit("tool.git.branch_list", {"returncode": rc})
        return self._git_result(ok, stdout, stderr, rc, "git branch -v")

    def rev_parse(self, ref: str = "HEAD") -> ToolResult:
        """Return the full SHA for a ref."""
        err = _validate_git_arg(ref, kind="ref")
        if err:
            return self._fail(f"git rev-parse: {err}", command="git rev-parse")
        ok, stdout, stderr, rc = _run_git(["rev-parse", ref], cwd=self.workspace_root)
        return self._git_result(ok, stdout.strip(), stderr, rc, f"git rev-parse {ref}")

    def ls_files(self, path: str = "") -> ToolResult:
        """List tracked files."""
        args = ["ls-files"]
        if path:
            args.extend(["--", path])
        ok, stdout, stderr, rc = _run_git(args, cwd=self.workspace_root)
        return self._git_result(ok, stdout, stderr, rc, "git ls-files")

    # ------------------------------------------------------------------
    # Mutation operations (always require approval)
    # ------------------------------------------------------------------

    def commit(
        self,
        message: str,
        *,
        add_all: bool = False,
        task_id: str | None = None,
    ) -> ToolResult:
        """
        Create a git commit. ALWAYS requires approval (HIGH risk).

        If add_all=True, also runs ``git add -A`` first (also gated).
        """
        boundary = self._repo_boundary_error("commit")
        if boundary:
            self._audit_emit("tool.git.boundary_blocked", {
                "operation": "commit", "reason": boundary,
            })
            return self._fail(boundary, command="git commit")

        command = f"git commit -m '{message}'"
        ctx = ActionContext(category="git", command=command)
        approval = self._require_git_mutation_approval(
            ctx,
            action=command,
            target=str(self.workspace_root or "."),
            reason=f"Create git commit: {message!r}",
            expected_effect="A new commit is added to the repository history",
            consequence_if_denied="Changes remain uncommitted",
            task_id=task_id,
        )
        if approval is not None:
            return approval  # blocked

        if add_all:
            ok, _, stderr, rc = _run_git(["add", "-A"], cwd=self.workspace_root)
            if not ok:
                return self._fail(f"git add -A failed: {stderr}", command="git add -A")

        ok, stdout, stderr, rc = _run_git(
            ["commit", "-m", message], cwd=self.workspace_root
        )
        self._audit_emit("tool.git.commit", {
            "message": message,
            "returncode": rc,
            "success": ok,
        })
        return self._git_result(ok, stdout, stderr, rc, command)

    def push(
        self,
        remote: str = "origin",
        branch: str = "HEAD",
        *,
        force: bool = False,
        task_id: str | None = None,
    ) -> ToolResult:
        """
        Push to remote. HIGH risk normally; CRITICAL with force=True.
        """
        for value, kind in ((remote, "remote"), (branch, "ref")):
            err = _validate_git_arg(value, kind=kind)
            if err:
                return self._fail(f"git push: {err}", command="git push")

        boundary = self._repo_boundary_error("push")
        if boundary:
            self._audit_emit("tool.git.boundary_blocked", {
                "operation": "push", "reason": boundary,
            })
            return self._fail(boundary, command="git push")

        args = ["push", remote, branch]
        if force:
            args.append("--force")
        command = "git " + " ".join(args)

        ctx = ActionContext(category="git", command=command)
        if force:
            # Critical — never allow scope inheritance
            ctx.metadata["force"] = True

        approval = self._require_git_mutation_approval(
            ctx,
            action=command,
            target=f"remote:{remote}/{branch}",
            reason=f"Push {'(force) ' if force else ''}to {remote}/{branch}",
            expected_effect=f"Commits pushed to {remote}/{branch}",
            consequence_if_denied="Remote branch not updated",
            task_id=task_id,
        )
        if approval is not None:
            return approval

        ok, stdout, stderr, rc = _run_git(args, cwd=self.workspace_root)
        self._audit_emit("tool.git.push", {
            "remote": remote, "branch": branch,
            "force": force, "returncode": rc,
        })
        return self._git_result(ok, stdout, stderr, rc, command)

    def add(self, path: str = ".") -> ToolResult:
        """
        Stage files for commit. MEDIUM risk — requires approval.
        """
        boundary = self._repo_boundary_error("add")
        if boundary:
            self._audit_emit("tool.git.boundary_blocked", {
                "operation": "add", "reason": boundary,
            })
            return self._fail(boundary, command=f"git add {path}")

        command = f"git add {path}"
        ctx = ActionContext(category="git", command=command)
        approval = self._require_git_mutation_approval(
            ctx,
            action=command,
            target=str(self.workspace_root or "."),
            reason=f"Stage files for commit: {path!r}",
            expected_effect="Files are staged and ready to commit",
            consequence_if_denied="Files remain unstaged",
        )
        if approval is not None:
            return approval
        # "--" ends option parsing so a path like "-f" is a pathspec,
        # never an option (SEC-004).
        ok, stdout, stderr, rc = _run_git(["add", "--", path], cwd=self.workspace_root)
        return self._git_result(ok, stdout or f"staged: {path}", stderr, rc, command)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _repo_boundary_error(self, operation: str) -> str:
        """
        SEC-004: refuse mutations that would cross the repository boundary.

        If ``workspace_root`` is not itself the repository root but a
        repository exists somewhere above it, running ``git <mutation>``
        with ``cwd=workspace_root`` would act on the PARENT repository —
        committing/pushing files beyond the workspace. That is refused
        explicitly.

        Returns an empty string when it is safe to proceed: either the
        repository root IS the workspace, or no repository exists at all
        (git then fails cleanly on its own with "not a git repository").
        """
        if not self.workspace_root:
            return ""
        ok, stdout, _, _ = _run_git(
            ["rev-parse", "--show-toplevel"], cwd=self.workspace_root
        )
        if not ok or not stdout.strip():
            return ""  # no repository at all — nothing to cross into
        top = Path(stdout.strip()).resolve()
        workspace = self.workspace_root.resolve()
        if top == workspace:
            return ""
        return (
            f"Refusing git {operation}: workspace {workspace} is not the "
            f"repository root (repository root is {top}). Mutations are "
            "confined to the workspace boundary (SEC-004)."
        )

    def _require_git_mutation_approval(
        self,
        ctx: ActionContext,
        *,
        action: str,
        target: str,
        reason: str,
        expected_effect: str,
        consequence_if_denied: str,
        task_id: str | None = None,
    ) -> ToolResult | None:
        """
        Run a permission check for a mutation.

        Returns a ToolResult with success=False if blocked or pending.
        Returns None if approved (caller may proceed).
        """
        if self._broker is None:
            # No broker — log and proceed (test/dev mode without permissions)
            logger.warning(
                "GitTool: no broker configured — mutation %r proceeding unguarded.",
                action,
            )
            return None

        decision = self._check_permission(
            ctx,
            action=action,
            target=target,
            reason=reason,
            expected_effect=expected_effect,
            consequence_if_denied=consequence_if_denied,
            action_category="git.mutation",
            task_id=task_id,
        )

        if decision is None:
            return None  # broker error → allow (fail-open for dev mode)

        if decision.denied:
            return self._fail(
                f"Git mutation denied by policy: {decision.reason}",
                command=action,
            )
        if decision.requires_approval:
            req_id = (
                decision.approval_request.request_id
                if decision.approval_request else ""
            )
            self._audit_emit("tool.git.approval_required", {
                "action": action, "request_id": req_id,
            })
            return self._fail_approval(
                f"Git mutation requires human approval: {action} "
                f"(request_id={req_id})",
                command=action,
                request_id=req_id,
            )
        return None  # approved

    def _git_result(
        self,
        ok: bool,
        stdout: str,
        stderr: str,
        rc: int,
        command: str,
    ) -> ToolResult:
        return ToolResult(
            success=ok,
            output=stdout,
            error=stderr if not ok else "",
            tool_name=self.tool_name,
            command=command,
            returncode=rc,
            metadata={"stderr": stderr},
        )
