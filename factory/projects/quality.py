"""
factory.projects.quality
=========================
Quality gate evaluation.

A QualityGate evaluates whether a project meets the thresholds defined in
its Requirements. It takes a TestResult and a ScanResult and returns a
structured QualityGateResult.

This is purely deterministic — no LLM involvement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class QualityGateResult:
    """
    Result of a quality gate evaluation.

    Attributes
    ----------
    passed:
        True if all quality thresholds were met.
    score:
        A 0–100 normalised quality score.
    failures:
        List of human-readable failure reasons.
    checks:
        Dict mapping check name → {passed, actual, threshold}.
    """
    passed: bool = False
    score: float = 0.0
    failures: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "score": round(self.score, 1),
            "failures": self.failures,
            "checks": self.checks,
        }


def _validate_test_evidence(test_result: Any) -> str | None:
    """
    Validate test evidence (COR-005, fail closed).

    Returns ``None`` when the evidence is usable for threshold checks,
    otherwise a human-readable failure reason. The accepted shape is the
    dict produced by ``TestResult.to_dict()`` (factory/tools/testing.py);
    the gate consumes ``passed``/``failed``/``errors``.
    """
    if test_result is None:
        return "Missing test evidence: no test_result was provided to the gate."
    if not isinstance(test_result, dict):
        return (
            "Malformed test evidence: expected a dict from TestResult.to_dict(), "
            f"got {type(test_result).__name__}."
        )

    required = ("passed", "failed", "errors")
    missing = [key for key in required if key not in test_result]
    if missing:
        return (
            "Malformed test evidence: missing required field(s): "
            + ", ".join(missing) + "."
        )
    try:
        counts = [int(test_result[key]) for key in required]
    except (TypeError, ValueError):
        return (
            "Malformed test evidence: 'passed'/'failed'/'errors' "
            "must be integers."
        )
    if any(count < 0 for count in counts):
        return "Malformed test evidence: negative test counts are not allowed."
    if sum(counts) == 0:
        # COR-001/COR-005: a suite that ran nothing must never pass just
        # because all_passed=True or the failure count is zero.
        return (
            "Missing test evidence: zero tests collected/executed — "
            "all_passed=True or a zero failure count alone cannot prove "
            "a passing suite."
        )
    return None


def _validate_scan_evidence(scan_result: Any) -> str | None:
    """
    Validate security-scan evidence (COR-006, fail closed).

    Returns ``None`` when the evidence is usable for threshold checks,
    otherwise a human-readable failure reason. The accepted shape is the
    dict produced by ``ScanResult.to_dict()`` (factory/tools/security.py);
    the gate consumes ``critical_count``/``high_count``.
    """
    if scan_result is None:
        return "Missing security-scan evidence: no scan_result was provided to the gate."
    if not isinstance(scan_result, dict):
        return (
            "Malformed security-scan evidence: expected a dict from "
            f"ScanResult.to_dict(), got {type(scan_result).__name__}."
        )

    required = ("critical_count", "high_count")
    missing = [key for key in required if key not in scan_result]
    if missing:
        return (
            "Malformed security-scan evidence: missing required field(s): "
            + ", ".join(missing) + "."
        )
    try:
        counts = [int(scan_result[key]) for key in required]
    except (TypeError, ValueError):
        return (
            "Malformed security-scan evidence: 'critical_count'/'high_count' "
            "must be integers."
        )
    if any(count < 0 for count in counts):
        return "Malformed security-scan evidence: negative finding counts are not allowed."
    return None


class QualityGate:
    """
    Evaluates project quality against defined thresholds.

    Default thresholds (can be overridden by Requirements.quality_gates):
    - max_failing_tests: 0 (all tests must pass)
    - max_security_findings: 0 (zero critical/high security findings)
    - test_coverage_min_pct: 0 (no coverage requirement by default)

    Usage::

        gate = QualityGate(thresholds=requirements.quality_gates)
        result = gate.evaluate(
            test_result=test_result_dict,
            scan_result=scan_result_dict,
        )
        if not result.passed:
            print("Quality gate failed:", result.failures)
    """

    def __init__(
        self,
        thresholds: dict[str, Any] | None = None,
    ) -> None:
        self.thresholds: dict[str, Any] = {
            "max_failing_tests": 0,
            "max_security_findings": 0,
            "test_coverage_min_pct": 0,
        }
        if thresholds:
            self.thresholds.update(thresholds)

    def evaluate(
        self,
        *,
        test_result: dict[str, Any] | None = None,
        scan_result: dict[str, Any] | None = None,
    ) -> QualityGateResult:
        """
        Evaluate quality based on test and scan results.

        Fail-closed contract (COR-005 / COR-006)
        -----------------------------------------
        * Both evidences are REQUIRED and validated before thresholds run.
        * Missing/malformed evidence, or evidence of zero executed tests,
          fails the gate with an explicit reason.
        * The score never defaults to 100 for "no checks to evaluate".

        Parameters
        ----------
        test_result:
            Dict from TestResult.to_dict() (keys: passed, failed, errors, ...).
        scan_result:
            Dict from ScanResult.to_dict() (keys: finding_count, critical_count, ...).

        Returns
        -------
        QualityGateResult
        """
        result = QualityGateResult()
        checks_passed = 0
        total_checks = 0

        # ---- Evidence validation (fail closed) ----
        test_error = _validate_test_evidence(test_result)
        total_checks += 1
        if test_error is None:
            checks_passed += 1
            result.checks["test_evidence"] = {"passed": True}
        else:
            result.failures.append(test_error)
            result.checks["test_evidence"] = {"passed": False, "error": test_error}

        scan_error = _validate_scan_evidence(scan_result)
        total_checks += 1
        if scan_error is None:
            checks_passed += 1
            result.checks["scan_evidence"] = {"passed": True}
        else:
            result.failures.append(scan_error)
            result.checks["scan_evidence"] = {"passed": False, "error": scan_error}

        # ---- Test checks (only with valid evidence) ----
        if test_error is None and isinstance(test_result, dict):
            # Max failing tests
            max_fail = int(self.thresholds.get("max_failing_tests", 0))
            actual_fail = int(test_result.get("failed", 0)) + int(test_result.get("errors", 0))
            total_checks += 1
            test_passed = actual_fail <= max_fail
            if test_passed:
                checks_passed += 1
            else:
                result.failures.append(
                    f"Tests: {actual_fail} failing (threshold: ≤{max_fail})"
                )
            result.checks["max_failing_tests"] = {
                "passed": test_passed,
                "actual": actual_fail,
                "threshold": max_fail,
            }

            # Test coverage (if reported) — malformed values fail closed
            coverage = test_result.get("coverage_pct")
            if coverage is not None:
                min_cov = float(self.thresholds.get("test_coverage_min_pct", 0))
                total_checks += 1
                try:
                    cov_value = float(coverage)
                except (TypeError, ValueError):
                    cov_passed = False
                    result.failures.append(
                        f"Coverage: malformed value {coverage!r} in test evidence"
                    )
                else:
                    cov_passed = cov_value >= min_cov
                    if not cov_passed:
                        result.failures.append(
                            f"Coverage: {cov_value:.1f}% (threshold: ≥{min_cov}%)"
                        )
                if cov_passed:
                    checks_passed += 1
                result.checks["test_coverage_min_pct"] = {
                    "passed": cov_passed,
                    "actual": coverage,
                    "threshold": min_cov,
                }

        # ---- Security checks (only with valid evidence) ----
        if scan_error is None and isinstance(scan_result, dict):
            max_findings = int(self.thresholds.get("max_security_findings", 0))
            # Count high+critical findings
            critical = int(scan_result.get("critical_count", 0))
            high = int(scan_result.get("high_count", 0))
            actual_findings = critical + high

            total_checks += 1
            sec_passed = actual_findings <= max_findings
            if sec_passed:
                checks_passed += 1
            else:
                result.failures.append(
                    f"Security: {actual_findings} high/critical findings "
                    f"(threshold: ≤{max_findings})"
                )
            result.checks["max_security_findings"] = {
                "passed": sec_passed,
                "actual": actual_findings,
                "threshold": max_findings,
                "critical": critical,
                "high": high,
            }

        # ---- Score ----
        if total_checks > 0:
            result.score = (checks_passed / total_checks) * 100
        else:
            # Fail closed: never award 100 for having nothing to evaluate.
            result.score = 0.0

        result.passed = len(result.failures) == 0

        logger.info(
            "QualityGate: %s (score=%.1f, checks=%d/%d).",
            "PASSED" if result.passed else "FAILED",
            result.score,
            checks_passed,
            total_checks,
        )
        return result

    def evaluate_from_reports(
        self,
        workspace_root: Any,  # Path
    ) -> QualityGateResult:
        """
        Convenience: load test_report.json and security_report.json from
        workspace and evaluate.
        """
        import json
        from pathlib import Path

        root = Path(workspace_root).resolve()
        test_result: dict[str, Any] | None = None
        scan_result: dict[str, Any] | None = None

        test_report_path = root / "test_report.json"
        if test_report_path.exists():
            try:
                data = json.loads(test_report_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    test_result = data.get("test_result")
                else:
                    logger.warning(
                        "QualityGate: test_report.json is not a JSON object (%s)",
                        type(data).__name__,
                    )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("QualityGate: could not read test_report.json: %s", exc)

        security_report_path = root / "security_report.json"
        if security_report_path.exists():
            try:
                data = json.loads(security_report_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    scan_result = data.get("scan_result")
                else:
                    logger.warning(
                        "QualityGate: security_report.json is not a JSON object (%s)",
                        type(data).__name__,
                    )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("QualityGate: could not read security_report.json: %s", exc)

        # Absent/malformed/missing-key reports leave the evidence as None,
        # which the fail-closed validation in evaluate() turns into a failure.
        return self.evaluate(test_result=test_result, scan_result=scan_result)
