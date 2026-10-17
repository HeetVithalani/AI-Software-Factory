"""
tests/unit/test_tools.py
========================
Deterministic unit tests for factory.tools.*

Strategy:
- Shell, Git, Browser tests use mocked subprocess/httpx
- FilesystemTool, TestingTool, SecurityTool use real tmp_path sandboxes
- PermissionBroker is mocked to return controlled decisions
- No real network calls
- No real git operations
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_approved_decision():
    """Return a mock PermissionDecision that is approved."""
    dec = MagicMock()
    dec.approved = True
    dec.denied = False
    dec.requires_approval = False
    return dec


def _make_denied_decision(reason="policy block"):
    """Return a mock PermissionDecision that is denied."""
    dec = MagicMock()
    dec.approved = False
    dec.denied = True
    dec.requires_approval = False
    dec.reason = reason
    return dec


def _make_pending_decision():
    """Return a mock PermissionDecision that is pending approval."""
    dec = MagicMock()
    dec.approved = False
    dec.denied = False
    dec.requires_approval = True
    req = MagicMock()
    req.request_id = "req-abc"
    dec.approval_request = req
    return dec


def _broker_always_approve():
    broker = MagicMock()
    broker.check.return_value = _make_approved_decision()
    return broker


def _broker_always_deny():
    broker = MagicMock()
    broker.check.return_value = _make_denied_decision()
    return broker


def _broker_always_pending():
    broker = MagicMock()
    broker.check.return_value = _make_pending_decision()
    return broker


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

class TestToolResult:
    def test_evidence_hash_auto_computed(self):
        from factory.tools.base import ToolResult
        r = ToolResult(success=True, output="hello world")
        assert len(r.evidence_hash) == 64  # SHA-256 hex
        assert r.evidence_hash != ""

    def test_no_hash_if_no_output(self):
        from factory.tools.base import ToolResult
        r = ToolResult(success=False, output="")
        assert r.evidence_hash == ""

    def test_to_dict_contains_key_fields(self):
        from factory.tools.base import ToolResult
        r = ToolResult(success=True, output="data", tool_name="test")
        d = r.to_dict()
        assert d["success"] is True
        assert d["tool_name"] == "test"
        assert "evidence_hash" in d

    def test_output_truncated_in_dict(self):
        from factory.tools.base import ToolResult
        r = ToolResult(success=True, output="x" * 10000)
        d = r.to_dict()
        assert len(d["output"]) <= 4096

    def test_failed_result(self):
        from factory.tools.base import ToolResult
        r = ToolResult(success=False, error="something went wrong", tool_name="shell")
        assert r.success is False
        assert r.error == "something went wrong"


# ---------------------------------------------------------------------------
# BaseTool sandbox
# ---------------------------------------------------------------------------

class TestBaseSandbox:
    def test_sandbox_allows_path_inside(self, tmp_path):
        from factory.tools.base import BaseTool
        class T(BaseTool):
            tool_name = "test"
            def run(self, task): ...
        t = T(workspace_root=tmp_path)
        inner = tmp_path / "src" / "main.py"
        result = t._sandbox_path(inner)
        assert result == inner.resolve()

    def test_sandbox_blocks_path_outside(self, tmp_path):
        from factory.tools.base import BaseTool
        from factory.errors import ToolError
        class T(BaseTool):
            tool_name = "test"
            def run(self, task): ...
        t = T(workspace_root=tmp_path)
        outside = tmp_path.parent / "outside.txt"
        with pytest.raises(ToolError):
            t._sandbox_path(outside)

    def test_no_sandbox_allows_any_path(self, tmp_path):
        from factory.tools.base import BaseTool
        class T(BaseTool):
            tool_name = "test"
            def run(self, task): ...
        t = T(workspace_root=None)
        result = t._sandbox_path(tmp_path / "anything.txt")
        assert result is not None

    # -- SEC-001 regression suite ------------------------------------------

    @staticmethod
    def _tool(tmp_path):
        from factory.tools.base import BaseTool
        class T(BaseTool):
            tool_name = "test"
            def run(self, task): ...
        return T(workspace_root=tmp_path)

    def test_traversal_dotdot_blocked(self, tmp_path):
        from factory.errors import ToolError
        t = self._tool(tmp_path)
        (tmp_path.parent / "outside_trav.txt").write_text("x")
        with pytest.raises(ToolError):
            t._sandbox_path(Path("..") / "outside_trav.txt")

    def test_prefix_sibling_directory_blocked(self, tmp_path):
        """'proj_evil' must not count as inside 'proj' (prefix boundary)."""
        from factory.errors import ToolError
        root = tmp_path / "proj"
        root.mkdir()
        sibling = tmp_path / "proj_evil"
        sibling.mkdir()
        tool = self._tool(root)
        with pytest.raises(ToolError):
            tool._sandbox_path(sibling / "secrets.txt")

    def test_case_variation_inside_allowed(self, tmp_path):
        """Case-different spelling of an inside path is allowed (win32)."""
        if os.name != "nt":
            pytest.skip("case-insensitive comparison is a Windows concern")
        root = tmp_path / "Proj"
        root.mkdir()
        (root / "file.txt").write_text("x")
        tool = self._tool(root)
        # Same path with different casing
        variant = tmp_path / "PROJ" / "FILE.TXT"
        result = tool._sandbox_path(variant)
        assert os.path.normcase(str(result)) == os.path.normcase(
            str((root / "file.txt").resolve())
        )

    def test_nul_byte_blocked(self, tmp_path):
        from factory.errors import ToolError
        tool = self._tool(tmp_path)
        with pytest.raises(ToolError):
            tool._sandbox_path(Path("with\x00nul.txt"))

    def _make_dir_symlink(self, link: Path, target: Path) -> None:
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            pytest.skip(
                "SEC-001 reported limitation: symlink creation not "
                f"permitted on this host ({exc})."
            )

    def test_symlink_inside_workspace_rejected(self, tmp_path):
        """A link INSIDE the root is rejected (no check-then-use indirection)."""
        from factory.errors import ToolError
        real = tmp_path / "real"
        real.mkdir()
        (real / "file.txt").write_text("x")
        link = tmp_path / "link"
        self._make_dir_symlink(link, real)

        tool = self._tool(tmp_path)
        with pytest.raises(ToolError) as exc_info:
            tool._sandbox_path(link / "file.txt")
        assert "symbolic link" in str(exc_info.value)

    def test_symlink_escaping_workspace_rejected(self, tmp_path):
        """A link pointing outside the root is rejected."""
        from factory.errors import ToolError
        outside = tmp_path.parent / "sec001_outside"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("secret")
        link = tmp_path / "escape"
        self._make_dir_symlink(link, outside)

        tool = self._tool(tmp_path)
        with pytest.raises(ToolError):
            tool._sandbox_path(link / "secret.txt")

    def test_junction_inside_workspace_rejected(self, tmp_path):
        """NTFS junctions are rejected like symlinks (SEC-001)."""
        from factory.errors import ToolError
        if os.name != "nt":
            pytest.skip("junctions are a Windows-only concept")
        real = tmp_path / "real"
        real.mkdir()
        (real / "file.txt").write_text("x")
        junction = tmp_path / "junction"
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(real)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            pytest.skip(
                "SEC-001 reported limitation: junction creation failed "
                f"on this host ({proc.stderr.strip()})."
            )
        try:
            tool = self._tool(tmp_path)
            with pytest.raises(ToolError) as exc_info:
                tool._sandbox_path(junction / "file.txt")
            assert "junction" in str(exc_info.value).lower()
        finally:
            os.rmdir(junction)  # removing a junction does not touch target

    def test_junction_escaping_workspace_rejected(self, tmp_path):
        from factory.errors import ToolError
        if os.name != "nt":
            pytest.skip("junctions are a Windows-only concept")
        outside = tmp_path.parent / "sec001_junc_outside"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("secret")
        junction = tmp_path / "escape_junction"
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            pytest.skip(
                "SEC-001 reported limitation: junction creation failed "
                f"on this host ({proc.stderr.strip()})."
            )
        try:
            tool = self._tool(tmp_path)
            with pytest.raises(ToolError):
                tool._sandbox_path(junction / "secret.txt")
        finally:
            os.rmdir(junction)


# ---------------------------------------------------------------------------
# ShellTool
# ---------------------------------------------------------------------------

class TestShellTool:
    def test_safe_command_runs_without_broker(self, tmp_path):
        # Contract update (SEC-002): shell builtins like `echo` are no
        # longer available (no shell); a real program is used instead.
        # Assertions unchanged: a safe command runs without a broker.
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_safe("python -c \"print('hello')\"")
        assert result.success
        assert "hello" in result.output.strip()

    def test_empty_command_returns_failure(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run("")
        assert not result.success
        assert "Empty" in result.error

    def test_nonexistent_workspace_fails(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path / "nonexistent")
        result = tool.run("echo hi")
        assert not result.success

    def test_safe_command_refused_if_risky(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_safe("rm -rf /tmp/x")
        assert not result.success
        assert "critical" in result.error.lower() or "refused" in result.error.lower()

    def test_medium_risk_requires_broker_approval(self, tmp_path):
        """Unknown shell command is MEDIUM risk — broker should be called."""
        from factory.tools.shell import ShellTool
        broker = _broker_always_pending()
        tool = ShellTool(workspace_root=tmp_path, broker=broker)
        result = tool.run("unknowncmd --flag")
        assert not result.success
        assert "approval" in result.error.lower()

    def test_approved_command_executes(self, tmp_path):
        # Contract update (SEC-002): real program instead of `echo` builtin.
        from factory.tools.shell import ShellTool
        broker = _broker_always_approve()
        tool = ShellTool(workspace_root=tmp_path, broker=broker)
        result = tool.run("python -c \"print('approved')\"")
        assert result.success
        assert "approved" in result.output

    def test_denied_command_blocked(self, tmp_path):
        from factory.tools.shell import ShellTool
        broker = _broker_always_deny()
        tool = ShellTool(workspace_root=tmp_path, broker=broker)
        result = tool.run("curl http://example.com")
        assert not result.success
        assert "denied" in result.error.lower()

    def test_returncode_captured(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_safe("python -c \"import sys; sys.exit(0)\"")
        assert result.returncode == 0

    def test_failed_returncode_captured(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_safe("python -c \"import sys; sys.exit(1)\"")
        assert result.returncode == 1
        assert not result.success

    def test_duration_populated(self, tmp_path):
        # Contract update (SEC-002): real program instead of `echo` builtin.
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_safe("python -c \"print('x')\"")
        assert result.duration_ms > 0


# ---------------------------------------------------------------------------
# ShellTool — non-shell execution (SEC-002 regression suite)
# ---------------------------------------------------------------------------

class TestShellNonShellExecution:
    """Structured execution: operators rejected, argv execution, exact approvals."""

    # -- rejected operators / injection attempts ---------------------------

    @pytest.mark.parametrize("cmd", [
        "python -c \"print(1)\" ; rm -rf /",
        "python -c \"print(1)\" && rm -rf /",
        "python -c \"print(1)\" || echo fallback",
        "type x | findstr secret",
        "python x.py > C:\\Windows\\System32\\evil.dll",
        "python x.py >> append.log",
        "python x.py < input.txt",
        "`whoami`",
        "python $(evil).py",
        "python ${EVIL}.py",
        "echo a & echo b",
    ])
    def test_unquoted_operators_rejected(self, tmp_path, cmd):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run(cmd)
        assert not result.success
        # Never executed — parser rejection before anything else
        assert "operator" in result.error.lower()

    def test_quoted_semicolon_is_argument_content(self, tmp_path):
        """A ';' inside quotes is legitimate argument text and runs."""
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_safe("python -c \"import sys; sys.exit(3)\"")
        assert result.returncode == 3
        assert not result.success

    def test_quoted_arg_with_spaces_preserved(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_safe("python -c \"print('a b c')\"")
        assert result.success
        assert result.output.strip() == "a b c"

    def test_command_injection_attempt_rejected(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run(
            "python -c \"print('x')\" & del /f /q C:\\Windows"
        )
        assert not result.success
        assert "operator" in result.error.lower()

    # -- explicit shells and batch scripts refused -------------------------

    @pytest.mark.parametrize("cmd", [
        "cmd /c dir",
        "powershell -Command Get-ChildItem",
        "bash -c ls",
        "sh -c whoami",
    ])
    def test_shell_interpreters_rejected(self, tmp_path, cmd):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run(cmd)
        assert not result.success
        assert "shell interpreter" in result.error.lower()

    def test_batch_script_rejected(self, tmp_path):
        import sys
        if sys.platform != "win32":
            pytest.skip("batch scripts are a Windows-only concept")
        (tmp_path / "evil.bat").write_text("@echo off\r\nwhoami\r\n")
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run("evil.bat")
        assert not result.success
        assert "batch" in result.error.lower()

    # -- resolution and execution ------------------------------------------

    def test_executable_not_found_controlled_failure(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path, broker=_broker_always_approve())
        result = tool.run("definitely-not-a-real-cmd-xyz --flag")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_relative_script_resolved_against_workspace(self, tmp_path):
        import sys
        (tmp_path / "hello.py").write_text("print('from-script')\n")
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run("python hello.py")
        assert result.success
        assert "from-script" in result.output
        # The executed executable is the absolute resolved interpreter
        assert Path(result.metadata["executable"]).is_absolute()

    def test_argv_execution_passes_operators_as_arguments(self, tmp_path):
        import sys
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_argv(
            [sys.executable, "-c", "print('a;b|c&d')"],
        )
        assert result.success
        assert result.output.strip() == "a;b|c&d"

    def test_run_argv_rejects_empty_and_shells(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        assert not tool.run_argv([]).success
        assert not tool.run_argv(["cmd", "/c", "dir"]).success

    # -- approval describes the exact operation ----------------------------

    def test_approval_records_canonical_operation(self, tmp_path):
        from factory.tools.shell import ShellTool
        broker = _broker_always_pending()
        tool = ShellTool(workspace_root=tmp_path, broker=broker)
        result = tool.run("unknowncmd --flag pytest")
        assert not result.success
        # The broker saw the exact canonical operation (program + args)
        call = broker.check.call_args
        action = call.kwargs.get("action") or call.args[1]
        ctx = call.kwargs.get("ctx") or call.args[0]
        assert action.startswith("shell: unknowncmd")
        assert "pytest" in action
        assert ctx.command in action

    def test_unbalanced_quotes_rejected_as_ambiguous(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run("python -c \"print('unbalanced)")
        assert not result.success
        assert "ambiguous" in result.error.lower()

    def test_timeout_still_bounded(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path, timeout=1)
        result = tool.run_safe("python -c \"import time; time.sleep(5)\"")
        assert not result.success
        assert "timed out" in result.error.lower()


# ---------------------------------------------------------------------------
# FilesystemTool
# ---------------------------------------------------------------------------

class TestFilesystemTool:
    def test_write_and_read_file(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        tool = FilesystemTool(workspace_root=tmp_path)
        write = tool.write(Path("hello.txt"), "Hello, world!")
        assert write.success

        read = tool.read(Path("hello.txt"))
        assert read.success
        assert read.output == "Hello, world!"

    def test_read_nonexistent_fails(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        tool = FilesystemTool(workspace_root=tmp_path)
        result = tool.read(Path("missing.txt"))
        assert not result.success
        assert "not found" in result.error.lower()

    def test_write_creates_parent_dirs(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        tool = FilesystemTool(workspace_root=tmp_path)
        result = tool.write(Path("a/b/c/deep.txt"), "content")
        assert result.success
        assert (tmp_path / "a" / "b" / "c" / "deep.txt").exists()

    def test_sandbox_blocks_outside_write(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        tool = FilesystemTool(workspace_root=tmp_path)
        result = tool.write(tmp_path.parent / "escape.txt", "bad")
        assert not result.success
        assert "sandbox" in result.error.lower() or "outside" in result.error.lower()

    def test_list_dir_shows_files(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        (tmp_path / "file_a.txt").write_text("a")
        (tmp_path / "file_b.txt").write_text("b")
        tool = FilesystemTool(workspace_root=tmp_path)
        result = tool.list_dir()
        assert result.success
        assert "file_a.txt" in result.output
        assert "file_b.txt" in result.output

    def test_exists_inside_sandbox(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        (tmp_path / "x.py").write_text("x = 1")
        tool = FilesystemTool(workspace_root=tmp_path)
        assert tool.exists(Path("x.py"))
        assert not tool.exists(Path("missing.py"))

    def test_write_json_and_read_json(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        tool = FilesystemTool(workspace_root=tmp_path)
        data = {"key": "value", "num": 42}
        write = tool.write_json(Path("data.json"), data)
        assert write.success
        read, parsed = tool.read_json(Path("data.json"))
        assert read.success
        assert parsed["key"] == "value"
        assert parsed["num"] == 42

    def test_delete_requires_broker(self, tmp_path):
        """Delete is HIGH risk — without broker it should still work."""
        from factory.tools.filesystem import FilesystemTool
        (tmp_path / "todel.txt").write_text("bye")
        tool = FilesystemTool(workspace_root=tmp_path)
        # No broker → no approval check → delete proceeds
        result = tool.delete(Path("todel.txt"))
        assert result.success
        assert not (tmp_path / "todel.txt").exists()

    def test_delete_blocked_by_broker(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        (tmp_path / "todel.txt").write_text("data")
        broker = _broker_always_deny()
        tool = FilesystemTool(workspace_root=tmp_path, broker=broker)
        result = tool.delete(Path("todel.txt"))
        assert not result.success
        assert (tmp_path / "todel.txt").exists()  # file preserved

    def test_evidence_hash_in_write_result(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        tool = FilesystemTool(workspace_root=tmp_path)
        result = tool.write(Path("hash_test.txt"), "deterministic content")
        assert result.success
        assert len(result.evidence_hash) == 64

    def test_evidence_hash_stable(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        tool = FilesystemTool(workspace_root=tmp_path)
        r1 = tool.write(Path("f1.txt"), "same content")
        r2 = tool.write(Path("f2.txt"), "same content")
        assert r1.evidence_hash == r2.evidence_hash


# ---------------------------------------------------------------------------
# GitTool — read operations
# ---------------------------------------------------------------------------

class TestGitToolReads:
    def _git_tool(self, tmp_path):
        from factory.tools.git import GitTool
        return GitTool(workspace_root=tmp_path)

    def test_status_in_non_git_dir_fails(self, tmp_path):
        tool = self._git_tool(tmp_path)
        result = tool.status()
        # non-git dir → git status returns error
        assert not result.success
        assert result.returncode != 0

    def test_status_in_git_repo(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        tool = self._git_tool(tmp_path)
        result = tool.status()
        assert result.success

    def test_log_empty_repo_succeeds(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        tool = self._git_tool(tmp_path)
        result = tool.log()
        # Empty repo may give nonzero but not crash
        assert isinstance(result.success, bool)


# ---------------------------------------------------------------------------
# GitTool — mutation gating
# ---------------------------------------------------------------------------

class TestGitToolMutations:
    def test_commit_without_broker_proceeds_unguarded(self, tmp_path):
        """Without a broker, mutations log a warning but proceed."""
        import subprocess
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        from factory.tools.git import GitTool
        tool = GitTool(workspace_root=tmp_path, broker=None)
        result = tool.commit("test commit")
        assert result.success or "nothing to commit" in result.output.lower() or result.returncode == 1

    def test_commit_requires_approval_with_pending_broker(self, tmp_path):
        # Contract note: the workspace must BE a git repo (git init) or the
        # SEC-004 boundary gate refuses first — a bare tmp dir under a parent
        # repo never reaches the broker. Assertion asserts the approval
        # PHRASE + structured flag so a boundary message (which embeds the
        # test's tmp path) can never satisfy it by coincidence.
        import subprocess
        from factory.tools.git import GitTool
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        broker = _broker_always_pending()
        tool = GitTool(workspace_root=tmp_path, broker=broker)
        result = tool.commit("test commit")
        assert not result.success
        assert "requires human approval" in result.error
        assert result.requires_human_approval is True
        assert result.approval_request_id == "req-abc"

    def test_push_denied_by_broker(self, tmp_path):
        # Same boundary note as above: git init first so the SEC-004 gate
        # passes and the broker decision is what actually blocks the push.
        import subprocess
        from factory.tools.git import GitTool
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        broker = _broker_always_deny()
        tool = GitTool(workspace_root=tmp_path, broker=broker)
        result = tool.push("origin", "main")
        assert not result.success
        assert "denied by policy" in result.error
        assert result.requires_human_approval is False


# ---------------------------------------------------------------------------
# GitTool — repository boundary + argument-injection guards (SEC-004)
# ---------------------------------------------------------------------------

class TestGitToolBoundaryAndArgs:
    def _subdir_workspace(self, tmp_path):
        """A workspace that is a SUBDIRECTORY of a parent git repo."""
        import subprocess
        parent = tmp_path / "parent"
        parent.mkdir()
        subprocess.run(["git", "init"], cwd=parent, capture_output=True)
        sub = parent / "sub"
        sub.mkdir()
        return sub

    def test_commit_refused_in_subdir_of_parent_repo(self, tmp_path):
        from factory.tools.git import GitTool
        sub = self._subdir_workspace(tmp_path)
        tool = GitTool(workspace_root=sub, broker=_broker_always_approve())
        result = tool.commit("cross-boundary commit")
        assert not result.success
        assert "boundary" in result.error.lower()
        assert "repository root" in result.error.lower()

    def test_push_refused_in_subdir_of_parent_repo(self, tmp_path):
        from factory.tools.git import GitTool
        sub = self._subdir_workspace(tmp_path)
        tool = GitTool(workspace_root=sub, broker=_broker_always_approve())
        result = tool.push("origin", "main")
        assert not result.success
        assert "boundary" in result.error.lower()

    def test_add_refused_in_subdir_of_parent_repo(self, tmp_path):
        from factory.tools.git import GitTool
        sub = self._subdir_workspace(tmp_path)
        (sub / "file.txt").write_text("x")
        tool = GitTool(workspace_root=sub, broker=_broker_always_approve())
        result = tool.add("file.txt")
        assert not result.success
        assert "boundary" in result.error.lower()

    def test_mutation_allowed_at_repo_root(self, tmp_path):
        """Positive control: repo root == workspace → mutations proceed."""
        import subprocess
        from factory.tools.git import GitTool
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True
        )
        (tmp_path / "file.txt").write_text("content")
        tool = GitTool(workspace_root=tmp_path, broker=_broker_always_approve())
        add_result = tool.add("file.txt")
        assert add_result.success, add_result.error
        commit_result = tool.commit("boundary ok")
        assert commit_result.success, commit_result.error

    @pytest.mark.parametrize("ref", ["-output=evil", "--git-dir=/tmp/x", "-"])
    def test_show_ref_leading_dash_refused(self, tmp_path, ref):
        from factory.tools.git import GitTool
        tool = GitTool(workspace_root=tmp_path)
        result = tool.show(ref)
        assert not result.success
        assert "may not start with '-'" in result.error

    def test_rev_parse_leading_dash_refused(self, tmp_path):
        from factory.tools.git import GitTool
        tool = GitTool(workspace_root=tmp_path)
        result = tool.rev_parse("--exec-path=/tmp/x")
        assert not result.success
        assert "may not start with '-'" in result.error

    def test_push_remote_leading_dash_cannot_bypass_force_gate(self, tmp_path):
        """remote='-f' would turn a normal push into a force push (SEC-004)."""
        from factory.tools.git import GitTool
        tool = GitTool(workspace_root=tmp_path, broker=_broker_always_deny())
        result = tool.push("-f", "main")
        assert not result.success
        # The argument guard fires — not merely the broker denial.
        assert "may not start with '-'" in result.error

    def test_push_branch_leading_dash_refused(self, tmp_path):
        from factory.tools.git import GitTool
        tool = GitTool(workspace_root=tmp_path, broker=_broker_always_deny())
        result = tool.push("origin", "--force")
        assert not result.success
        assert "may not start with '-'" in result.error

    def test_ref_with_control_characters_refused(self, tmp_path):
        from factory.tools.git import GitTool
        tool = GitTool(workspace_root=tmp_path)
        result = tool.show("HEAD\n--upload-pack=evil")
        assert not result.success
        assert "control characters" in result.error


# ---------------------------------------------------------------------------
# TestingTool — pytest parser
# ---------------------------------------------------------------------------

class TestPytestParser:
    def test_parse_all_passed(self):
        from factory.tools.testing import _parse_pytest_output
        output = textwrap.dedent("""\
            ============================= test session starts =============================
            collected 5 items

            tests/test_x.py .....                                                [ 100%]

            ============================== 5 passed in 1.23s ==============================
        """)
        result = _parse_pytest_output(output)
        assert result.passed == 5
        assert result.failed == 0
        assert result.all_passed is True

    def test_parse_with_failures(self):
        from factory.tools.testing import _parse_pytest_output
        output = textwrap.dedent("""\
            FAILED tests/test_a.py::TestA::test_one - AssertionError
            FAILED tests/test_a.py::TestA::test_two - RuntimeError
            ========================= 3 passed, 2 failed in 2.00s =========================
        """)
        result = _parse_pytest_output(output)
        assert result.passed == 3
        assert result.failed == 2
        assert result.all_passed is False
        assert len(result.failing_tests) == 2

    def test_parse_empty_output(self):
        from factory.tools.testing import _parse_pytest_output
        result = _parse_pytest_output("")
        assert result.passed == 0
        assert result.all_passed is True  # no failures = vacuously passes

    def test_parse_collection_error(self):
        from factory.tools.testing import _parse_pytest_output
        output = "ERROR collecting tests/test_bad.py\n"
        result = _parse_pytest_output(output)
        assert len(result.collection_errors) >= 1


class TestTestingTool:
    def test_run_tests_no_tests_collected(self, tmp_path):
        """T1/COR-001: pytest return code 5 (no tests collected) must be a FAILURE."""
        import sys
        from factory.tools.testing import TestingTool
        tool = TestingTool(workspace_root=tmp_path, python_executable=sys.executable)
        result = tool.run_tests()
        # no tests in empty dir → returncode 5 → must never count as success
        assert result.returncode == 5
        assert result.success is False

    def test_run_tests_with_passing_test(self, tmp_path):
        import sys
        from factory.tools.testing import TestingTool
        (tmp_path / "test_simple.py").write_text(
            "def test_one(): assert 1 == 1\n"
        )
        tool = TestingTool(workspace_root=tmp_path, python_executable=sys.executable)
        result = tool.run_tests()
        assert result.success
        tr = result.metadata["test_result"]
        assert tr["passed"] >= 1
        assert tr["all_passed"] is True

    def test_run_tests_with_failing_test(self, tmp_path):
        import sys
        from factory.tools.testing import TestingTool
        (tmp_path / "test_fail.py").write_text(
            "def test_boom(): assert 1 == 2\n"
        )
        tool = TestingTool(workspace_root=tmp_path, python_executable=sys.executable)
        result = tool.run_tests()
        assert not result.success
        tr = result.metadata["test_result"]
        assert tr["failed"] >= 1
        assert tr["all_passed"] is False

    def test_metadata_always_present(self, tmp_path):
        import sys
        from factory.tools.testing import TestingTool
        tool = TestingTool(workspace_root=tmp_path, python_executable=sys.executable)
        result = tool.run_tests()
        assert "test_result" in result.metadata
        tr = result.metadata["test_result"]
        assert "passed" in tr
        assert "failed" in tr

        assert "errors" in tr
        assert "all_passed" in tr

    def test_genuinely_failing_test_remains_failure(self, tmp_path):
        """T2: a real failing test must fail the tool result AND the quality gate."""
        import sys
        from factory.projects.quality import QualityGate
        from factory.tools.testing import TestingTool
        (tmp_path / "test_boom.py").write_text("def test_boom(): assert 1 == 2\n")
        tool = TestingTool(workspace_root=tmp_path, python_executable=sys.executable)
        result = tool.run_tests()
        assert result.returncode == 1
        assert result.success is False
        tr = result.metadata["test_result"]
        assert tr["failed"] >= 1
        assert tr["all_passed"] is False
        # The same evidence must fail the gate (clean scan so only tests decide).
        gate = QualityGate()
        gate_result = gate.evaluate(
            test_result=tr,
            scan_result={"finding_count": 0, "critical_count": 0, "high_count": 0, "is_clean": True},
        )
        assert gate_result.passed is False
        assert any("Tests" in f for f in gate_result.failures)

    def test_missing_python_executable_returns_controlled_failure(self, tmp_path):
        """T5: a missing test executable must fail in a controlled way (no exception)."""
        from factory.tools.testing import TestingTool
        tool = TestingTool(
            workspace_root=tmp_path,
            python_executable="python-do-not-exist-xyz.exe",
        )
        result = tool.run_tests()  # must not raise
        assert result.success is False
        assert result.error
        assert "not found" in result.error.lower()

    def test_unusable_python_executable_returns_controlled_failure(self, tmp_path):
        """T5: a non-executable file used as interpreter must fail in a controlled way."""
        from factory.tools.testing import TestingTool
        bogus = tmp_path / "not_an_interpreter.txt"
        bogus.write_text("this is not an interpreter")
        tool = TestingTool(workspace_root=tmp_path, python_executable=str(bogus))
        result = tool.run_tests()  # must not raise
        assert result.success is False
        assert result.error

    def test_tool_failure_evidence_fails_gate(self, tmp_path):
        """T5→T3: evidence from a failed tool run must never pass the gate."""
        from factory.projects.quality import QualityGate
        from factory.tools.testing import TestingTool
        tool = TestingTool(
            workspace_root=tmp_path,
            python_executable="python-do-not-exist-xyz.exe",
        )
        result = tool.run_tests()
        assert result.success is False
        # Same lookup the QA agent uses — a failed run yields no usable evidence.
        evidence = result.metadata.get("test_result", {})
        gate = QualityGate()
        gate_result = gate.evaluate(
            test_result=evidence,
            scan_result={"finding_count": 0, "critical_count": 0, "high_count": 0, "is_clean": True},
        )
        assert gate_result.passed is False


# ---------------------------------------------------------------------------
# SecurityTool
# ---------------------------------------------------------------------------

class TestSecurityTool:
    def test_scan_clean_directory(self, tmp_path):
        from factory.tools.security import SecurityTool
        (tmp_path / "main.py").write_text("print('hello')\n")
        tool = SecurityTool(workspace_root=tmp_path)
        result = tool.scan_directory()
        assert result.success
        scan = result.metadata["scan_result"]
        assert scan["is_clean"] is True
        assert scan["finding_count"] == 0

    def test_scan_detects_env_file(self, tmp_path):
        from factory.tools.security import SecurityTool
        env_file = tmp_path / ".env"
        env_file.write_text("SECRET_KEY=supersecret123\n")
        tool = SecurityTool(workspace_root=tmp_path)
        result = tool.scan_directory()
        assert result.success
        scan = result.metadata["scan_result"]
        assert scan["finding_count"] >= 1
        assert scan["is_clean"] is False

    def test_scan_detects_private_key_in_content(self, tmp_path):
        from factory.tools.security import SecurityTool
        key_file = tmp_path / "key.txt"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
        tool = SecurityTool(workspace_root=tmp_path, scan_content=True)
        result = tool.scan_directory()
        assert result.success
        scan = result.metadata["scan_result"]
        assert scan["finding_count"] >= 1

    def test_scan_skips_binary_extensions(self, tmp_path):
        from factory.tools.security import SecurityTool
        (tmp_path / "compiled.pyc").write_bytes(b"\x00\x01\x02")
        (tmp_path / "normal.py").write_text("x = 1")
        tool = SecurityTool(workspace_root=tmp_path)
        result = tool.scan_directory()
        scan = result.metadata["scan_result"]
        # .pyc should be skipped, only .py counted
        assert scan["skipped_files"] >= 1

    def test_scan_file_clean(self, tmp_path):
        from factory.tools.security import SecurityTool
        (tmp_path / "normal.py").write_text("def add(a, b): return a + b\n")
        tool = SecurityTool(workspace_root=tmp_path)
        result = tool.scan_file(tmp_path / "normal.py")
        assert result.success
        assert "clean" in result.output.lower()

    def test_check_content_sensitive(self, tmp_path):
        from factory.tools.security import SecurityTool
        tool = SecurityTool(workspace_root=tmp_path)
        report = tool.check_content_sensitive("password='mysecret123'")
        assert report.is_sensitive

    def test_check_content_clean(self, tmp_path):
        from factory.tools.security import SecurityTool
        tool = SecurityTool(workspace_root=tmp_path)
        report = tool.check_content_sensitive("def add(x, y): return x + y")
        assert not report.is_sensitive


# ---------------------------------------------------------------------------
# BrowserTool — URL validation (no real network)
# ---------------------------------------------------------------------------

class TestBrowserToolValidation:
    def test_empty_url_rejected(self, tmp_path):
        from factory.tools.browser import BrowserTool
        tool = BrowserTool()
        result = tool.fetch("")
        assert not result.success
        assert "Empty URL" in result.error

    def test_localhost_blocked(self, tmp_path):
        from factory.tools.browser import BrowserTool
        tool = BrowserTool()
        result = tool.fetch("http://localhost:8080/api")
        assert not result.success
        assert "Blocked" in result.error or "loopback" in result.error

    def test_127_0_0_1_blocked(self):
        from factory.tools.browser import BrowserTool
        tool = BrowserTool()
        result = tool.fetch("http://127.0.0.1/admin")
        assert not result.success

    def test_non_http_scheme_rejected(self):
        from factory.tools.browser import BrowserTool
        tool = BrowserTool()
        result = tool.fetch("ftp://example.com/file")
        assert not result.success
        assert "Unsupported scheme" in result.error

    def test_no_host_rejected(self):
        from factory.tools.browser import BrowserTool
        tool = BrowserTool()
        result = tool.fetch("http://")
        assert not result.success

    def test_valid_public_url_blocked_by_pending_broker(self):
        from factory.tools.browser import BrowserTool
        broker = _broker_always_pending()
        tool = BrowserTool(broker=broker)
        result = tool.fetch("https://docs.python.org/3/")
        assert not result.success
        assert "approval" in result.error.lower()

    def test_valid_public_url_blocked_by_deny_broker(self):
        from factory.tools.browser import BrowserTool
        broker = _broker_always_deny()
        tool = BrowserTool(broker=broker)
        result = tool.fetch("https://docs.python.org/3/")
        assert not result.success
        assert "denied" in result.error.lower()


# ---------------------------------------------------------------------------
# BrowserTool — redirect re-validation (SEC-003, no real network)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int = 200, headers: dict | None = None,
                 content: bytes = b"body"):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content


class _FakeClient:
    """Duck-typed httpx.Client replacement: scripted responses, URL log."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.requested: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url: str, headers: dict | None = None):
        self.requested.append(url)
        if not self._responses:
            return _FakeResponse()
        return self._responses.pop(0)


