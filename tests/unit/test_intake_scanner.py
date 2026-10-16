"""
Unit tests for factory.intake.scanner — fixed-argv construction, report
schema validation, and fail-closed handling of every scanner failure mode.

A fake ProcessRunner writes checked-in fixture reports; these tests never
locate, install, or execute the real SkillSpector CLI. The schema and
exit-code contract was live-verified once in Phase 2b against the pinned
v2.12.0 release; that capture is preserved as the ``report_realv212_*``
fixtures, so the real CLI is evidence — not a test dependency.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from factory.errors import IntakeScanError
from factory.intake.scanner import (
    SkillSpectorScanner,
    build_scan_argv,
    count_unresolved,
    parse_scan_report,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "intake"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Fake runner (injectable boundary — no real process)
# ---------------------------------------------------------------------------

class FakeRunner:
    """Writes a configured report to the --output path; returns rc."""

    def __init__(
        self,
        report: bytes | None = None,
        returncode: int = 0,
        timeout: bool = False,
        write_report: bool = True,
    ) -> None:
        self.report = report
        self.returncode = returncode
        self.timeout = timeout
        self.write_report = write_report
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, cwd: Path, timeout_s: float):
        self.calls.append(list(argv))
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_s)
        if self.write_report and self.report is not None:
            out = Path(argv[argv.index("--output") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(self.report)
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Argv construction
# ---------------------------------------------------------------------------

class TestArgv:
    def test_fixed_argv_static_no_llm(self, tmp_path: Path) -> None:
        argv = build_scan_argv("skillspector", tmp_path / "t", tmp_path / "r.json")
        assert argv[0] == "skillspector"
        assert argv[1] == "scan"
        assert "--no-llm" in argv
        assert "--fail-on-incomplete" in argv
        assert argv[argv.index("--format") + 1] == "json"
        assert argv[argv.index("--output") + 1] == str(tmp_path / "r.json")

    def test_argv_is_list_of_plain_strings(self, tmp_path: None = None) -> None:
        argv = build_scan_argv("exe", Path("/a/b"), Path("/a/r.json"))
        assert all(isinstance(x, str) for x in argv)
        # No shell metacharacter concatenation: every token is standalone.
        assert not any(" && " in x or ";" in x for x in argv)


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------

class TestParseScanReport:
    def test_valid_complete_report_parses(self) -> None:
        data = parse_scan_report(_fixture("report_clean.json"))
        assert data["risk_score"] == 0
        assert data["findings"] == []

    def test_empty_report_fails(self) -> None:
        with pytest.raises(IntakeScanError, match="empty"):
            parse_scan_report(b"")

    def test_malformed_json_fails(self) -> None:
        with pytest.raises(IntakeScanError, match="not valid JSON"):
            parse_scan_report(_fixture("report_malformed.json"))

    def test_non_object_json_fails(self) -> None:
        with pytest.raises(IntakeScanError, match="JSON object"):
            parse_scan_report(b'[1, 2, 3]')

    def test_missing_required_field_fails(self) -> None:
        with pytest.raises(IntakeScanError, match="risk_score"):
            parse_scan_report(_fixture("report_missing_score.json"))

    def test_explicit_incomplete_status_fails(self) -> None:
        with pytest.raises(IntakeScanError, match="incomplete"):
            parse_scan_report(_fixture("report_incomplete.json"))

    def test_error_marker_fails(self) -> None:
        with pytest.raises(IntakeScanError, match="error"):
            parse_scan_report(b'{"error": "boom", "findings": []}')

    def test_scan_complete_false_fails(self) -> None:
        raw = (b'{"scan_complete": false, "findings": [], "risk_score": 0, '
               b'"severity": "LOW", "recommendation": "SAFE"}')
        with pytest.raises(IntakeScanError, match="incomplete"):
            parse_scan_report(raw)

    def test_findings_must_be_list(self) -> None:
        raw = (b'{"findings": null, "risk_score": 0, "severity": "LOW", '
               b'"recommendation": "SAFE"}')
        with pytest.raises(IntakeScanError, match="findings"):
            parse_scan_report(raw)

    def test_non_dict_finding_fails(self) -> None:
        raw = (b'{"findings": ["str"], "risk_score": 0, "severity": "LOW", '
               b'"recommendation": "SAFE"}')
        with pytest.raises(IntakeScanError, match="finding"):
            parse_scan_report(raw)

    def test_count_unresolved_high_critical(self) -> None:
        findings = [{"severity": "HIGH"}, {"severity": "critical"},
                    {"severity": "MEDIUM"}, {"severity": "LOW"}]
        assert count_unresolved(findings) == 2


# ---------------------------------------------------------------------------
# Live-verified SkillSpector v2.12.0 report shape (Phase 2b captures)
# ---------------------------------------------------------------------------

class TestVerifiedV212Schema:
    def test_real_clean_report_parses_with_canonical_aliases(self) -> None:
        data = parse_scan_report(_fixture("report_realv212_clean.json"))
        assert data["risk_score"] == 0
        assert data["findings"] == []
        assert data["severity"] == "LOW"
        assert data["recommendation"] == "SAFE"
        # Original verified nesting is preserved alongside the aliases.
        assert data["risk_assessment"]["score"] == 0

    def test_real_findings_report_parses_and_normalizes(self) -> None:
        data = parse_scan_report(_fixture("report_realv212_findings.json"))
        assert data["risk_score"] == 83
        assert data["severity"] == "CRITICAL"
        assert data["recommendation"] == "DO_NOT_INSTALL"
        assert len(data["findings"]) == 5
        assert data["findings"] is data["issues"]
        # Real issues carry id/pattern (there is no 'title' upstream).
        first = data["findings"][0]
        assert first["id"] == "P1"
        assert first["severity"] == "HIGH"
        assert isinstance(first["pattern"], str)

    def test_real_report_missing_execution_success_fails_closed(self) -> None:
        raw = (b'{"risk_assessment": {"score": 0, "severity": "LOW", '
               b'"recommendation": "SAFE"}, "execution_successful": false, '
               b'"analysis_completeness": {"is_complete": true}, "issues": []}')
        with pytest.raises(IntakeScanError, match="execution_successful"):
            parse_scan_report(raw)

    def test_real_report_without_execution_flag_fails_closed(self) -> None:
        # Fail-closed: the completion evidence must be present, not assumed.
        raw = (b'{"risk_assessment": {"score": 0, "severity": "LOW", '
               b'"recommendation": "SAFE"}, '
               b'"analysis_completeness": {"is_complete": true}, "issues": []}')
        with pytest.raises(IntakeScanError, match="execution_successful"):
            parse_scan_report(raw)

    def test_real_incomplete_report_fails_closed(self) -> None:
        with pytest.raises(IntakeScanError, match="incomplete"):
            parse_scan_report(_fixture("report_realv212_incomplete.json"))

    def test_real_report_requires_completeness_object(self) -> None:
        raw = (b'{"risk_assessment": {"score": 0, "severity": "LOW", '
               b'"recommendation": "SAFE"}, "execution_successful": true, '
               b'"issues": []}')
        with pytest.raises(IntakeScanError, match="analysis_completeness"):
            parse_scan_report(raw)

    def test_real_report_missing_score_fails_closed(self) -> None:
        raw = (b'{"risk_assessment": {"severity": "LOW", '
               b'"recommendation": "SAFE"}, "execution_successful": true, '
               b'"analysis_completeness": {"is_complete": true}, "issues": []}')
        with pytest.raises(IntakeScanError, match="score"):
            parse_scan_report(raw)

    def test_real_report_score_must_be_int_in_range(self) -> None:
        for bad in (b'"0"', b"-5", b"101", b"true"):
            raw = (b'{"risk_assessment": {"score": ' + bad +
                   b', "severity": "LOW", "recommendation": "SAFE"}, '
                   b'"execution_successful": true, '
                   b'"analysis_completeness": {"is_complete": true}, '
                   b'"issues": []}')
            with pytest.raises(IntakeScanError, match="score"):
                parse_scan_report(raw)

    def test_real_report_issues_must_be_list(self) -> None:
        raw = (b'{"risk_assessment": {"score": 0, "severity": "LOW", '
               b'"recommendation": "SAFE"}, "execution_successful": true, '
               b'"analysis_completeness": {"is_complete": true}, '
               b'"issues": null}')
        with pytest.raises(IntakeScanError, match="issues"):
            parse_scan_report(raw)

    def test_real_report_missing_recommendation_fails_closed(self) -> None:
        raw = (b'{"risk_assessment": {"score": 0, "severity": "LOW"}, '
               b'"execution_successful": true, '
               b'"analysis_completeness": {"is_complete": true}, "issues": []}')
        with pytest.raises(IntakeScanError, match="recommendation"):
            parse_scan_report(raw)


# ---------------------------------------------------------------------------
# Scanner adapter — every failure mode fails closed
# ---------------------------------------------------------------------------

class TestScannerAdapter:
    def _scanner(self, runner: FakeRunner, exe: str = "skillspector",
                 available: bool = True) -> SkillSpectorScanner:
        return SkillSpectorScanner(
            executable=exe,
            runner=runner,
            availability_check=lambda: available,
        )

    def test_clean_scan_zero_findings_is_completed_not_failure(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner(report=_fixture("report_clean.json"))
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is True
        assert outcome.has_findings is False
        assert outcome.risk_score == 0
        assert outcome.recommendation == "SAFE"

    def test_nonempty_findings_is_completed_scan_with_findings(
        self, tmp_path: Path
    ) -> None:
        runner = FakeRunner(report=_fixture("report_findings.json"))
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is True
        assert outcome.has_findings is True
        assert outcome.unresolved_findings == 1  # one HIGH
        assert outcome.risk_score == 78

    def test_missing_scanner_executable_fails_closed(self, tmp_path: Path) -> None:
        scanner = self._scanner(
            FakeRunner(report=b"never called"),
            exe="definitely-not-installed-xyz-9000",
            available=False,
        )
        outcome = scanner.run_scan(tmp_path, tmp_path / "r.json", timeout_s=10)
        assert outcome.completed is False
        assert "not available" in outcome.reason

    def test_timeout_fails_closed(self, tmp_path: Path) -> None:
        runner = FakeRunner(timeout=True)
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=1
        )
        assert outcome.completed is False
        assert "timed out" in outcome.reason

    def test_nonzero_exit_fails_closed(self, tmp_path: Path) -> None:
        runner = FakeRunner(report=_fixture("report_clean.json"), returncode=3)
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is False
        assert "code 3" in outcome.reason

    def test_verdict_exit_one_with_complete_report_is_completed(
        self, tmp_path: Path
    ) -> None:
        # Phase 2b live finding: SkillSpector exits 1 whenever score > 50,
        # but writes the complete report first — the report is authoritative,
        # so a findings scan must reach the pipeline as COMPLETED (and be
        # judged by policy), never be mislabelled a scanner failure.
        runner = FakeRunner(
            report=_fixture("report_realv212_findings.json"), returncode=1
        )
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is True
        assert outcome.has_findings is True
        assert outcome.risk_score == 83
        assert outcome.recommendation == "DO_NOT_INSTALL"
        assert outcome.unresolved_findings == 4  # four HIGH issues

    def test_exit_one_without_report_fails_closed(self, tmp_path: Path) -> None:
        runner = FakeRunner(report=None, write_report=False, returncode=1)
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is False
        assert "no report file" in outcome.reason

    def test_exit_one_with_incomplete_report_fails_closed(
        self, tmp_path: Path
    ) -> None:
        # --fail-on-incomplete exits 1 AND the report admits incompleteness:
        # the exit code alone neither pardons nor convicts; the report does.
        runner = FakeRunner(
            report=_fixture("report_realv212_incomplete.json"), returncode=1
        )
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is False
        assert "incomplete" in outcome.reason

    def test_fatal_exit_two_fails_closed_even_with_report(
        self, tmp_path: Path
    ) -> None:
        # Exit 2 = execution/usage error in SkillSpector v2.12.0: fail before
        # reading anything it produced.
        runner = FakeRunner(report=_fixture("report_realv212_clean.json"), returncode=2)
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is False
        assert "code 2" in outcome.reason

    def test_missing_report_file_fails_closed(self, tmp_path: Path) -> None:
        runner = FakeRunner(report=None, write_report=False)
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is False
        assert "no report file" in outcome.reason

    def test_malformed_report_fails_closed(self, tmp_path: Path) -> None:
        runner = FakeRunner(report=_fixture("report_malformed.json"))
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is False
        assert "not valid JSON" in outcome.reason

    def test_invalid_schema_fails_closed(self, tmp_path: Path) -> None:
        runner = FakeRunner(report=_fixture("report_missing_score.json"))
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is False
        assert "risk_score" in outcome.reason

    def test_explicit_incomplete_scan_fails_closed(self, tmp_path: Path) -> None:
        runner = FakeRunner(report=_fixture("report_incomplete.json"))
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is False
        assert "incomplete" in outcome.reason

    def test_exit_code_alone_never_sufficient(self, tmp_path: Path) -> None:
        # rc == 0 but the report is garbage → still fail closed.
        runner = FakeRunner(report=b"garbage", returncode=0)
        outcome = self._scanner(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is False

    def test_process_exception_fails_closed(self, tmp_path: Path) -> None:
        class ExplodingRunner:
            def run(self, argv, *, cwd, timeout_s):
                raise OSError("cannot exec")

        scanner = SkillSpectorScanner(
            executable="skillspector",
            runner=ExplodingRunner(),
            availability_check=lambda: True,
        )
        outcome = scanner.run_scan(tmp_path, tmp_path / "r.json", timeout_s=1)
        assert outcome.completed is False
        assert "could not be executed" in outcome.reason


# ---------------------------------------------------------------------------
# Evidence freshness (M1) — every report must come from THIS invocation only
# ---------------------------------------------------------------------------

def _scanner_for(runner: FakeRunner) -> SkillSpectorScanner:
    return SkillSpectorScanner(
        executable="skillspector",
        runner=runner,
        availability_check=lambda: True,
    )


class TestReportFreshness:
    """A pre-existing report at the destination is never reused as evidence."""

    def _partials(self, tmp_path: Path) -> list[Path]:
        return [p for p in tmp_path.iterdir() if "partial" in p.name]

    @pytest.mark.parametrize("exit_code", [0, 1])
    def test_stale_destination_report_not_consumed_when_nothing_written(
        self, tmp_path: Path, exit_code: int,
    ) -> None:
        # A valid CLEAN report already sits at the expected destination,
        # while this invocation exits 0/1 WITHOUT producing any report.
        dest = tmp_path / "r.json"
        stale = _fixture("report_clean.json")
        dest.write_bytes(stale)

        runner = FakeRunner(
            report=_fixture("report_findings.json"),
            returncode=exit_code,
            write_report=False,
        )
        outcome = _scanner_for(runner).run_scan(tmp_path, dest, timeout_s=10)

        assert outcome.completed is False
        assert "no report file" in outcome.reason
        # The stale bytes were neither read nor replaced, and the runner's
        # invocation output path is a fresh temporary file, not dest.
        assert dest.read_bytes() == stale
        assert outcome.report_raw == b""
        assert runner.calls, "runner should have been invoked"
        assert runner.calls[0][runner.calls[0].index("--output") + 1] != str(dest)
        # No partial temp reports are left behind after the failure.
        assert self._partials(tmp_path) == []

    def test_fresh_report_is_accepted_and_replaces_stale_bytes(
        self, tmp_path: Path,
    ) -> None:
        dest = tmp_path / "r.json"
        stale = _fixture("report_clean.json")
        dest.write_bytes(stale)

        fresh = _fixture("report_findings.json")
        assert fresh != stale
        expected_score = parse_scan_report(fresh)["risk_score"]

        runner = FakeRunner(report=fresh, returncode=1)  # exit 1 is fine with a valid report
        outcome = _scanner_for(runner).run_scan(tmp_path, dest, timeout_s=10)

        assert outcome.completed is True
        assert outcome.report_raw == fresh
        # Stale bytes were replaced ONLY by this invocation's fresh report.
        assert dest.read_bytes() == fresh
        assert outcome.risk_score == expected_score
        assert self._partials(tmp_path) == []

    def test_no_new_report_on_exit_2_leaves_destination_untouched(
        self, tmp_path: Path,
    ) -> None:
        dest = tmp_path / "r.json"
        stale = _fixture("report_clean.json")
        dest.write_bytes(stale)

        runner = FakeRunner(report=_fixture("report_findings.json"), returncode=2)
        outcome = _scanner_for(runner).run_scan(tmp_path, dest, timeout_s=10)

        assert outcome.completed is False
        assert "code 2" in outcome.reason
        assert dest.read_bytes() == stale
        assert self._partials(tmp_path) == []


# ---------------------------------------------------------------------------
# Version binding (L3) — concrete scanner version from this report's metadata
# ---------------------------------------------------------------------------

class TestScannerVersionEvidence:
    def test_version_bound_from_v2_report_metadata(self, tmp_path: Path) -> None:
        runner = FakeRunner(report=_fixture("report_realv212_clean.json"))
        outcome = _scanner_for(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is True
        # Already-established local interface: the pinned CLI embeds its
        # version in the JSON report it just produced — no extra invocation.
        assert outcome.scanner_version == "2.12.0"

    def test_version_omitted_when_report_carries_none(self, tmp_path: Path) -> None:
        runner = FakeRunner(report=_fixture("report_clean.json"))
        outcome = _scanner_for(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is True
        # Never guessed when the report does not provide one.
        assert outcome.scanner_version is None

    def test_version_ignored_unless_it_looks_like_a_token(
        self, tmp_path: Path,
    ) -> None:
        import json
        data = json.loads(_fixture("report_realv212_clean.json"))
        data["metadata"]["skillspector_version"] = "2.12.0; unexpected payload"
        runner = FakeRunner(report=json.dumps(data).encode("utf-8"))
        outcome = _scanner_for(runner).run_scan(
            tmp_path, tmp_path / "r.json", timeout_s=10
        )
        assert outcome.completed is True
        assert outcome.scanner_version is None
