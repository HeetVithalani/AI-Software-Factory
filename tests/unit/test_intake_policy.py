"""
Unit tests for factory.intake.policy — deterministic, explainable license,
capability, and scan-policy evaluation with stable reason codes.

Pure functions: no I/O, no LLM, fully offline.
"""

from __future__ import annotations

import pytest

from factory.intake.policy import (
    PolicyResult,
    classify_license,
    evaluate_capabilities,
    evaluate_policy,
    evaluate_scan,
)


def _codes(result: PolicyResult) -> set[str]:
    return {r.code for r in result.reasons}


class TestLicenseClassification:
    @pytest.mark.parametrize("lic", [
        "MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "ISC",
        "CC0-1.0", "Unlicense", "mit", "  Apache-2.0  ",
    ])
    def test_permissive_eligible(self, lic: str) -> None:
        category, reason = classify_license(lic)
        assert category == "permissive"
        assert reason.code == "LICENSE_PERMISSIVE"

    def test_missing_license_blocked(self) -> None:
        category, reason = classify_license(None)
        assert category == "unknown"
        assert reason.code == "LICENSE_UNKNOWN"

    def test_empty_license_blocked(self) -> None:
        category, reason = classify_license("   ")
        assert category == "unknown"
        assert reason.code == "LICENSE_UNKNOWN"

    @pytest.mark.parametrize("lic", [
        "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "MPL-2.0", "FSL-1.1-ALv2",
        "BUSL-1.1", "SSPL-1.0", "Proprietary", "CC-BY-NC-4.0",
        "Source-Available",
    ])
    def test_restrictive_requires_manual_review(self, lic: str) -> None:
        category, reason = classify_license(lic)
        assert category == "restrictive"
        assert reason.code == "LICENSE_RESTRICTIVE_MANUAL_REVIEW"

    def test_unrecognized_license_blocked_not_mislabelled(self) -> None:
        category, reason = classify_license("WeirdCorp Terms 9000")
        assert category == "unknown"
        assert reason.code == "LICENSE_UNKNOWN"
        assert category != "permissive"

    def test_raw_text_preserved_in_detail(self) -> None:
        _category, reason = classify_license("MyCustom 1.0")
        assert "MyCustom 1.0" in reason.detail

    @pytest.mark.parametrize("expr", [
        "MIT OR GPL-3.0-only",
        "MIT AND AGPL-3.0-only",
        "MIT-proprietary",
    ])
    def test_spdx_expressions_never_inherit_permissive(self, expr: str) -> None:
        # Regression: prefix matching must NOT label expressions/compounds
        # permissive just because they START with a permissive identifier.
        category, reason = classify_license(expr)
        assert category == "restrictive"
        assert category != "permissive"
        assert reason.code == "LICENSE_RESTRICTIVE_MANUAL_REVIEW"
        assert reason.code != "LICENSE_PERMISSIVE"

    def test_permissive_expression_fails_closed_to_unknown(self) -> None:
        # Even an expression of two permissive identifiers is not parsed
        # and must fail closed (no expression parser in dependencies).
        category, reason = classify_license("MIT OR Apache-2.0")
        assert category == "unknown"
        assert reason.code == "LICENSE_UNKNOWN"
        assert category != "permissive"


class TestCapabilities:
    def test_no_capabilities_is_ok(self) -> None:
        assert evaluate_capabilities([]) == []

    @pytest.mark.parametrize("cap,code", [
        ("shell", "CAPABILITY_BLOCKED_SHELL"),
        ("network", "CAPABILITY_BLOCKED_NETWORK"),
        ("secrets", "CAPABILITY_BLOCKED_SECRETS"),
        ("filesystem_write", "CAPABILITY_BLOCKED_FS_MUTATION"),
        ("privileged", "CAPABILITY_BLOCKED_PRIVILEGED"),
        ("SHELL", "CAPABILITY_BLOCKED_SHELL"),
    ])
    def test_blocked_capabilities(self, cap: str, code: str) -> None:
        reasons = evaluate_capabilities([cap])
        assert len(reasons) == 1
        assert reasons[0].code == code

    def test_unknown_capability_fails_closed(self) -> None:
        reasons = evaluate_capabilities(["do_anything"])
        assert reasons[0].code == "CAPABILITY_UNRECOGNIZED"

    def test_requested_capabilities_never_granted_implicitly(self) -> None:
        reasons = evaluate_capabilities(["network", "shell"])
        assert all(r.code.startswith("CAPABILITY_BLOCKED") for r in reasons)


