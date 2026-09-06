"""
factory.tools.security
=======================
Static security scanning tool — pattern-based, no LLM.

Wraps SensitiveDataDetector for systematic workspace scanning.  Produces
structured ScanResult objects that feed into quality gates and audit reports.

Usage::

    tool = SecurityTool(workspace_root=Path("/project"))
    result = tool.scan_directory()
    if result.success:
        findings = result.metadata["scan_result"]["findings"]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.permissions.sensitive_data import SensitiveDataDetector, SensitivityReport
from factory.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

_detector = SensitiveDataDetector()

# Extensions we skip during directory scans (binary/noisy)
_SKIP_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".whl",
    ".db", ".sqlite", ".sqlite3",
})

_MAX_SCAN_FILES = 500  # prevent runaway scans


@dataclass
class ScanFinding:
    """A single security finding from a scan."""
    path: str
    reasons: list[str]
    severity: str  # "safe", "low", "medium", "high", "critical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "reasons": self.reasons,
            "severity": self.severity,
        }


@dataclass
class ScanResult:
    """Structured output of a security scan."""
    total_files_scanned: int = 0
    findings: list[ScanFinding] = field(default_factory=list)
    skipped_files: int = 0
    # Aggregate severity: highest severity found across all findings
    max_severity: str = "safe"

    _SEVERITY_ORDER = ["safe", "low", "medium", "high", "critical"]

    def add_finding(self, finding: ScanFinding) -> None:
        self.findings.append(finding)
        # Update max_severity
        if (self._SEVERITY_ORDER.index(finding.severity) >
                self._SEVERITY_ORDER.index(self.max_severity)):
            self.max_severity = finding.severity

    @property
    def is_clean(self) -> bool:
        """True if no sensitive findings exist."""
        return len(self.findings) == 0

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "high")

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_files_scanned": self.total_files_scanned,
            "skipped_files": self.skipped_files,
            "finding_count": len(self.findings),
            "max_severity": self.max_severity,
            "is_clean": self.is_clean,
            "critical_count": self.critical_count,
            "high_count": self.high_count,
            "findings": [f.to_dict() for f in self.findings],
        }


class SecurityTool(BaseTool):
    """
    Pattern-based static security scanner.

    Uses SensitiveDataDetector for path-level and content-level checks.
    Does not involve an LLM at any point.
    """

    tool_name = "security"

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        scan_content: bool = True,
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
        self.scan_content = scan_content

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan_file(self, path: Path) -> ToolResult:
        """
        Scan a single file for sensitive content.

        Returns a ToolResult with ScanResult in metadata.
        """
        try:
            safe_path = self._sandbox_path(path)
        except Exception as exc:
            return self._fail(str(exc), command=f"scan:{path}")

        if not safe_path.exists() or not safe_path.is_file():
            return self._fail(f"File not found: {safe_path}", command=f"scan:{path}")

        report = _detector.check_file(safe_path, read_content=self.scan_content)

        scan = ScanResult(total_files_scanned=1)
        if report.is_sensitive:
            scan.add_finding(ScanFinding(
                path=str(safe_path),
                reasons=report.reasons,
                severity=report.severity,
            ))

        summary = (
            f"File {safe_path.name}: "
            + ("SENSITIVE — " + "; ".join(report.reasons) if report.is_sensitive
               else "clean")
        )

        self._audit_emit("tool.security.scan_file", {
            "path": str(safe_path),
            "is_sensitive": report.is_sensitive,
            "severity": report.severity,
        })

        return ToolResult(
            success=True,
            output=summary,
            tool_name=self.tool_name,
            command=f"scan:{safe_path}",
            metadata={"scan_result": scan.to_dict()},
        )

    def scan_directory(
        self,
        path: Path | None = None,
        *,
        recursive: bool = True,
        task_id: str | None = None,
    ) -> ToolResult:
        """
        Scan all files in a directory for sensitive content.

        Returns a ToolResult with aggregated ScanResult in metadata.
        """
        target = path if path is not None else (self.workspace_root or Path("."))
        try:
            safe_target = self._sandbox_path(target)
        except Exception as exc:
            return self._fail(str(exc), command=f"scan_dir:{target}")

        if not safe_target.exists():
            return self._fail(
                f"Directory not found: {safe_target}",
                command=f"scan_dir:{target}",
            )

        if not safe_target.is_dir():
            # If given a file, delegate to scan_file
            return self.scan_file(safe_target)

        scan = ScanResult()
        glob_fn = safe_target.rglob if recursive else safe_target.glob
        files = list(glob_fn("*"))

        for file_path in files:
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() in _SKIP_EXTENSIONS:
                scan.skipped_files += 1
                continue
            if scan.total_files_scanned >= _MAX_SCAN_FILES:
                logger.warning(
                    "SecurityTool: scan limit (%d files) reached at %s.",
                    _MAX_SCAN_FILES, safe_target,
                )
                break

            scan.total_files_scanned += 1
            report = _detector.check_file(file_path, read_content=self.scan_content)
            if report.is_sensitive:
                scan.add_finding(ScanFinding(
                    path=str(file_path.relative_to(safe_target)),
                    reasons=report.reasons,
                    severity=report.severity,
                ))

        summary_lines = [
            f"Scanned {scan.total_files_scanned} files "
            f"(skipped {scan.skipped_files} binary/non-text).",
            f"Findings: {len(scan.findings)} "
            f"(critical={scan.critical_count}, high={scan.high_count}).",
            f"Max severity: {scan.max_severity}.",
        ]
        if scan.findings:
            summary_lines.append("Sensitive files:")
            for finding in scan.findings[:10]:  # cap output
                summary_lines.append(f"  [{finding.severity.upper()}] {finding.path}")

        summary = "\n".join(summary_lines)

        self._audit_emit("tool.security.scan_directory", {
            "path": str(safe_target),
            "total_files": scan.total_files_scanned,
            "findings": len(scan.findings),
            "max_severity": scan.max_severity,
        })

        logger.info(
            "SecurityTool: scanned %d files, %d findings (max=%s).",
            scan.total_files_scanned, len(scan.findings), scan.max_severity,
        )

        return ToolResult(
            success=True,
            output=summary,
            tool_name=self.tool_name,
            command=f"scan_dir:{safe_target}",
            metadata={"scan_result": scan.to_dict()},
        )

    def check_path_sensitive(self, path: Path) -> bool:
        """Quick boolean check — True if path is considered sensitive."""
        return _detector.is_sensitive_path(path)

    def check_content_sensitive(self, content: str) -> SensitivityReport:
        """Check string content for sensitive patterns."""
        return _detector.check_content(content)
