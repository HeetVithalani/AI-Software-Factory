"""
tests/unit/test_projects.py
============================
Deterministic unit tests for factory.projects.*

Covers:
- ProjectIntake: validation, persistence, load
- BaselineCapture: hashing, diff
- RequirementsParser: YAML and text parsing
- QualityGate: threshold evaluation
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    """Create an isolated test DB."""
    from factory.database import init_db
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# ProjectIntake
# ---------------------------------------------------------------------------

class TestProjectIntake:
    def test_register_returns_project_record(self, tmp_path):
        from factory.projects.intake import ProjectIntake
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        workspace = tmp_path / "proj"
        record = intake.register("my-project", "Test project", workspace_root=workspace)
        assert record.name == "my-project"
        assert record.description == "Test project"
        assert record.project_id != ""

    def test_workspace_directory_created(self, tmp_path):
        from factory.projects.intake import ProjectIntake
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        workspace = tmp_path / "new-proj"
        assert not workspace.exists()
        intake.register("new-proj", workspace_root=workspace, create_workspace=True)
        assert workspace.exists()
        assert workspace.is_dir()

    def test_workspace_not_created_if_flag_false(self, tmp_path):
        from factory.projects.intake import ProjectIntake
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        workspace = tmp_path / "no-create"
        intake.register("no-create", workspace_root=workspace, create_workspace=False)
        assert not workspace.exists()

    def test_project_persisted_in_db(self, tmp_path):
        from factory.projects.intake import ProjectIntake
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        workspace = tmp_path / "proj"
        record = intake.register("dbproj", workspace_root=workspace)
        loaded = intake.load(record.project_id)
        assert loaded is not None
        assert loaded.name == "dbproj"
        assert loaded.project_id == record.project_id

    def test_load_nonexistent_returns_none(self, tmp_path):
        from factory.projects.intake import ProjectIntake
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        result = intake.load("nonexistent-id")
        assert result is None

    def test_invalid_name_raises(self, tmp_path):
        from factory.projects.intake import ProjectIntake, IntakeError
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        with pytest.raises(IntakeError):
            intake.register("")

    def test_invalid_name_special_chars(self, tmp_path):
        from factory.projects.intake import ProjectIntake, IntakeError
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        with pytest.raises(IntakeError):
            intake.register("bad name with spaces")

    def test_name_with_hyphens_valid(self, tmp_path):
        from factory.projects.intake import ProjectIntake
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        record = intake.register("valid-name-123")
        assert record.name == "valid-name-123"

    def test_name_with_underscores_valid(self, tmp_path):
        from factory.projects.intake import ProjectIntake
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        record = intake.register("valid_name_abc")
        assert record.name == "valid_name_abc"

    def test_quality_profile_stored(self, tmp_path):
        from factory.projects.intake import ProjectIntake
        from factory.models import QualityProfile
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        record = intake.register("proj-hrd", quality_profile=QualityProfile.HARDENED)
        loaded = intake.load(record.project_id)
        assert loaded is not None
        assert loaded.quality_profile == "hardened"  # use_enum_values

    def test_update_status(self, tmp_path):
        from factory.projects.intake import ProjectIntake
        from factory.models import ProjectStatus
        db = _make_db(tmp_path)
        intake = ProjectIntake(db_path=db)
        record = intake.register("proj-upd")
        intake.update_status(record.project_id, ProjectStatus.BUILDING)
        loaded = intake.load(record.project_id)
        assert loaded is not None
        assert loaded.status == "building"


# ---------------------------------------------------------------------------
# BaselineCapture
# ---------------------------------------------------------------------------

class TestBaselineCapture:
    def test_capture_hashes_files(self, tmp_path):
        from factory.projects.baseline import BaselineCapture
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.py").write_text("y = 2")
        bc = BaselineCapture()
        hashes = bc.capture(tmp_path, save_checkpoint=False)
        assert "a.py" in hashes
        assert "b.py" in hashes
        assert len(hashes["a.py"]) == 64  # SHA-256

    def test_capture_empty_dir(self, tmp_path):
        from factory.projects.baseline import BaselineCapture
        bc = BaselineCapture()
        hashes = bc.capture(tmp_path, save_checkpoint=False)
        assert hashes == {}

    def test_capture_skips_pyc(self, tmp_path):
        from factory.projects.baseline import BaselineCapture
        (tmp_path / "main.py").write_text("pass")
        (tmp_path / "main.pyc").write_bytes(b"\x00\x01")
        bc = BaselineCapture()
        hashes = bc.capture(tmp_path, save_checkpoint=False)
        assert "main.py" in hashes
        assert "main.pyc" not in hashes

    def test_hash_is_deterministic(self, tmp_path):
        from factory.projects.baseline import BaselineCapture
        (tmp_path / "file.txt").write_text("stable content")
        bc = BaselineCapture()
        h1 = bc.capture(tmp_path, save_checkpoint=False)
        h2 = bc.capture(tmp_path, save_checkpoint=False)
        assert h1 == h2

    def test_diff_detects_added_file(self, tmp_path):
        from factory.projects.baseline import BaselineCapture
        (tmp_path / "original.py").write_text("x = 1")
        bc = BaselineCapture()
        baseline = bc.capture(tmp_path, save_checkpoint=False)
        (tmp_path / "newfile.py").write_text("y = 2")
        diff = bc.diff(baseline, tmp_path)
        assert "newfile.py" in diff["added"]

    def test_diff_detects_removed_file(self, tmp_path):
        from factory.projects.baseline import BaselineCapture
        (tmp_path / "gone.py").write_text("x = 1")
        bc = BaselineCapture()
        baseline = bc.capture(tmp_path, save_checkpoint=False)
        (tmp_path / "gone.py").unlink()
        diff = bc.diff(baseline, tmp_path)
        assert "gone.py" in diff["removed"]

    def test_diff_detects_modified_file(self, tmp_path):
        from factory.projects.baseline import BaselineCapture
        (tmp_path / "changed.py").write_text("x = 1")
        bc = BaselineCapture()
        baseline = bc.capture(tmp_path, save_checkpoint=False)
        (tmp_path / "changed.py").write_text("x = 999")
        diff = bc.diff(baseline, tmp_path)
        assert "changed.py" in diff["modified"]

    def test_diff_unchanged_file(self, tmp_path):
        from factory.projects.baseline import BaselineCapture
        (tmp_path / "stable.py").write_text("x = 1")
        bc = BaselineCapture()
        baseline = bc.capture(tmp_path, save_checkpoint=False)
        diff = bc.diff(baseline, tmp_path)
        assert "stable.py" in diff["unchanged"]

    def test_diff_summary_counts(self, tmp_path):
        from factory.projects.baseline import BaselineCapture
        (tmp_path / "keep.py").write_text("x = 1")
        (tmp_path / "change.py").write_text("y = 1")
        bc = BaselineCapture()
        baseline = bc.capture(tmp_path, save_checkpoint=False)
        (tmp_path / "change.py").write_text("y = 99")
        (tmp_path / "new.py").write_text("z = 1")
        diff = bc.diff(baseline, tmp_path)
        assert diff["summary"]["unchanged"] == 1
        assert diff["summary"]["modified"] == 1
        assert diff["summary"]["added"] == 1


# ---------------------------------------------------------------------------
# RequirementsParser
# ---------------------------------------------------------------------------

class TestRequirementsParser:
    def test_parse_text_extracts_list_items(self):
        from factory.projects.requirements import RequirementsParser
        parser = RequirementsParser()
        text = "- User can log in\n- User can sign up\n- User can reset password"
        reqs = parser.parse_text(text)
        assert len(reqs.functional) == 3
        assert reqs.source == "text"

    def test_parse_text_extracts_must_statements(self):
        from factory.projects.requirements import RequirementsParser
        parser = RequirementsParser()
        text = "The system must handle 1000 concurrent users."
        reqs = parser.parse_text(text)
        assert len(reqs.functional) >= 1

    def test_parse_text_identifies_non_functional(self):
        from factory.projects.requirements import RequirementsParser
        parser = RequirementsParser()
        text = "The API must provide low latency responses."
        reqs = parser.parse_text(text)
        assert len(reqs.non_functional) >= 1

    def test_parse_text_fallback_for_plain_sentence(self):
        from factory.projects.requirements import RequirementsParser
        parser = RequirementsParser()
        text = "Build a calculator app"
        reqs = parser.parse_text(text)
        # Fallback: whole text treated as one functional req
        assert len(reqs.functional) >= 1

    def test_parse_dict_basic(self):
        from factory.projects.requirements import RequirementsParser
        parser = RequirementsParser()
        data = {
            "functional": ["Login", "Register"],
            "non_functional": ["Fast response"],
            "quality_gates": {"max_failing_tests": 0},
        }
        reqs = parser.parse_dict(data)
        assert len(reqs.functional) == 2
        assert len(reqs.non_functional) == 1
        assert reqs.quality_gates["max_failing_tests"] == 0
        assert reqs.source == "dict"

    def test_parse_workspace_returns_default_when_no_file(self, tmp_path):
        from factory.projects.requirements import RequirementsParser
        parser = RequirementsParser()
        reqs = parser.parse_workspace(tmp_path)
        assert reqs.source == "default"

    def test_parse_workspace_reads_yaml_file(self, tmp_path):
        from factory.projects.requirements import RequirementsParser
        yaml_content = textwrap.dedent("""\
            functional:
              - User can login
              - User can logout
            non_functional:
              - Page loads in under 1 second
            quality_gates:
              max_failing_tests: 0
        """)
        (tmp_path / "requirements.yaml").write_text(yaml_content)
        parser = RequirementsParser()
        reqs = parser.parse_workspace(tmp_path)
        assert reqs.source == "yaml"
        assert len(reqs.functional) == 2
        assert len(reqs.non_functional) == 1
        assert reqs.quality_gates["max_failing_tests"] == 0

    def test_default_quality_gates_present(self):
        from factory.projects.requirements import RequirementsParser
        parser = RequirementsParser()
        reqs = parser.parse_text("A project")
        assert "max_failing_tests" in reqs.quality_gates
        assert "max_security_findings" in reqs.quality_gates


import textwrap


# ---------------------------------------------------------------------------
# QualityGate
# ---------------------------------------------------------------------------

class TestQualityGate:
    def _passing_test_result(self) -> dict:
        return {"passed": 10, "failed": 0, "errors": 0, "all_passed": True}

    def _failing_test_result(self) -> dict:
        return {"passed": 8, "failed": 2, "errors": 0, "all_passed": False}

    def _clean_scan_result(self) -> dict:
        return {"finding_count": 0, "critical_count": 0, "high_count": 0, "is_clean": True}

    def _dirty_scan_result(self) -> dict:
        return {
            "finding_count": 2,
            "critical_count": 1,
            "high_count": 1,
            "is_clean": False,
        }

    def test_all_passing_returns_passed(self):
        from factory.projects.quality import QualityGate
        gate = QualityGate({"max_failing_tests": 0, "max_security_findings": 0})
        result = gate.evaluate(
            test_result=self._passing_test_result(),
            scan_result=self._clean_scan_result(),
        )
        assert result.passed
        assert result.score == 100.0
        assert len(result.failures) == 0

    def test_failing_tests_fails_gate(self):
        from factory.projects.quality import QualityGate
        gate = QualityGate({"max_failing_tests": 0})
        result = gate.evaluate(test_result=self._failing_test_result())
        assert not result.passed
        assert any("Tests" in f for f in result.failures)

    def test_security_findings_fails_gate(self):
        from factory.projects.quality import QualityGate
        gate = QualityGate({"max_security_findings": 0})
        result = gate.evaluate(scan_result=self._dirty_scan_result())
        assert not result.passed
        assert any("Security" in f for f in result.failures)

    def test_threshold_allows_some_failures(self):
        from factory.projects.quality import QualityGate
        gate = QualityGate({"max_failing_tests": 3})
        result = gate.evaluate(test_result=self._failing_test_result())
        # 2 failures <= 3 threshold → passes
        assert result.passed

    def test_no_inputs_vacuously_passes(self):
        from factory.projects.quality import QualityGate
        gate = QualityGate()
        result = gate.evaluate()
        assert result.passed
        assert result.score == 100.0

    def test_checks_recorded_in_result(self):
        from factory.projects.quality import QualityGate
        gate = QualityGate({"max_failing_tests": 0, "max_security_findings": 0})
        result = gate.evaluate(
            test_result=self._passing_test_result(),
            scan_result=self._clean_scan_result(),
        )
        assert "max_failing_tests" in result.checks
        assert "max_security_findings" in result.checks

    def test_score_partial_pass(self):
        from factory.projects.quality import QualityGate
        gate = QualityGate({"max_failing_tests": 0, "max_security_findings": 0})
        result = gate.evaluate(
            test_result=self._passing_test_result(),  # passes
            scan_result=self._dirty_scan_result(),     # fails
        )
        assert not result.passed
        assert 0.0 < result.score < 100.0

    def test_evaluate_from_reports_missing_files(self, tmp_path):
        from factory.projects.quality import QualityGate
        gate = QualityGate()
        # No report files → vacuously passes
        result = gate.evaluate_from_reports(tmp_path)
        assert result.passed

    def test_evaluate_from_reports_reads_json_files(self, tmp_path):
        from factory.projects.quality import QualityGate
        test_report = {
            "test_result": {"passed": 5, "failed": 0, "errors": 0, "all_passed": True}
        }
        security_report = {
            "scan_result": {
                "finding_count": 0, "critical_count": 0, "high_count": 0
            }
        }
        (tmp_path / "test_report.json").write_text(json.dumps(test_report))
        (tmp_path / "security_report.json").write_text(json.dumps(security_report))

        gate = QualityGate({"max_failing_tests": 0, "max_security_findings": 0})
        result = gate.evaluate_from_reports(tmp_path)
        assert result.passed

    def test_to_dict_contains_all_fields(self):
        from factory.projects.quality import QualityGate
        gate = QualityGate()
        result = gate.evaluate(test_result=self._passing_test_result())
        d = result.to_dict()
        assert "passed" in d
        assert "score" in d
        assert "failures" in d
        assert "checks" in d
