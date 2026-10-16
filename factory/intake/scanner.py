"""
factory.intake.scanner
=======================
SkillSpector scanner boundary for the tool intake (Phase 2a).

Design rules (fail-closed):
* Fixed argv list, ``shell=False`` — never a shell string, never
  tool-supplied text in the command line. Nothing from the candidate
  archive is ever executed.
* Static-only (``--no-llm``); the optional upstream LLM stage is disabled
  by policy (deterministic scanning only).
* Process success alone is NEVER sufficient: the raw JSON report must be
  present, valid, schema-conformant, and free of incomplete/error markers.
* Evidence freshness: every invocation writes to a UNIQUE temporary report
  path and only publishes to the destination after this invocation's
  report validates. A report left by an earlier run is never read as fresh
  evidence, so exit 0/1 without a new report fails closed.
* A valid, complete report with ``findings: []`` is a **clean scan**, not a
  failure. Missing/malformed/incomplete reports are failures.

Phase 2b verification (pinned SkillSpector v2.12.0, tag commit
``c7958a3268d9498644b22edb75d0f051bbc8cbfc``):
* argv confirmed against ``skillspector scan --help``: all four flags
  (``--no-llm``, ``--format json``, ``--output``, ``--fail-on-incomplete``)
  exist with the meanings assumed here.
* The real CLI JSON report does NOT use a flat shape. It nests the verdict
  under ``risk_assessment`` (``score``/``severity``/``recommendation``),
  lists findings under ``issues``, and carries completion evidence in
  ``execution_successful`` + ``analysis_completeness.is_complete``.
  ``parse_scan_report`` accepts this verified v2 shape (normalizing it to
  the canonical flat keys for downstream consumers) as well as the
  project's original flat fixture shape.
* Exit codes (live-observed): ``0`` = complete and score <= 50; ``1`` =
  written report exists but the verdict/``--fail-on-incomplete`` tripped
  (the CLI writes the report BEFORE exiting); ``2`` = execution/usage
  error. The report — not the exit code — is authoritative: exit 0/1 are
  accepted only when the report validates as complete, exit >= 2 always
  fails closed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from factory.errors import IntakeScanError

logger = logging.getLogger(__name__)


class ProcessRunner(Protocol):
    """Injectable subprocess boundary — tests provide fakes."""

    def run(
        self, argv: list[str], *, cwd: Path, timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        ...


class SubprocessRunner:
    """Default runner: fixed argv, shell=False, captured output, timeout."""

    def run(
        self, argv: list[str], *, cwd: Path, timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — fixed argv, shell=False
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
            check=False,
        )


# ---------------------------------------------------------------------------
# Report schema validation (single home for upstream-format knowledge)
# ---------------------------------------------------------------------------

_INCOMPLETE_STATUS = {"error", "failed", "incomplete", "timeout"}


def _report_invalid(reason: str) -> IntakeScanError:
    return IntakeScanError(f"Scan report unusable (fail-closed): {reason}")


def _parse_verified_v2_report(data: dict[str, Any]) -> dict[str, Any]:
    """
    Validate the LIVE-VERIFIED SkillSpector v2.12.0 CLI report shape.

    Required (fail-closed, all of them — missing evidence is rejection):
    * ``risk_assessment`` object with ``score`` (int 0..100),
      ``severity`` and ``recommendation`` (non-empty strings)
    * ``execution_successful`` literally ``true``
    * ``analysis_completeness`` object with ``is_complete`` literally ``true``
    * ``issues`` list of objects (``[]`` = clean)

    On success the nested verdict is normalized onto the canonical flat
    keys (``risk_score``/``severity``/``recommendation``/``findings``) so
    every downstream consumer sees one contract regardless of upstream
    shape; the original nested keys are preserved unchanged.
    """
    assessment = data.get("risk_assessment")
    if not isinstance(assessment, dict):
        raise _report_invalid("'risk_assessment' must be an object.")
    score = assessment.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not (0 <= score <= 100):
        raise _report_invalid("'risk_assessment.score' must be an integer in 0..100.")
    severity = assessment.get("severity")
    if not isinstance(severity, str) or not severity.strip():
        raise _report_invalid("'risk_assessment.severity' must be a non-empty string.")
    recommendation = assessment.get("recommendation")
    if not isinstance(recommendation, str) or not recommendation.strip():
        raise _report_invalid(
            "'risk_assessment.recommendation' must be a non-empty string."
        )
    if data.get("execution_successful") is not True:
        raise _report_invalid("'execution_successful' must be true.")
    completeness = data.get("analysis_completeness")
    if not isinstance(completeness, dict):
        raise _report_invalid("'analysis_completeness' must be an object.")
    if completeness.get("is_complete") is not True:
        raise _report_invalid(
            "report marks the scan as incomplete (analysis_completeness.is_complete)."
        )
    issues = data.get("issues")
    if not isinstance(issues, list):
        raise _report_invalid("'issues' must be a list (use [] for none).")
    for f in issues:
        if not isinstance(f, dict):
            raise _report_invalid("every issue must be an object.")
    # Canonical aliases — the verified nested values are the source of truth.
    data["risk_score"] = score
    data["severity"] = severity
    data["recommendation"] = recommendation
    data["findings"] = issues
    return data


def parse_scan_report(raw: bytes) -> dict[str, Any]:
    """
    Validate raw scanner output and return the parsed report dict.

    Two accepted shapes, both strictly validated:
    * the live-verified SkillSpector v2.12.0 CLI report (nested
      ``risk_assessment``/``issues``/``analysis_completeness`` — see
      ``_parse_verified_v2_report``), normalized to canonical flat keys;
    * the project's original flat contract (``findings`` list,
      ``risk_score`` int 0-100, ``severity`` str, ``recommendation`` str).

    Raises IntakeScanError for: empty output, invalid JSON, non-object JSON,
    explicit incomplete/error markers, or missing/mistyped required fields
    in either shape.
    """
    if not raw or not raw.strip():
        raise _report_invalid("report output is empty.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _report_invalid(f"report is not valid JSON ({exc}).") from exc
    if not isinstance(data, dict):
        raise _report_invalid("report root must be a JSON object.")

    # Explicit upstream incomplete/error markers — always fail closed.
    if data.get("error"):
        raise _report_invalid(f"report records an error: {data.get('error')!r}.")
    status = str(data.get("status", "")).lower()
    if status in _INCOMPLETE_STATUS:
        raise _report_invalid(f"report status is {status!r}.")
    if data.get("scan_complete") is False or data.get("completed") is False:
        raise _report_invalid("report marks the scan as incomplete.")

    # Live-verified v2 CLI shape (Phase 2b) — detected by its nesting.
    if "risk_assessment" in data:
        return _parse_verified_v2_report(data)

    findings = data.get("findings")
    if not isinstance(findings, list):
        raise _report_invalid("'findings' must be a list (use [] for none).")
    score = data.get("risk_score")
    if not isinstance(score, int) or isinstance(score, bool) or not (0 <= score <= 100):
        raise _report_invalid("'risk_score' must be an integer in 0..100.")
    severity = data.get("severity")
    if not isinstance(severity, str) or not severity.strip():
        raise _report_invalid("'severity' must be a non-empty string.")
    recommendation = data.get("recommendation")
    if not isinstance(recommendation, str) or not recommendation.strip():
        raise _report_invalid("'recommendation' must be a non-empty string.")
    for f in findings:
        if not isinstance(f, dict):
            raise _report_invalid("every finding must be an object.")
    return data


def count_unresolved(findings: list[dict[str, Any]]) -> int:
    """Count findings with HIGH or CRITICAL severity (case-insensitive)."""
    count = 0
    for f in findings:
        sev = str(f.get("severity", "")).upper()
        if sev in ("HIGH", "CRITICAL"):
            count += 1
    return count


_VERSION_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}")


def _extract_scanner_version(data: dict[str, Any]) -> str | None:
    """
    Concrete scanner version reported by THIS scan's report (v2 CLI shape).

    The pinned SkillSpector CLI embeds its own version in the JSON report
    under ``metadata.skillspector_version`` — an already-established local
    interface that needs no extra process invocation, no install/update, and
    no network access. Values that are missing or do not look like a version
    token are ignored (None) rather than guessed at.
    """
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return None
    value = meta.get("skillspector_version")
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or _VERSION_VALUE_RE.fullmatch(value) is None:
        return None
    return value


# ---------------------------------------------------------------------------
# Scanner adapter
# ---------------------------------------------------------------------------

@dataclass
class ScanOutcome:
    """Deterministic result of one scan attempt."""
    completed: bool                      # True only for a valid, complete report
    reason: str = ""                     # failure reason when not completed
    report_raw: bytes = b""              # exact bytes (even if later judged invalid)
    report: dict[str, Any] | None = None
    risk_score: int | None = None
    severity: str | None = None
    recommendation: str | None = None
    unresolved_findings: int = 0
    findings_summary: list[str] = field(default_factory=list)
    scanner_version: str | None = None   # from THIS report's metadata (v2 shape)

    @property
    def has_findings(self) -> bool:
        return bool(self.report and (self.report.get("findings") or []))


def build_scan_argv(exe: str, target_dir: Path, report_path: Path) -> list[str]:
    """
    Fixed SkillSpector argv (static-only, incomplete-scan failure).

    Single location for the upstream CLI contract. LIVE-VERIFIED in
    Phase 2b against SkillSpector v2.12.0 ``scan --help``: every flag
    below exists with the assumed meaning (``--fail-on-incomplete`` exits
    1 when analysis is partial; the report is written first).
    """
    return [
        exe,
        "scan",
        str(target_dir),
        "--no-llm",
        "--format", "json",
        "--output", str(report_path),
        "--fail-on-incomplete",
    ]


@dataclass
class SkillSpectorScanner:
    """
    Runs SkillSpector over a quarantined directory via an injected runner.

    ``availability_check`` is injectable so tests can simulate an installed
    (or missing) scanner without touching the real PATH or executing
    anything. When None, availability is probed via the filesystem/PATH.
    """

    executable: str = "skillspector"
    runner: ProcessRunner = field(default_factory=SubprocessRunner)
    availability_check: "Callable[[], bool] | None" = None

    def available(self) -> bool:
        """True when the configured executable can be found on PATH
        (or is an explicit existing path) — or when an injectable
        availability_check says so (tests)."""
        if self.availability_check is not None:
            return self.availability_check()
        exe = self.executable
        if Path(exe).is_file():
            return True
        return shutil.which(exe) is not None

    def run_scan(self, target_dir: Path, report_path: Path, *, timeout_s: float) -> ScanOutcome:
        """
        Execute the scan and validate its report. Every failure mode fails
        closed with completed=False (never raises for expected failures).
        """
        if not self.available():
            return ScanOutcome(
                completed=False,
                reason=(
                    f"scanner executable not available: {self.executable!r} "
                    "(install/configure SkillSpector, or set "
                    "intake.skillspector_executable)"
                ),
            )

        # Evidence freshness: run the scanner against a UNIQUE per-run
        # temporary report path. A process that exits 0/1 without writing
        # cannot inherit — and this code never reads — a report left behind
        # by an earlier invocation. Only a report this invocation produced,
        # and that validated as complete, is atomically published to
        # report_path (replacing any older file at that moment only).
        tmp_report = report_path.parent / (
            f".{report_path.name}.partial-{uuid4().hex}"
        )

        def _fail(reason: str, *, report_raw: bytes = b"") -> ScanOutcome:
            try:
                tmp_report.unlink(missing_ok=True)
            except OSError:  # pragma: no cover — best effort cleanup
                pass
            return ScanOutcome(completed=False, reason=reason, report_raw=report_raw)

        argv = build_scan_argv(self.executable, target_dir, tmp_report)
        try:
            proc = self.runner.run(argv, cwd=target_dir, timeout_s=timeout_s)
        except subprocess.TimeoutExpired:
            return _fail(f"scanner timed out after {timeout_s}s.")
        except OSError as exc:
            return _fail(f"scanner could not be executed: {exc}")

        if proc.returncode not in (0, 1):
            # Exit >= 2 is an execution/usage error in SkillSpector v2.12.0;
            # nothing it produced can be trusted. Fail closed before reading.
            return _fail(
                f"scanner exited with code {proc.returncode} "
                "(fatal scanner error; fail-closed)."
            )

        # Exit 0 (complete, score <= 50) and exit 1 (verdict score > 50 or
        # --fail-on-incomplete tripped) both write the report BEFORE exiting,
        # so the report — not the exit code — is authoritative. The report
        # must exist at the FRESH temp path of THIS run; a valid report at
        # report_path from an earlier run is never consulted. An exit-1
        # report still has to prove completeness below, otherwise fail closed.

        if not tmp_report.is_file():
            return _fail("scanner produced no report file.")
        try:
            raw = tmp_report.read_bytes()
        except OSError as exc:
            return _fail(f"cannot read report: {exc}")

        try:
            data = parse_scan_report(raw)
        except IntakeScanError as exc:
            return _fail(str(exc), report_raw=raw)

        # Publish atomically: validated fresh bytes become the stored
        # evidence; any pre-existing file at report_path is replaced only
        # now that this invocation has proven a complete fresh report.
        try:
            os.replace(tmp_report, report_path)
        except OSError as exc:
            return _fail(f"cannot publish scan report: {exc}", report_raw=raw)

        if proc.returncode == 1:
            logger.info(
                "skillspector exited 1 (verdict score > 50 or incomplete "
                "flag); report validated as complete — report is "
                "authoritative, verdict handled by policy."
            )

        findings = data.get("findings") or []
        return ScanOutcome(
            completed=True,
            report_raw=raw,
            report=data,
            risk_score=data.get("risk_score"),
            severity=data.get("severity"),
            recommendation=data.get("recommendation"),
            unresolved_findings=count_unresolved(findings),
            findings_summary=[
                f"{f.get('id', '?')}: {f.get('title', f.get('pattern', 'finding'))}"
                for f in findings[:20]
            ],
            scanner_version=_extract_scanner_version(data),
        )