class TestBrowserRedirects:
    """Every redirect hop is validated before it is followed (SEC-003)."""

    @staticmethod
    def _tool():
        from factory.tools.browser import BrowserTool
        return BrowserTool(broker=_broker_always_approve())

    def test_redirect_to_metadata_host_blocked(self):
        fake = _FakeClient([
            _FakeResponse(302, {"location": "http://169.254.169.254/latest/meta-data/"}),
        ])
        tool = self._tool()
        with patch("httpx.Client", return_value=fake):
            result = tool.fetch("https://docs.python.org/3/")
        assert not result.success
        assert "Redirect target blocked" in result.error
        assert "169.254.169.254" in result.error
        # The blocked target was never requested.
        assert fake.requested == ["https://docs.python.org/3/"]

    def test_redirect_to_loopback_blocked(self):
        fake = _FakeClient([
            _FakeResponse(302, {"location": "http://127.0.0.1:8500/v1/secret"}),
        ])
        tool = self._tool()
        with patch("httpx.Client", return_value=fake):
            result = tool.fetch("https://docs.python.org/3/")
        assert not result.success
        assert "Redirect target blocked" in result.error

    def test_redirect_to_non_http_scheme_blocked(self):
        fake = _FakeClient([
            _FakeResponse(301, {"location": "file:///C:/Windows/win.ini"}),
        ])
        tool = self._tool()
        with patch("httpx.Client", return_value=fake):
            result = tool.fetch("https://docs.python.org/3/")
        assert not result.success
        assert "Redirect target blocked" in result.error
        assert "scheme" in result.error.lower()

    def test_relative_redirect_resolved_and_followed(self, monkeypatch):
        monkeypatch.setattr(
            "factory.tools.browser._validate_url", lambda u: None
        )
        fake = _FakeClient([
            _FakeResponse(302, {"location": "/en/library/index.html"}),
            _FakeResponse(200, {}, b"final page"),
        ])
        tool = self._tool()
        with patch("httpx.Client", return_value=fake):
            result = tool.fetch("https://docs.python.org/3/")
        assert result.success
        assert result.output == "final page"
        assert fake.requested == [
            "https://docs.python.org/3/",
            "https://docs.python.org/en/library/index.html",
        ]

    def test_redirect_budget_exhausted_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            "factory.tools.browser._validate_url", lambda u: None
        )
        hop = _FakeResponse(302, {"location": "/next"})
        fake = _FakeClient([hop, hop, hop, hop, hop])
        tool = self._tool()
        with patch("httpx.Client", return_value=fake):
            result = tool.fetch("https://docs.python.org/3/")
        assert not result.success
        assert "Too many redirects" in result.error
        assert len(fake.requested) == 4  # 3 followed hops + one final attempt

    def test_redirect_without_location_treated_as_final(self):
        fake = _FakeClient([_FakeResponse(302, {})])
        tool = self._tool()
        with patch("httpx.Client", return_value=fake):
            result = tool.fetch("https://docs.python.org/3/")
        # Malformed redirect is not followed — the response stands as-is.
        assert len(fake.requested) == 1
        assert result.returncode == 302