class TestScanPolicy:
    def test_clean_scan_no_reasons(self) -> None:
        assert evaluate_scan("clean", 5, "SAFE") == []

    @pytest.mark.parametrize("state", ["not_run", "failed", "unavailable"])
    def test_non_clean_blocks(self, state: str) -> None:
        reasons = evaluate_scan(state, None, None)
        assert len(reasons) == 1
        assert reasons[0].code == "SCAN_NOT_CLEAN"

    def test_findings_state_blocks_with_findings_reason(self) -> None:
        reasons = evaluate_scan("findings", None, None)
        assert len(reasons) == 1
        assert reasons[0].code == "SCAN_FINDINGS_PRESENT"

    def test_findings_state_also_records_upstream_and_local(self) -> None:
        reasons = evaluate_scan("findings", 78, "DO NOT INSTALL")
        codes = {r.code for r in reasons}
        assert codes == {
            "SCAN_FINDINGS_PRESENT",
            "UPSTREAM_RECOMMENDATION_BLOCKED",
            "LOCAL_POLICY_STRICTER_SCORE",
        }

    def test_upstream_do_not_install_blocks(self) -> None:
        reasons = evaluate_scan("clean", 78, "DO NOT INSTALL")
        assert "UPSTREAM_RECOMMENDATION_BLOCKED" in {r.code for r in reasons}

    def test_underscored_do_not_install_from_real_json_blocks(self) -> None:
        # Phase 2b live verification: the SkillSpector v2.12.0 JSON report
        # emits "DO_NOT_INSTALL" (underscores), never the spaced terminal
        # form — it must hit the same blocked-reason branch.
        reasons = evaluate_scan("clean", 83, "DO_NOT_INSTALL")
        assert "UPSTREAM_RECOMMENDATION_BLOCKED" in {r.code for r in reasons}

    def test_lowercase_underscored_do_not_install_blocks(self) -> None:
        reasons = evaluate_scan("findings", 60, "do_not_install")
        codes = {r.code for r in reasons}
        assert "UPSTREAM_RECOMMENDATION_BLOCKED" in codes
        assert "SCAN_FINDINGS_PRESENT" in codes

    def test_upstream_caution_not_mislabelled_as_blocked(self) -> None:
        reasons = evaluate_scan("clean", 35, "CAUTION")
        codes = {r.code for r in reasons}
        assert "UPSTREAM_CAUTION_RECORDED" in codes
        assert "UPSTREAM_RECOMMENDATION_BLOCKED" not in codes

    def test_local_policy_stricter_than_upstream(self) -> None:
        reasons = evaluate_scan("clean", 35, "CAUTION")
        assert "LOCAL_POLICY_STRICTER_SCORE" in {r.code for r in reasons}

    def test_score_20_boundary_passes(self) -> None:
        assert evaluate_scan("clean", 20, "SAFE") == []

    def test_score_21_blocked_by_local_policy(self) -> None:
        reasons = evaluate_scan("clean", 21, "SAFE")
        assert "LOCAL_POLICY_STRICTER_SCORE" in {r.code for r in reasons}


