"""
factory.tools.testing
======================
Test execution tool — runs pytest within the project workspace.

Security model
--------------
* Tests run as a subprocess within the workspace sandbox.
* The command is classified as SAFE (pytest is in the safe-prefix list).
* Output is parsed into a structured TestResult for downstream quality gates.
* Test output is hashed and attached as evidence.

Usage::

    tool = TestingTool(workspace_root=Path("/project"))
    result = tool.run_tests()
    if result.success:
        test_data = result.metadata["test_result"]
        print(f"Passed: {test_data['passed']}, Failed: {test_data['failed']}")
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_PYTEST_TIMEOUT = 300  # 5 minutes max

# Pattern: "5 passed, 2 failed, 1 error in 3.45s"
_SUMMARY_PATTERN = re.compile(
    r"(?:(\d+)\s+passed)?[,\s]*"
    r"(?:(\d+)\s+failed)?[,\s]*"
    r"(?:(\d+)\s+error[s]?)?[,\s]*"
    r"(?:(\d+)\s+warning[s]?)?",
    re.IGNORECASE,
)
_DURATION_PATTERN = re.compile(r"in\s+([\d.]+)s", re.IGNORECASE)


@dataclass
class TestResult:
    """Structured summary of a pytest run."""
    passed: int = 0
    failed: int = 0
    errors: int = 0
    warnings: int = 0
    duration_s: float = 0.0
    all_passed: bool = False
    raw_summary: str = ""
    failing_tests: list[str] = field(default_factory=list)
    collection_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "warnings": self.warnings,
            "duration_s": self.duration_s,
            "all_passed": self.all_passed,
            "raw_summary": self.raw_summary,
            "failing_tests": self.failing_tests,
            "collection_errors": self.collection_errors,
        }


def _parse_pytest_output(output: str) -> TestResult:
    """
    Parse pytest stdout/stderr into a structured TestResult.

    Handles two summary line formats:
    - Verbose: ``============================== 5 passed in 1.23s ==============================``
    - Quiet:   ``1 passed in 0.02s``
    """
    result = TestResult()
    lines = output.splitlines()

    # Find the last line that looks like a summary:
    # either starts/ends with "=" (verbose) or is a bare "N passed/failed in Xs"
    summary_line = ""
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Verbose: === N passed/failed === lines
        if stripped.startswith("=") and stripped.endswith("="):
            if any(kw in stripped for kw in ("passed", "failed", "error", "no tests")):
                summary_line = stripped
                break
        # Quiet: bare "N passed in Xs" or "N passed, M failed in Xs"
        if re.search(r"\d+\s+(?:passed|failed|error)", stripped):
            if re.search(r"\bin\s+[\d.]+s\b", stripped):
                summary_line = stripped
                break

    result.raw_summary = summary_line

    if summary_line:
        # Extract individual counts with targeted patterns
        m_passed = re.search(r"(\d+)\s+passed", summary_line)
        m_failed = re.search(r"(\d+)\s+failed", summary_line)
        m_error = re.search(r"(\d+)\s+error", summary_line, re.IGNORECASE)
        m_warn = re.search(r"(\d+)\s+warning", summary_line, re.IGNORECASE)
        m_dur = re.search(r"in\s+([\d.]+)s", summary_line)

        result.passed = int(m_passed.group(1)) if m_passed else 0
        result.failed = int(m_failed.group(1)) if m_failed else 0
        result.errors = int(m_error.group(1)) if m_error else 0
        result.warnings = int(m_warn.group(1)) if m_warn else 0
        result.duration_s = float(m_dur.group(1)) if m_dur else 0.0

    result.all_passed = result.failed == 0 and result.errors == 0

    # Collect failing test names from "FAILED" lines
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            test_name = stripped[len("FAILED "):].split(" - ")[0].strip()
            result.failing_tests.append(test_name)
        elif "ERROR collecting" in stripped:
            result.collection_errors.append(stripped)

    return result



class TestingTool(BaseTool):
    """
    Runs pytest within the project workspace and returns structured results.
    """

    tool_name = "testing"

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        timeout: int = _PYTEST_TIMEOUT,
        python_executable: str | None = None,
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
        # Default to sys.executable so tests run in the same venv as the factory
        import sys as _sys
        self.python_executable = python_executable or _sys.executable


    def run_tests(
        self,
        *,
        test_path: str = "",
        extra_args: list[str] | None = None,
        task_id: str | None = None,
    ) -> ToolResult:
        """
        Run pytest and return a ToolResult with structured TestResult in metadata.

        Parameters
        ----------
        test_path:
            Optional path to restrict test discovery (e.g. "tests/unit/").
        extra_args:
            Additional pytest flags (e.g. ["-v", "--tb=short"]).
        task_id:
            For audit context.
        """
        args = [self.python_executable, "-m", "pytest", "--tb=short", "-q"]
        if test_path:
            args.append(test_path)
        if extra_args:
            args.extend(extra_args)

        command = " ".join(args)
        cwd = self.workspace_root

        logger.info("TestingTool: running %s (cwd=%s)", command, cwd)

        start = time.monotonic()
        try:
            proc = subprocess.run(
                args,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            duration_ms = (time.monotonic() - start) * 1000
        except subprocess.TimeoutExpired:
            duration_ms = self.timeout * 1000
            self._audit_emit("tool.testing.timeout", {
                "command": command,
                "timeout_s": self.timeout,
            })
            return self._fail(
                f"pytest timed out after {self.timeout}s",
                command=command,
            )
        except FileNotFoundError:
            return self._fail(
                f"Python executable not found: {self.python_executable}",
                command=command,
            )
        except OSError as exc:
            return self._fail(str(exc), command=command)

        combined_output = (proc.stdout or "") + (proc.stderr or "")
        test_result = _parse_pytest_output(combined_output)
        test_result.duration_s = duration_ms / 1000

        # pytest returns 0 = all passed, 1 = failures, 2 = interrupted,
        # 3 = internal error, 4 = usage error, 5 = no tests collected.
        # COR-001: only exit code 0 counts as success; 5 (no tests collected)
        # fails closed — an empty suite must never pass the gate.
        success = proc.returncode == 0

        self._audit_emit("tool.testing.completed", {
            "passed": test_result.passed,
            "failed": test_result.failed,
            "errors": test_result.errors,
            "duration_s": round(test_result.duration_s, 2),
            "all_passed": test_result.all_passed,
            "returncode": proc.returncode,
        })

        logger.info(
            "TestingTool: %d passed, %d failed, %d errors (rc=%d).",
            test_result.passed, test_result.failed,
            test_result.errors, proc.returncode,
        )

        return ToolResult(
            success=success and test_result.all_passed,
            output=combined_output,
            error=proc.stderr if not success else "",
            tool_name=self.tool_name,
            command=command,
            returncode=proc.returncode,
            duration_ms=duration_ms,
            metadata={"test_result": test_result.to_dict()},
        )

    def run_single_test(
        self,
        test_id: str,
        *,
        task_id: str | None = None,
    ) -> ToolResult:
        """
        Run a single test by its pytest node ID (e.g. "tests/unit/test_x.py::TestX::test_y").
        """
        return self.run_tests(
            test_path=test_id,
            extra_args=["-v"],
            task_id=task_id,
        )