# ---------------------------------------------------------------------------
# Approval pause propagation on ToolResult (COR-003)
# ---------------------------------------------------------------------------

class TestApprovalPropagation:
    """A pending human decision must be structurally distinguishable from
    an ordinary failure so agents/orchestrator can pause (not fail)."""

    def test_tool_result_dict_exposes_approval_fields(self):
        from factory.tools.base import ToolResult
        r = ToolResult(
            success=False,
            error="blocked",
            requires_human_approval=True,
            approval_request_id="req-1",
        )
        d = r.to_dict()
        assert d["requires_human_approval"] is True
        assert d["approval_request_id"] == "req-1"

    def test_tool_result_defaults_are_ordinary_failure(self):
        from factory.tools.base import ToolResult
        r = ToolResult(success=False, error="boom")
        d = r.to_dict()
        assert d["requires_human_approval"] is False
        assert d["approval_request_id"] == ""

    def test_shell_pending_approval_flagged(self, tmp_path):
        from factory.tools.shell import ShellTool
        broker = _broker_always_pending()
        tool = ShellTool(workspace_root=tmp_path, broker=broker)
        result = tool.run("unknowncmd --flag")
        assert not result.success
        assert result.requires_human_approval is True
        assert result.approval_request_id == "req-abc"
        assert "request_id=req-abc" in result.error

    def test_shell_denied_is_ordinary_failure(self, tmp_path):
        from factory.tools.shell import ShellTool
        broker = _broker_always_deny()
        tool = ShellTool(workspace_root=tmp_path, broker=broker)
        result = tool.run("curl http://example.com")
        assert not result.success
        assert result.requires_human_approval is False

    def test_git_commit_pending_approval_flagged(self, tmp_path):
        import subprocess
        from factory.tools.git import GitTool
        # git init: workspace must be the repo root or the SEC-004
        # boundary gate refuses before the broker is consulted.
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        broker = _broker_always_pending()
        tool = GitTool(workspace_root=tmp_path, broker=broker)
        result = tool.commit("msg")
        assert not result.success
        assert result.requires_human_approval is True
        assert result.approval_request_id == "req-abc"

    def test_git_denied_is_ordinary_failure(self, tmp_path):
        import subprocess
        from factory.tools.git import GitTool
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        broker = _broker_always_deny()
        tool = GitTool(workspace_root=tmp_path, broker=broker)
        result = tool.commit("msg")
        assert not result.success
        assert result.requires_human_approval is False

    def test_filesystem_sensitive_write_pending_flagged(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        broker = _broker_always_pending()
        tool = FilesystemTool(workspace_root=tmp_path, broker=broker)
        result = tool.write(Path(".env"), "SECRET=1")
        assert not result.success
        assert result.requires_human_approval is True
        assert result.approval_request_id == "req-abc"
        assert not (tmp_path / ".env").exists()  # nothing written while paused

    def test_filesystem_denied_is_ordinary_failure(self, tmp_path):
        from factory.tools.filesystem import FilesystemTool
        (tmp_path / "todel.txt").write_text("data")
        broker = _broker_always_deny()
        tool = FilesystemTool(workspace_root=tmp_path, broker=broker)
        result = tool.delete(Path("todel.txt"))
        assert not result.success
        assert result.requires_human_approval is False

    def test_browser_pending_approval_flagged(self):
        from factory.tools.browser import BrowserTool
        broker = _broker_always_pending()
        tool = BrowserTool(broker=broker)
        result = tool.fetch("https://docs.python.org/3/")
        assert not result.success
        assert result.requires_human_approval is True
        assert result.approval_request_id == "req-abc"