class TestEvaluatePolicy:
    def test_clean_permissive_no_caps_passes(self) -> None:
        result = evaluate_policy(
            scan_state="clean", risk_score=0, recommendation="SAFE",
            license_declared="MIT", requested_capabilities=[],
        )
        assert result.passed
        assert result.decision == "pass"

    def test_declared_provenance_recorded_but_does_not_block_check(self) -> None:
        # Finding 2: operator-declared provenance is VISIBLE in policy
        # reasons (never labelled verified) yet does not block check() —
        # approval enforces the --manual-review --note attestation.
        result = evaluate_policy(
            scan_state="clean", risk_score=0, recommendation="SAFE",
            license_declared="MIT", license_source="declared",
            requested_capabilities=[],
        )
        assert result.passed
        assert result.decision == "pass"
        reason = next(
            r for r in result.reasons if r.code == "LICENSE_DECLARED_UNVERIFIED"
        )
        assert "declared" in reason.detail
        assert "NOT been independently verified" in reason.detail
        assert "--manual-review --note" in reason.detail
        assert "LICENSE_PERMISSIVE" in _codes(result)

    def test_declared_reason_present_even_with_default_unknown_source(self) -> None:
        # Fail-closed: any non-'verified' provenance records the reason.
        result = evaluate_policy(
            scan_state="clean", risk_score=0, recommendation="SAFE",
            license_declared="MIT", requested_capabilities=[],
        )
        assert "LICENSE_DECLARED_UNVERIFIED" in _codes(result)
        assert result.passed  # non-blocking at check(); enforced at approve

    def test_verified_provenance_records_no_unverified_reason(self) -> None:
        result = evaluate_policy(
            scan_state="clean", risk_score=0, recommendation="SAFE",
            license_declared="MIT", license_source="verified",
            requested_capabilities=[],
        )
        assert result.passed
        assert "LICENSE_DECLARED_UNVERIFIED" not in _codes(result)

    def test_missing_license_still_blocks_with_provenance_reason_absent(self) -> None:
        result = evaluate_policy(
            scan_state="clean", risk_score=0, recommendation="SAFE",
            license_declared=None, license_source="unknown",
            requested_capabilities=[],
        )
        assert not result.passed
        codes = _codes(result)
        assert "LICENSE_UNKNOWN" in codes
        assert "LICENSE_DECLARED_UNVERIFIED" not in codes

    def test_unknown_license_blocks(self) -> None:
        result = evaluate_policy(
            scan_state="clean", risk_score=0, recommendation="SAFE",
            license_declared=None, requested_capabilities=[],
        )
        assert not result.passed
        assert "LICENSE_UNKNOWN" in _codes(result)

    def test_gpl_license_blocks(self) -> None:
        result = evaluate_policy(
            scan_state="clean", risk_score=0, recommendation="SAFE",
            license_declared="GPL-3.0", requested_capabilities=[],
        )
        assert not result.passed
        assert "LICENSE_RESTRICTIVE_MANUAL_REVIEW" in _codes(result)

    def test_capability_blocks(self) -> None:
        result = evaluate_policy(
            scan_state="clean", risk_score=0, recommendation="SAFE",
            license_declared="MIT", requested_capabilities=["shell"],
        )
        assert not result.passed
        assert "CAPABILITY_BLOCKED_SHELL" in _codes(result)

    def test_failed_scan_blocks_regardless_of_license(self) -> None:
        result = evaluate_policy(
            scan_state="failed", risk_score=None, recommendation=None,
            license_declared="MIT", requested_capabilities=[],
        )
        assert not result.passed
        assert "SCAN_NOT_CLEAN" in _codes(result)

    def test_deterministic_and_serializable(self) -> None:
        kwargs = dict(
            scan_state="clean", risk_score=35, recommendation="CAUTION",
            license_declared="Apache-2.0", requested_capabilities=[],
        )
        r1 = evaluate_policy(**kwargs)
        r2 = evaluate_policy(**kwargs)
        assert r1.as_dicts() == r2.as_dicts()
        assert all({"code", "detail"} == set(d.keys()) for d in r1.as_dicts())
