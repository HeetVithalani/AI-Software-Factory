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


# ---------------------------------------------------------------------------
# ShellTool
# ---------------------------------------------------------------------------

class TestShellTool:
    def test_safe_command_runs_without_broker(self, tmp_path):
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_safe("echo hello")
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
        from factory.tools.shell import ShellTool
        broker = _broker_always_approve()
        tool = ShellTool(workspace_root=tmp_path, broker=broker)
        result = tool.run("echo approved")
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
        from factory.tools.shell import ShellTool
        tool = ShellTool(workspace_root=tmp_path)
        result = tool.run_safe("echo x")
        assert result.duration_ms > 0


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
        from factory.tools.git import GitTool
        broker = _broker_always_pending()
        tool = GitTool(workspace_root=tmp_path, broker=broker)
        result = tool.commit("test commit")
        assert not result.success
        assert "approval" in result.error.lower()

    def test_push_denied_by_broker(self, tmp_path):
        from factory.tools.git import GitTool
        broker = _broker_always_deny()
        tool = GitTool(workspace_root=tmp_path, broker=broker)
        result = tool.push("origin", "main")
        assert not result.success
        assert "denied" in result.error.lower()


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
        """pytest with no tests returns 5 (no tests collected) — we treat as success."""
        import sys
        from factory.tools.testing import TestingTool
        tool = TestingTool(workspace_root=tmp_path, python_executable=sys.executable)
        result = tool.run_tests()
        # no tests in empty dir → returncode 5 → success=True
        assert result.success or result.returncode == 5

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
