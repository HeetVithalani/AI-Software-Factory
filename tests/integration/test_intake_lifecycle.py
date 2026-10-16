"""
Integration tests for the full Phase 2a tool-intake lifecycle, using a
temporary database, a fake transport, and a fake scanner runner.

Every boundary is mocked: NO network requests, NO real scanner execution,
NO real third-party content is fetched. The tar.gz artifact is built
locally in pytest temp dirs.

Covers the required safety properties:
  1. happy path discover → fetch → scan → check → approve → install
  2. derived restricted download URL
  3. state-order enforcement
  4. scanner-unavailable fail-closed (never approvable)
  5. tamper between approval and install invalidates approval
  6. tamper before scan invalidates on re-fetch
  7. approval not reusable across revisions
  8. policy-blocked license needs explicit manual review
  9. findings scan blocks auto-approval and is not mislabelled
 10. idempotent re-fetch vs. changed-bytes refusal
 11. revoke retains evidence
 12. audit events for success/failure without leaking report content
 13. installed skills are not auto-enabled or injected
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.database import get_connection, initialize_database
from factory.errors import (
    IntakeFetchError,
    IntakeInstallError,
    IntakeIntegrityError,
    IntakePolicyError,
    IntakeScanError,
    IntakeStateError,
)
from factory.intake.scanner import SkillSpectorScanner
from factory.intake.service import IntakeService
from factory.models import IntakeStatus, ScanState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "intake"

SHA1 = "1111111111111111111111111111111111111111"
SHA2 = "2222222222222222222222222222222222222222"
SOURCE = "https://github.com/demo-org/demo-skill"

SKILL_MD = b"# Demo Skill\nYou are a helpful assistant.\n"
NOTES_MD = b"Reference material.\n"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTransport:
    """Injectable download boundary — returns canned bytes, records URLs."""

    def __init__(self, payload: bytes, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    def fetch(self, url: str, *, timeout_s: float, max_bytes: int) -> bytes:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.payload


class FakeRunner:
    """Writes a canned report to the --output path; returns rc 0.

    Records every argv it was invoked with (``calls``) so tests can prove
    rejection paths never reach the scanner at all.
    """

    def __init__(self, report: bytes) -> None:
        self.report = report
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, cwd: Path, timeout_s: float):
        import subprocess
        self.calls.append(list(argv))
        out = Path(argv[argv.index("--output") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(self.report)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _skill_tar() -> bytes:
    """Build the candidate skill tar.gz locally (no network)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        d = tarfile.TarInfo("demo-skill")
        d.type = tarfile.DIRTYPE
        tar.addfile(d)
        for name, data in [
            ("demo-skill/SKILL.md", SKILL_MD),
            ("demo-skill/reference/notes.md", NOTES_MD),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def env(tmp_path: Path) -> SimpleNamespace:
    db = tmp_path / "factory.db"
    initialize_database(db)
    qroot = tmp_path / "quarantine"
    iroot = tmp_path / "install"
    transport = FakeTransport(_skill_tar())
    scanner = SkillSpectorScanner(
        executable="skillspector",
        runner=FakeRunner((FIXTURES / "report_clean.json").read_bytes()),
        availability_check=lambda: True,  # fake scanner; never on PATH
    )
    svc = IntakeService(
        db_path=db,
        quarantine_root=qroot,
        install_root=iroot,
        transport=transport,
        scanner=scanner,
        ignore_checker=lambda p: True,   # tests bypass the git check
        total_cap_bytes=50 * 1024 * 1024,
        fetch_timeout_s=5,
        scan_timeout_s=5,
    )
    return SimpleNamespace(
        db=db, qroot=qroot, iroot=iroot, transport=transport,
        scanner=scanner, svc=svc, tmp=tmp_path,
    )


def _events(db: Path) -> list[dict]:
    with get_connection(db) as conn:
        rows = conn.execute(
            "SELECT event_type, payload FROM events "
            "WHERE event_type LIKE 'tool.intake.%' ORDER BY rowid"
        ).fetchall()
    return [{"type": r["event_type"], "payload": json.loads(r["payload"])} for r in rows]


def _approve_to(env, sha: str = SHA1, license_: str | None = "MIT") -> None:
    """Run discover→fetch→scan→check→approve up to APPROVED.

    Operator-declared licenses require the explicit provenance attestation
    (--manual-review --note) at approval — every Phase 2a approval does.
    """
    env.svc.discover(SOURCE, sha, license_declared=license_)
    env.svc.fetch(env.svc.discover(SOURCE, sha).tool.tool_id, sha)
    tool_id = env.svc.discover(SOURCE, sha).tool.tool_id
    env.svc.scan(tool_id, sha)
    env.svc.check(tool_id, sha)
    env.svc.approve(
        tool_id, sha, manual_review=True,
        note="Attested operator-declared license provenance",
    )


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_full_lifecycle_discover_to_install(self, env) -> None:
        summary = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = summary.tool.tool_id
        assert summary.tool.status == IntakeStatus.DISCOVERED

        fetched = env.svc.fetch(tool_id, SHA1)
        assert fetched.tool.status == IntakeStatus.QUARANTINED
        assert fetched.revision.artifact_sha256
        assert fetched.revision.tree_sha256

        scanned = env.svc.scan(tool_id, SHA1)
        assert scanned.tool.status == IntakeStatus.SCANNED
        assert scanned.revision.scan_state == ScanState.CLEAN
        assert scanned.revision.scan_report_sha256
        assert scanned.revision.risk_score == 0

        checked, result = env.svc.check(tool_id, SHA1)
        assert result.passed
        assert checked.tool.status == IntakeStatus.PENDING_APPROVAL
        assert checked.revision.approval_request_id

        approved = env.svc.approve(
            tool_id, SHA1, manual_review=True,
            note="Attested operator-declared license provenance",
        )
        assert approved.tool.status == IntakeStatus.APPROVED
        assert approved.revision.approval_artifact_sha256 == (
            approved.revision.artifact_sha256
        )
        assert approved.revision.approval_report_sha256 == (
            approved.revision.scan_report_sha256
        )

        installed = env.svc.install(tool_id, SHA1)
        assert installed.tool.status == IntakeStatus.INSTALLED
        dest = Path(installed.tool.install_path or "")
        assert (dest / "demo-skill" / "SKILL.md").read_bytes() == SKILL_MD
        assert (dest / "demo-skill" / "reference" / "notes.md").read_bytes() == NOTES_MD

    def test_download_url_is_derived_restricted_endpoint(self, env) -> None:
        summary = env.svc.discover(SOURCE, SHA1)
        env.svc.fetch(summary.tool.tool_id, SHA1)
        assert env.transport.calls == [
            f"https://codeload.github.com/demo-org/demo-skill/tar.gz/{SHA1}"
        ]

    def test_scan_report_persisted_as_evidence_with_hash(self, env) -> None:
        summary = env.svc.discover(SOURCE, SHA1)
        env.svc.fetch(summary.tool.tool_id, SHA1)
        env.svc.scan(summary.tool.tool_id, SHA1)
        with get_connection(env.db) as conn:
            row = conn.execute(
                "SELECT path, hash_sha256 FROM evidence "
                "WHERE kind='scan_result' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        report = Path(row["path"])
        assert report.is_file()
        import hashlib
        assert row["hash_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()

    def test_status_labels_distinguish_states(self, env) -> None:
        # discovered
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        assert "discovered" in env.svc.status(tool_id)[0]["label"]

        # quarantined
        env.svc.fetch(tool_id, SHA1)
        assert "quarantined" in env.svc.status(tool_id)[0]["label"]

        # clean scan
        env.svc.scan(tool_id, SHA1)
        assert "clean (0 findings)" in env.svc.status(tool_id)[0]["label"]

        # pending → approved → installed
        env.svc.check(tool_id, SHA1)
        assert "pending" in env.svc.status(tool_id)[0]["label"]
        env.svc.approve(
            tool_id, SHA1, manual_review=True,
            note="Attested operator-declared license provenance",
        )
        assert "approved" in env.svc.status(tool_id)[0]["label"]
        env.svc.install(tool_id, SHA1)
        assert "disabled/unloaded" in env.svc.status(tool_id)[0]["label"]


# ---------------------------------------------------------------------------
# 3. State-order enforcement
# ---------------------------------------------------------------------------

class TestStateOrdering:
    def test_scan_before_fetch_fails(self, env) -> None:
        s = env.svc.discover(SOURCE, SHA1)
        with pytest.raises(IntakeStateError):
            env.svc.scan(s.tool.tool_id, SHA1)

    def test_install_before_approval_fails(self, env) -> None:
        s = env.svc.discover(SOURCE, SHA1)
        env.svc.fetch(s.tool.tool_id, SHA1)
        with pytest.raises(IntakeStateError):
            env.svc.install(s.tool.tool_id, SHA1)

    def test_approve_before_check_fails(self, env) -> None:
        s = env.svc.discover(SOURCE, SHA1)
        env.svc.fetch(s.tool.tool_id, SHA1)
        env.svc.scan(s.tool.tool_id, SHA1)
        with pytest.raises(IntakeStateError):
            env.svc.approve(s.tool.tool_id, SHA1)

    def test_unknown_tool_and_revision_fail(self, env) -> None:
        with pytest.raises(IntakeStateError):
            env.svc.fetch("nonexistent", SHA1)
        s = env.svc.discover(SOURCE, SHA1)
        with pytest.raises(IntakeStateError):
            env.svc.fetch(s.tool.tool_id, SHA2)


# ---------------------------------------------------------------------------
# 4. Scanner unavailable → fail closed, never approvable
# ---------------------------------------------------------------------------

class TestScannerUnavailable:
    def test_scan_fails_and_state_stays_unapprovable(self, env) -> None:
        # Simulate a missing scanner binary via the injectable boundary.
        env.scanner.executable = "definitely-not-installed-xyz-9000"
        env.scanner.availability_check = lambda: False
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)

        with pytest.raises(IntakeScanError):
            env.svc.scan(tool_id, SHA1)

        row = env.svc.status(tool_id)[0]
        assert row["status"] == "scan_failed"
        assert "scanner unavailable" in row["label"]
        assert row["revision"]["scan_state"] == "unavailable"

        # check blocks; approve refuses even with manual review.
        _, result = env.svc.check(tool_id, SHA1)
        assert not result.passed
        with pytest.raises(IntakePolicyError, match="clean scan"):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="override")


# ---------------------------------------------------------------------------
# 5/6. Tamper detection
# ---------------------------------------------------------------------------

class TestTamperDetection:
    def test_tamper_after_approval_invalidates_at_install(self, env) -> None:
        _approve_to(env)
        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id
        rev = env.svc.status(tool_id)[0]["revision"]
        content = env.qroot / tool_id / SHA1 / "content" / "demo-skill" / "SKILL.md"
        content.write_bytes(b"# Tampered after approval\n")

        with pytest.raises(IntakeIntegrityError):
            env.svc.install(tool_id, SHA1)

        row = env.svc.status(tool_id)[0]
        assert row["status"] == "stale"
        assert "approval invalidated" in row["label"]
        assert row["revision"]["approved_by"] is None  # binding cleared
        assert rev["approved_by"] is not None  # was bound before tamper

    def test_tamper_before_scan_detected_on_refetch(self, env) -> None:
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        content = env.qroot / tool_id / SHA1 / "content" / "demo-skill" / "SKILL.md"
        content.write_bytes(b"# Tampered before scan\n")

        with pytest.raises(IntakeIntegrityError):
            env.svc.fetch(tool_id, SHA1)  # re-entry verifies the tree
        assert env.svc.status(tool_id)[0]["status"] == "stale"

    def test_scan_detects_tamper_and_fails(self, env) -> None:
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        content = env.qroot / tool_id / SHA1 / "content" / "demo-skill" / "SKILL.md"
        content.write_bytes(b"# Tampered\n")
        with pytest.raises(IntakeIntegrityError):
            env.svc.scan(tool_id, SHA1)


# ---------------------------------------------------------------------------
# 7. Approval bound to one revision only
# ---------------------------------------------------------------------------

class TestApprovalBinding:
    def test_approval_not_reusable_across_revisions(self, env) -> None:
        _approve_to(env, SHA1)
        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id

        # Second revision of the same tool.
        env.svc.discover(SOURCE, SHA2)
        env.svc.fetch(tool_id, SHA2)
        env.svc.scan(tool_id, SHA2)
        env.svc.check(tool_id, SHA2)

        # SHA1's approval does not authorize SHA2.
        with pytest.raises(IntakeStateError):
            env.svc.install(tool_id, SHA2)

        approved2 = env.svc.approve(
            tool_id, SHA2, manual_review=True,
            note="Attested operator-declared license provenance",
        )
        assert approved2.revision.approval_request_id
        rev1 = env.svc.status(tool_id)[0]  # sanity: both rows exist
        with get_connection(env.db) as conn:
            reqs = conn.execute(
                "SELECT request_id, target, action_category FROM approvals "
                "WHERE action_category='tool.install' ORDER BY rowid"
            ).fetchall()
        assert len(reqs) == 2
        assert reqs[0]["target"] != reqs[1]["target"]
        assert approved2.revision.revision_sha == SHA2
        assert rev1 is not None

    def test_fresh_approval_created_after_denial(self, env) -> None:
        from factory.models import ApprovalDecision, ApprovalStatus
        from factory.permissions.approvals import ApprovalManager

        # Reach PENDING_APPROVAL (request R1 created by check).
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        env.svc.check(tool_id, SHA1)
        req1 = env.svc.status(tool_id)[0]["revision"]["approval_request_id"]
        assert req1

        # Human denial on R1 (via the approvals system).
        mgr = ApprovalManager(db_path=env.db)
        mgr.record_decision(ApprovalDecision(
            request_id=req1, status=ApprovalStatus.DENIED,
            decided_by="human-cli", decision_note="no",
        ))

        # approve() must create a fresh request rather than reuse a denial.
        summary = env.svc.approve(
            tool_id, SHA1, manual_review=True,
            note="Attested operator-declared license provenance",
        )
        assert summary.revision.approval_request_id != req1
        assert summary.tool.status == IntakeStatus.APPROVED


# ---------------------------------------------------------------------------
# 8. Policy-blocked license: explicit manual review required
# ---------------------------------------------------------------------------

class TestPolicyBlockedLicense:
    def test_unknown_license_requires_manual_review(self, env) -> None:
        s = env.svc.discover(SOURCE, SHA1)  # no license recorded
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        checked, result = env.svc.check(tool_id, SHA1)
        assert not result.passed
        assert checked.tool.status == IntakeStatus.REJECTED

        with pytest.raises(IntakePolicyError, match="blocked"):
            env.svc.approve(tool_id, SHA1)

        # Manual review without a note is still refused.
        with pytest.raises(IntakePolicyError):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="  ")

        approved = env.svc.approve(
            tool_id, SHA1, manual_review=True,
            note="License verified by maintainer review",
        )
        codes = {r["code"] for r in approved.revision.policy_reasons}
        assert "MANUAL_REVIEW_OVERRIDE" in codes
        assert approved.tool.status == IntakeStatus.APPROVED

    def test_gpl_license_blocked_not_mislabelled(self, env) -> None:
        s = env.svc.discover(SOURCE, SHA1, license_declared="GPL-3.0")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        _, result = env.svc.check(tool_id, SHA1)
        assert not result.passed
        codes = {r.code for r in result.reasons}
        assert "LICENSE_RESTRICTIVE_MANUAL_REVIEW" in codes
        assert "LICENSE_PERMISSIVE" not in codes


# ---------------------------------------------------------------------------
# 8b. License provenance attestation (Finding 2): declared ≠ verified
# ---------------------------------------------------------------------------

class TestLicenseProvenanceAttestation:
    def test_declared_mit_shown_unverified_and_not_approved_silently(
        self, env,
    ) -> None:
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        # Provenance is recorded as operator-declared, never "verified".
        assert s.tool.license_source == "declared"

        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        checked, result = env.svc.check(tool_id, SHA1)

        # The unverified provenance is visible in policy reasons...
        assert "LICENSE_DECLARED_UNVERIFIED" in {r.code for r in result.reasons}
        assert "LICENSE_PERMISSIVE" in {r.code for r in result.reasons}
        reason = next(
            r for r in result.reasons
            if r.code == "LICENSE_DECLARED_UNVERIFIED"
        )
        assert "NOT been independently verified" in reason.detail
        assert "declared" in reason.detail
        rev_codes = {r["code"] for r in checked.revision.policy_reasons}
        assert "LICENSE_DECLARED_UNVERIFIED" in rev_codes
        # ...and status output shows the declared provenance.
        row = env.svc.status(tool_id)[0]
        assert row["license_source"] == "declared"

        # The permissive path reaches pending human approval (clean scan +
        # exact permissive match) — it does not silently complete.
        assert result.passed
        assert checked.tool.status == IntakeStatus.PENDING_APPROVAL

        # Approval without the attestation is refused...
        with pytest.raises(IntakePolicyError, match="attestation"):
            env.svc.approve(tool_id, SHA1)
        # ...and so is manual review with a blank note.
        with pytest.raises(IntakePolicyError):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="   ")

        approved = env.svc.approve(
            tool_id, SHA1, manual_review=True,
            note="Reviewed declared MIT against repository LICENSE wording",
        )
        approved_codes = {r["code"] for r in approved.revision.policy_reasons}
        # The attestation is persisted for audit...
        assert "LICENSE_PROVENANCE_ATTESTED" in approved_codes
        # ...without inventing a policy override (policy itself passed).
        assert "MANUAL_REVIEW_OVERRIDE" not in approved_codes
        assert approved.tool.status == IntakeStatus.APPROVED

    def test_provenance_attestation_recorded_in_events_never_as_verified(
        self, env,
    ) -> None:
        # Approved event carries the manual_review flag; no audit payload
        # ever labels the license provenance "verified".
        _approve_to(env)
        raw = ""
        with get_connection(env.db) as conn:
            for r in conn.execute(
                "SELECT payload FROM events WHERE event_type LIKE 'tool.intake.%'"
            ):
                raw += r["payload"]
        approved = [e for e in _events(env.db)
                    if e["type"] == "tool.intake.approved"]
        assert approved and approved[0]["payload"]["manual_review"] is True
        assert '"verified"' not in raw  # no provenance claim in audit events


# ---------------------------------------------------------------------------
# 9. Findings scan: completed scan, policy blocks, not mislabelled
# ---------------------------------------------------------------------------

class TestFindingsScan:
    def test_findings_scan_blocks_and_not_mislabelled(self, env) -> None:
        env.scanner.runner = FakeRunner(
            (FIXTURES / "report_findings.json").read_bytes()
        )
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        scanned = env.svc.scan(tool_id, SHA1)

        # A completed scan WITH findings is not a scan failure.
        assert scanned.revision.scan_state == ScanState.FINDINGS
        assert scanned.tool.status == IntakeStatus.SCANNED
        assert scanned.revision.unresolved_findings == 1

        _, result = env.svc.check(tool_id, SHA1)
        assert not result.passed
        codes = {r.code for r in result.reasons}
        assert "UPSTREAM_RECOMMENDATION_BLOCKED" in codes
        assert "UPSTREAM_CAUTION_RECORDED" not in codes

        # Findings scans cannot be approved (clean-scan gate).
        with pytest.raises(IntakePolicyError, match="clean scan"):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="x")

    def test_upstream_caution_recorded_as_caution(self, env) -> None:
        env.scanner.runner = FakeRunner(
            (FIXTURES / "report_caution.json").read_bytes()
        )
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        _, result = env.svc.check(tool_id, SHA1)
        codes = {r.code for r in result.reasons}
        assert "UPSTREAM_CAUTION_RECORDED" in codes
        assert "UPSTREAM_RECOMMENDATION_BLOCKED" not in codes
        assert "LOCAL_POLICY_STRICTER_SCORE" in codes


# ---------------------------------------------------------------------------
# 8c/9b. check() branch semantics (Finding 3): distinct, never conflated
# ---------------------------------------------------------------------------

class TestCheckBranchSemantics:
    def test_not_run_scan_checks_to_scan_failed(self, env) -> None:
        # check() BEFORE any scan: not_run is missing evidence, not a
        # policy rejection, and never approvable.
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)

        checked, result = env.svc.check(tool_id, SHA1)
        assert not result.passed
        assert checked.tool.status == IntakeStatus.SCAN_FAILED
        row = env.svc.status(tool_id)[0]
        assert row["status"] == "scan_failed"
        assert row["revision"]["scan_state"] == "not_run"
        assert "not run" in row["label"]

        with pytest.raises(IntakePolicyError, match="clean scan"):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="x")

    def test_failed_scan_checks_to_scan_failed(self, env) -> None:
        # FAILED (malformed report) → same fail-closed scan-evidence state.
        env.scanner.runner = FakeRunner(b"not json at all")
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        with pytest.raises(IntakeScanError):
            env.svc.scan(tool_id, SHA1)

        checked, result = env.svc.check(tool_id, SHA1)
        assert not result.passed
        assert checked.tool.status == IntakeStatus.SCAN_FAILED
        row = env.svc.status(tool_id)[0]
        assert row["status"] == "scan_failed"
        assert row["revision"]["scan_state"] == "failed"

        # Failed scans stay unapprovable even with manual review flags.
        with pytest.raises(IntakePolicyError, match="clean scan"):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="x")

    def test_findings_check_is_policy_rejection_not_scan_failed(
        self, env,
    ) -> None:
        # A COMPLETED scan with findings: status is REJECTED with a label
        # that says "completed with findings" — never "scan failed".
        env.scanner.runner = FakeRunner(
            (FIXTURES / "report_findings.json").read_bytes()
        )
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)

        checked, result = env.svc.check(tool_id, SHA1)
        assert not result.passed
        assert checked.tool.status == IntakeStatus.REJECTED
        assert checked.tool.status != IntakeStatus.SCAN_FAILED
        label = env.svc.status(tool_id)[0]["label"]
        assert "completed with findings" in label
        assert "not a scanner failure" in label
        assert "scan failed" not in label

    def test_clean_and_blocked_license_checks_to_policy_rejection(
        self, env,
    ) -> None:
        # CLEAN + policy block (no license declared) → REJECTED with the
        # policy-rejection label (distinct from scan evidence failures).
        s = env.svc.discover(SOURCE, SHA1)
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)

        checked, result = env.svc.check(tool_id, SHA1)
        assert not result.passed
        assert checked.tool.status == IntakeStatus.REJECTED
        label = env.svc.status(tool_id)[0]["label"]
        assert "rejected by deterministic policy" in label

    def test_clean_and_pass_checks_to_pending_human_approval(self, env) -> None:
        # CLEAN + policy pass (clean report, exact permissive license,
        # no extra capabilities) → PENDING_APPROVAL with a bound request.
        # Provenance is declared/unverified, which does not block check();
        # approval then requires the attestation (see section 8b).
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)

        checked, result = env.svc.check(tool_id, SHA1)
        assert result.passed
        assert checked.tool.status == IntakeStatus.PENDING_APPROVAL
        assert checked.revision.approval_request_id
        assert "pending human approval" in env.svc.status(tool_id)[0]["label"]


# ---------------------------------------------------------------------------
# 10. Re-fetch semantics: idempotent vs changed bytes
# ---------------------------------------------------------------------------

class TestRefetchSemantics:
    def test_refetch_same_bytes_is_idempotent(self, env) -> None:
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        first_hash = env.svc.status(tool_id)[0]["revision"]["artifact_sha256"]

        again = env.svc.fetch(tool_id, SHA1)
        assert again.detail.get("already") == "quarantined"
        assert len(env.transport.calls) == 1  # no second download
        assert env.svc.status(tool_id)[0]["revision"]["artifact_sha256"] == first_hash

    def test_changed_upstream_bytes_refuse_overwrite_and_invalidate(self, env) -> None:
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        artifact = env.qroot / tool_id / SHA1 / "source.tar.gz"
        original_bytes = artifact.read_bytes()

        # Simulate a different download payload at the same pinned revision.
        tampered = _skill_tar() + b"different"
        env.transport.payload = tampered

        # Delete artifact so the already-quarantined early-return is skipped,
        # forcing the re-download path.
        artifact.unlink()

        with pytest.raises(IntakeIntegrityError):
            env.svc.fetch(tool_id, SHA1)

        # Nothing silently overwritten: recorded approval/scan reset, stale.
        row = env.svc.status(tool_id)[0]
        assert row["status"] == "stale"
        assert row["revision"]["approved_by"] is None
        assert row["revision"]["scan_state"] == "not_run"


# ---------------------------------------------------------------------------
# 11. Revoke retains evidence
# ---------------------------------------------------------------------------

class TestRevoke:
    def test_disable_revokes_but_retains_evidence(self, env) -> None:
        _approve_to(env)
        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id
        env.svc.install(tool_id, SHA1)

        tool = env.svc.disable(tool_id)
        assert tool.status == IntakeStatus.REVOKED
        assert "revoked" in env.svc.status(tool_id)[0]["label"]
        assert "evidence retained" in env.svc.status(tool_id)[0]["label"]

        # Rows and evidence survive.
        with get_connection(env.db) as conn:
            rev = conn.execute(
                "SELECT * FROM tool_revisions WHERE tool_id=?", (tool_id,)
            ).fetchone()
            ev = conn.execute(
                "SELECT * FROM evidence WHERE kind='scan_result'"
            ).fetchall()
        assert rev is not None
        assert len(ev) >= 1

        # Revoked tools cannot be installed or re-approved.
        with pytest.raises(IntakeStateError):
            env.svc.install(tool_id, SHA1)
        with pytest.raises(IntakeStateError):
            env.svc.approve(tool_id, SHA1)


# ---------------------------------------------------------------------------
# 12. Audit events: success + failure, no secret/report-content leakage
# ---------------------------------------------------------------------------

class TestAuditTrail:
    def test_success_transitions_all_evented(self, env) -> None:
        _approve_to(env)
        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id
        env.svc.install(tool_id, SHA1)
        types = [e["type"] for e in _events(env.db)]
        for expected in [
            "tool.intake.discovered",
            "tool.intake.fetch_completed",
            "tool.intake.scan_completed",
            "tool.intake.pending_approval",
            "tool.intake.approved",
            "tool.intake.installed",
        ]:
            assert expected in types, f"missing event {expected}"

    def test_failed_fetch_evented_with_reason(self, env) -> None:
        env.transport.error = IntakeFetchError("simulated download failure")
        s = env.svc.discover(SOURCE, SHA1)
        with pytest.raises(IntakeFetchError):
            env.svc.fetch(s.tool.tool_id, SHA1)
        events = _events(env.db)
        failed = [e for e in events if e["type"] == "tool.intake.fetch_failed"]
        assert len(failed) == 1
        assert "simulated download failure" in failed[0]["payload"]["reason"]
        assert failed[0]["payload"]["revision_sha"] == SHA1

    def test_failed_scan_evented(self, env) -> None:
        env.scanner.runner = FakeRunner(b"not json at all")
        s = env.svc.discover(SOURCE, SHA1)
        env.svc.fetch(s.tool.tool_id, SHA1)
        with pytest.raises(IntakeScanError):
            env.svc.scan(s.tool.tool_id, SHA1)
        types = [e["type"] for e in _events(env.db)]
        assert "tool.intake.scan_failed" in types

    def test_events_do_not_leak_report_content_or_secrets(self, env) -> None:
        # Report contains a marker that must never appear in audit events.
        marker = "SECRET_MARKER_XYZ_DO_NOT_LOG"
        report = {
            "risk_score": 50, "severity": "HIGH",
            "recommendation": "CAUTION",
            "findings": [{"id": "X1", "severity": "HIGH", "title": marker}],
        }
        env.scanner.runner = FakeRunner(json.dumps(report).encode())
        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        env.svc.fetch(s.tool.tool_id, SHA1)
        env.svc.scan(s.tool.tool_id, SHA1)

        raw_events = ""
        with get_connection(env.db) as conn:
            for row in conn.execute(
                "SELECT payload FROM events WHERE event_type LIKE 'tool.intake.%'"
            ):
                raw_events += row["payload"]
        assert marker not in raw_events
        # The raw report (with the marker) IS preserved as evidence, though.
        with get_connection(env.db) as conn:
            ev = conn.execute(
                "SELECT path FROM evidence WHERE kind='scan_result' "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        assert marker in Path(ev["path"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 13. Installed skills are inert: no auto-enable, no prompt injection
# ---------------------------------------------------------------------------

class TestInstalledSkillsInert:
    def test_installed_event_marks_enabled_false(self, env) -> None:
        _approve_to(env)
        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id
        env.svc.install(tool_id, SHA1)
        installed = [e for e in _events(env.db)
                     if e["type"] == "tool.intake.installed"]
        assert installed and installed[0]["payload"]["enabled"] is False

    def test_agents_and_context_do_not_reference_intake(self, tmp_path: Path) -> None:
        # No agent or context module may import the intake package —
        # installed skills must not be auto-loaded into prompts.
        root = Path(__file__).resolve().parents[2] / "factory"
        for rel in ["agents", "context"]:
            for py in (root / rel).rglob("*.py"):
                text = py.read_text(encoding="utf-8")
                assert "factory.intake" not in text, (
                    f"{py} references factory.intake — installed skills must "
                    "not be auto-injected"
                )


# ---------------------------------------------------------------------------
# 13b. Install destination must be git-ignored (Finding 4)
# ---------------------------------------------------------------------------

class TestInstallIgnoreGate:
    def test_unignored_install_destination_fails_before_any_write(
        self, env,
    ) -> None:
        # Reach APPROVED via the permissive (bypassing) test checker...
        _approve_to(env)
        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id

        # ...then install through a service whose checker says the install
        # root is NOT git-ignored (quarantine still fine for consistency).
        blocked = IntakeService(
            db_path=env.db,
            quarantine_root=env.qroot,
            install_root=env.iroot,
            transport=env.transport,
            scanner=env.scanner,
            ignore_checker=lambda p: p != env.iroot,
            total_cap_bytes=50 * 1024 * 1024,
            fetch_timeout_s=5,
            scan_timeout_s=5,
        )
        with pytest.raises(IntakeInstallError, match="gitignore"):
            blocked.install(tool_id, SHA1)

        # Fail-closed before writing: no install dir was ever created.
        assert not env.iroot.exists()
        # The revision stays approved (not installed) for a retry after
        # the operator fixes .gitignore.
        row = env.svc.status(tool_id)[0]
        assert row["status"] == "approved"
        # The block is audited.
        events = [e for e in _events(env.db)
                  if e["type"] == "tool.intake.install_blocked"]
        assert len(events) == 1
        assert events[0]["payload"]["tool_id"] == tool_id
        assert events[0]["payload"]["revision_sha"] == SHA1

    def test_ignored_destination_installs_inert_files(self, env) -> None:
        # The env fixture's checker treats the install root as ignored →
        # install succeeds and the content lands disabled/unloaded.
        _approve_to(env)
        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id
        summary = env.svc.install(tool_id, SHA1)

        dest = Path(summary.tool.install_path or "")
        assert (dest / "demo-skill" / "SKILL.md").read_bytes() == SKILL_MD
        assert (dest / "demo-skill" / "reference" / "notes.md").read_bytes() == NOTES_MD
        # Inert: disabled/unloaded, never enabled at install time.
        assert "disabled/unloaded" in env.svc.status(tool_id)[0]["label"]
        installed = [e for e in _events(env.db)
                     if e["type"] == "tool.intake.installed"]
        assert installed and installed[0]["payload"]["enabled"] is False


# ---------------------------------------------------------------------------
# CLI parser smoke (offline, no DB)
# ---------------------------------------------------------------------------

class TestCliParser:
    def test_register_and_parse_intake_commands(self) -> None:
        from factory.cli.intake_cmd import register_intake_subparser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register_intake_subparser(sub)

        args = parser.parse_args([
            "intake", "discover", "--source", SOURCE, "--revision", SHA1,
            "--license", "MIT", "--capability", "shell",
        ])
        assert args.intake_command == "discover"
        assert args.source == SOURCE
        assert args.revision == SHA1
        assert args.capability == ["shell"]

        args = parser.parse_args([
            "intake", "approve", "abc123", "--revision", SHA1,
            "--manual-review", "--note", "reviewed",
        ])
        assert args.intake_command == "approve"
        assert args.tool_id == "abc123"
        assert args.manual_review is True
        assert args.note == "reviewed"

        args = parser.parse_args(["intake", "status"])
        assert args.intake_command == "status"
        assert args.tool_id is None


# ---------------------------------------------------------------------------
# CLI provenance output (Finding 2): declared is never shown as verified
# ---------------------------------------------------------------------------

class TestCliProvenanceOutput:
    def test_license_provenance_label_distinguishes_declared(self) -> None:
        from factory.cli.intake_cmd import _license_provenance_label

        declared = _license_provenance_label("declared")
        assert "operator-declared" in declared
        assert "NOT independently verified" in declared
        assert declared != _license_provenance_label("verified")
        assert _license_provenance_label("verified") == "independently verified"
        assert "not recorded" in _license_provenance_label("unknown")

    def test_cmd_check_shows_provenance_reason_and_attestation_hint(
        self, env, monkeypatch, capsys,
    ) -> None:
        from factory.cli import intake_cmd

        s = env.svc.discover(SOURCE, SHA1, license_declared="MIT")
        tool_id = s.tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)

        monkeypatch.setattr(intake_cmd, "_svc", lambda: env.svc)
        rc = intake_cmd.cmd_check(SimpleNamespace(tool_id=tool_id, revision=SHA1))
        out = capsys.readouterr().out

        assert rc == 0
        # License line shows declared provenance explicitly...
        assert "operator-declared, NOT independently verified" in out
        # ...the unverified reason is listed...
        assert "LICENSE_DECLARED_UNVERIFIED" in out
        # ...and pending next-steps demand the attestation.
        assert "pending_approval" in out
        assert "--manual-review --note" in out


# ---------------------------------------------------------------------------
# H1: REVOKED is a terminal kill-switch (service + central registry guard)
# ---------------------------------------------------------------------------

class TestRevocationKillSwitch:
    def test_revoked_tool_rejects_scan_check_fetch_approve_install(
        self, env,
    ) -> None:
        from factory.intake import registry

        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        checked, result = env.svc.check(tool_id, SHA1)
        assert result.passed
        assert checked.tool.status == IntakeStatus.PENDING_APPROVAL

        transport_calls = list(env.transport.calls)
        scanner_calls = [list(c) for c in env.scanner.runner.calls]
        rev_before = registry.get_revision(tool_id, SHA1, env.db)
        approval_request = rev_before.approval_request_id
        assert approval_request  # a pending approval exists pre-revoke

        env.svc.disable(tool_id)
        assert env.svc.status(tool_id)[0]["status"] == "revoked"

        # scan is rejected BEFORE the scanner runs: zero invocations.
        with pytest.raises(IntakeStateError, match="REVOKED"):
            env.svc.scan(tool_id, SHA1)
        assert env.scanner.runner.calls == scanner_calls

        # check is rejected: no approval can be created or renewed.
        with pytest.raises(IntakeStateError, match="REVOKED"):
            env.svc.check(tool_id, SHA1)
        rev_after = registry.get_revision(tool_id, SHA1, env.db)
        assert rev_after.approval_request_id == approval_request

        # fetch is rejected BEFORE transport downloads anything.
        with pytest.raises(IntakeStateError, match="REVOKED"):
            env.svc.fetch(tool_id, SHA1)
        assert env.transport.calls == transport_calls

        # approve is rejected even with an explicit manual-review override.
        with pytest.raises(IntakeStateError, match="REVOKED"):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="forced")

        # install is rejected BEFORE any bytes reach the install root.
        with pytest.raises(IntakeStateError, match="REVOKED"):
            env.svc.install(tool_id, SHA1)
        assert not (env.iroot / tool_id / SHA1).exists()
        assert not env.iroot.exists()

        # Terminal throughout: status never left REVOKED across the chain.
        assert env.svc.status(tool_id)[0]["status"] == "revoked"

    def test_rediscovery_cannot_mutate_or_reactivate_revoked_tool(
        self, env,
    ) -> None:
        from factory.intake import registry

        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.disable(tool_id)

        with pytest.raises(IntakeStateError, match="REVOKED"):
            env.svc.discover(
                SOURCE, SHA1, license_declared="GPL-3.0-only",
                capabilities=["shell"],
            )

        after = registry.get_tool(tool_id, env.db)
        assert after.status == IntakeStatus.REVOKED   # not silently reactivated
        assert after.license_declared == "MIT"         # record not mutated
        assert after.capabilities == []

    def test_central_transition_guard_blocks_unrevocation(self, env) -> None:
        from factory.intake import registry

        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id
        env.svc.disable(tool_id)

        # Central choke point: no ordinary transition out of REVOKED.
        with pytest.raises(IntakeStateError, match="terminal"):
            registry.set_tool_status(tool_id, IntakeStatus.SCANNED, env.db)
        with pytest.raises(IntakeStateError, match="terminal"):
            registry.set_tool_status(tool_id, IntakeStatus.PENDING_APPROVAL, env.db)
        # Idempotent re-revocation remains allowed.
        registry.set_tool_status(tool_id, IntakeStatus.REVOKED, env.db)

        # Even a REVOKED -> REVOKED upsert may not rewrite the record.
        tool = registry.get_tool(tool_id, env.db)
        original_name = tool.name
        tool.name = "hijacked-name"           # pydantic model mutation
        tool.status = IntakeStatus.SCANNED     # attempts a reactivating write
        with pytest.raises(IntakeStateError, match="REVOKED"):
            registry.upsert_tool(tool, env.db)
        after = registry.get_tool(tool_id, env.db)
        assert after.name == original_name
        assert after.status == IntakeStatus.REVOKED


# ---------------------------------------------------------------------------
# M1: fresh scan evidence only — a stale report is never reused
# ---------------------------------------------------------------------------

class NoWriteRunner:
    """Scanner runner that exits WITHOUT producing any report."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, cwd: Path, timeout_s: float):
        import subprocess
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, self.exit_code, stdout="", stderr=""
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestStaleScanEvidence:
    @pytest.mark.parametrize("exit_code", [0, 1])
    def test_failed_rescan_cannot_approve_on_earlier_clean_scan(
        self, env, exit_code,
    ) -> None:
        from factory.intake import registry

        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        env.svc.check(tool_id, SHA1)

        before = registry.get_revision(tool_id, SHA1, env.db)
        old_path = Path(before.scan_report_path or "")
        old_hash = before.scan_report_sha256
        # Pre-existing VALID clean report at the recorded destination.
        assert old_hash and old_path.is_file()

        # Rescan with a runner that exits 0/1 but writes NO report.
        env.scanner.runner = NoWriteRunner(exit_code=exit_code)
        with pytest.raises(IntakeScanError, match="no report file"):
            env.svc.scan(tool_id, SHA1)

        after = registry.get_revision(tool_id, SHA1, env.db)
        row = env.svc.status(tool_id)[0]
        assert row["status"] == "scan_failed"
        assert after.scan_state == ScanState.FAILED
        # Stale bytes were NOT consumed as fresh evidence: the historical
        # report reference is retained and still matches the untouched file.
        assert after.scan_report_path == str(old_path)
        assert after.scan_report_sha256 == old_hash
        assert _sha256(old_path.read_bytes()) == old_hash
        # The failed attempt published no report and left no temp artifacts.
        tool_dir = env.qroot / tool_id / SHA1
        assert len(list(tool_dir.glob("scan-report-*.json"))) == 1
        assert [p for p in tool_dir.iterdir() if "partial" in p.name] == []

        # The earlier clean scan can no longer authorize approval.
        checked, result = env.svc.check(tool_id, SHA1)
        assert not result.passed
        assert checked.tool.status == IntakeStatus.SCAN_FAILED
        with pytest.raises(IntakePolicyError, match="clean scan"):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="forced")
        assert env.svc.status(tool_id)[0]["status"] == "scan_failed"

    def test_rescan_publishes_fresh_report_and_keeps_history(self, env) -> None:
        from factory.intake import registry

        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        first = registry.get_revision(tool_id, SHA1, env.db)
        first_path = Path(first.scan_report_path or "")
        first_hash = first.scan_report_sha256
        assert first.scan_state == ScanState.CLEAN and first_hash

        # Rescan producing a DIFFERENT, findings-bearing fresh report.
        env.scanner.runner = FakeRunner(
            (FIXTURES / "report_findings.json").read_bytes()
        )
        scanned = env.svc.scan(tool_id, SHA1)

        second_path = Path(scanned.revision.scan_report_path or "")
        second_hash = scanned.revision.scan_report_sha256
        assert scanned.revision.scan_state == ScanState.FINDINGS
        assert second_hash and second_hash != first_hash
        # Fresh bytes live at a NEW destination; earlier evidence survives.
        assert second_path != first_path
        assert _sha256(second_path.read_bytes()) == second_hash
        assert _sha256(first_path.read_bytes()) == first_hash
        assert len(list((env.qroot / tool_id / SHA1).glob("scan-report-*.json"))) == 2

    def test_scanner_identity_binds_version_from_fresh_report(self, env) -> None:
        from factory.intake import registry

        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)

        # Flat fixture carries no version -> identity omits it (never guessed).
        env.svc.scan(tool_id, SHA1)
        identity = registry.get_revision(tool_id, SHA1, env.db).scanner_identity
        assert identity == "skillspector(static,no-llm)"

        # The pinned v2 report embeds its concrete version in metadata —
        # an already-established local interface (no extra invocation).
        env.scanner.runner = FakeRunner(
            (FIXTURES / "report_realv212_clean.json").read_bytes()
        )
        env.svc.scan(tool_id, SHA1)
        identity = registry.get_revision(tool_id, SHA1, env.db).scanner_identity
        # Legacy identity token kept verbatim; concrete version appended
        # only because THIS report carried one.
        assert identity == "skillspector(static,no-llm),version=2.12.0"

    def test_failed_rescan_preserves_versioned_scanner_identity(self, env) -> None:
        from factory.intake import registry

        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)

        # First scan succeeds with a report that carries a concrete version.
        env.scanner.runner = FakeRunner(
            (FIXTURES / "report_realv212_clean.json").read_bytes()
        )
        env.svc.scan(tool_id, SHA1)
        first = registry.get_revision(tool_id, SHA1, env.db)
        identity = first.scanner_identity
        assert identity == "skillspector(static,no-llm),version=2.12.0"
        old_path = Path(first.scan_report_path or "")
        old_hash = first.scan_report_sha256
        assert old_hash and old_path.is_file()

        # A fatal scanner exit forces a failed rescan.
        env.scanner.runner = NoWriteRunner(exit_code=2)
        with pytest.raises(IntakeScanError):
            env.svc.scan(tool_id, SHA1)

        after = registry.get_revision(tool_id, SHA1, env.db)
        # Identity preserved value-for-value — no generic downgrade and no
        # version-evidence loss from the failed attempt.
        assert after.scanner_identity == identity
        row = env.svc.status(tool_id)[0]
        assert row["status"] == "scan_failed"
        assert after.scan_state == ScanState.FAILED
        # Historical report reference still matches the untouched bytes.
        assert after.scan_report_path == str(old_path)
        assert after.scan_report_sha256 == old_hash
        assert _sha256(old_path.read_bytes()) == old_hash
        # Still non-approvable, even under an explicit override.
        with pytest.raises(IntakePolicyError, match="clean scan"):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="forced")

    def test_initial_scan_failure_leaves_identity_none(self, env) -> None:
        from factory.intake import registry

        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)

        # No scan has ever succeeded when this one fails.
        env.scanner.runner = NoWriteRunner(exit_code=1)
        with pytest.raises(IntakeScanError):
            env.svc.scan(tool_id, SHA1)

        after = registry.get_revision(tool_id, SHA1, env.db)
        # Identity stays None — never invented for a failed attempt.
        assert after.scanner_identity is None
        assert after.scan_state == ScanState.FAILED
        assert env.svc.status(tool_id)[0]["status"] == "scan_failed"


# ---------------------------------------------------------------------------
# M2: policy is revalidated against CURRENT metadata at approval time
# ---------------------------------------------------------------------------

class TestStalePolicyRevalidation:
    def test_license_change_after_check_blocks_stale_approval(self, env) -> None:
        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        checked, result = env.svc.check(tool_id, SHA1)
        assert result.passed  # MIT + clean scan -> pending human approval

        # Rediscovery rewrites the license AFTER check (status preserved).
        env.svc.discover(SOURCE, SHA1, license_declared="GPL-3.0-only")
        row = env.svc.status(tool_id)[0]
        assert row["status"] == "pending_approval"
        assert row["license_declared"] == "GPL-3.0-only"

        # The stale recorded 'pass' cannot authorize approval — NOT even
        # with an explicit manual-review override (that override only covers
        # a still-current blocked decision).
        with pytest.raises(IntakePolicyError, match="changed since check"):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="forced")

        # Proper explicit review flow: re-check reflects the current license.
        checked2, result2 = env.svc.check(tool_id, SHA1)
        assert not result2.passed
        assert checked2.tool.status == IntakeStatus.REJECTED
        assert any(
            r["code"] == "LICENSE_RESTRICTIVE_MANUAL_REVIEW"
            for r in checked2.revision.policy_reasons
        )
        # Approval is only possible through that reviewed flow.
        approved = env.svc.approve(
            tool_id, SHA1, manual_review=True,
            note="Reviewed GPL terms for internal use",
        )
        assert approved.tool.status == IntakeStatus.APPROVED

    def test_capability_change_after_check_blocks_stale_approval(
        self, env,
    ) -> None:
        # Prompt/reference-only posture: no privileged capabilities requested.
        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        checked, result = env.svc.check(tool_id, SHA1)
        assert result.passed

        # Rediscovery raises the request from prompt-only to shell AFTER check.
        env.svc.discover(SOURCE, SHA1, license_declared="MIT",
                         capabilities=["shell"])
        from factory.intake import registry
        assert registry.get_tool(tool_id, env.db).capabilities == ["shell"]
        assert env.svc.status(tool_id)[0]["status"] == "pending_approval"

        # Stale 'pass' blocked even under an explicit override.
        with pytest.raises(IntakePolicyError, match="changed since check"):
            env.svc.approve(tool_id, SHA1, manual_review=True, note="forced")

        checked2, result2 = env.svc.check(tool_id, SHA1)
        assert not result2.passed
        assert any(
            r["code"] == "CAPABILITY_BLOCKED_SHELL"
            for r in checked2.revision.policy_reasons
        )
        approved = env.svc.approve(
            tool_id, SHA1, manual_review=True,
            note="Reviewed shell capability grant",
        )
        assert approved.tool.status == IntakeStatus.APPROVED


# ---------------------------------------------------------------------------
# L6: failed extraction never destroys pre-existing quarantine content
# ---------------------------------------------------------------------------

class TestFetchFailurePreservesContent:
    def test_failed_fetch_preserves_preexisting_content_sentinel(self, env) -> None:
        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id

        # Attempt 1: invalid archive payload -> extraction rejected.
        env.transport.payload = b"this is not a gzip stream at all"
        with pytest.raises(IntakeFetchError):
            env.svc.fetch(tool_id, SHA1)
        assert env.svc.status(tool_id)[0]["status"] == "discovery_failed"

        # Pre-existing destination content sits where extraction would go.
        tool_dir = env.qroot / tool_id / SHA1
        content = tool_dir / "content"
        content.mkdir(parents=True, exist_ok=True)
        sentinel = content / "sentinel.txt"
        sentinel.write_bytes(b"pre-existing quarantine content")

        # Attempt 2: still-invalid archive must NOT delete that content.
        with pytest.raises(IntakeFetchError):
            env.svc.fetch(tool_id, SHA1)

        assert sentinel.read_bytes() == b"pre-existing quarantine content"
        assert {p.name for p in tool_dir.iterdir()} >= {"content", "source.tar.gz"}
        # The owned temporary extraction directory was cleaned up.
        assert [p.name for p in tool_dir.iterdir() if "partial" in p.name] == []


# ---------------------------------------------------------------------------
# L7: install-root creation failures surface as typed intake errors
# ---------------------------------------------------------------------------

class TestInstallRootFailures:
    def test_install_root_mkdir_failure_is_typed_error(self, env, monkeypatch) -> None:
        _approve_to(env)
        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id
        target = env.iroot / tool_id  # dest.parent mkdir target

        real_mkdir = Path.mkdir

        def _failing_mkdir(self, *args, **kwargs):
            if Path(self) == target:
                raise PermissionError(13, "simulated permission denied")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _failing_mkdir)

        with pytest.raises(IntakeInstallError,
                           match="Cannot create install directory"):
            env.svc.install(tool_id, SHA1)

        # Clean failure: nothing written, approval state untouched.
        assert not target.exists()
        assert env.svc.status(tool_id)[0]["status"] == "approved"


# ---------------------------------------------------------------------------
# L1: check exit status reflects blocked/non-approvable outcomes
# ---------------------------------------------------------------------------

class TestCliExitCodes:
    def test_cmd_check_pending_next_state_returns_zero(
        self, env, monkeypatch, capsys,
    ) -> None:
        from factory.cli import intake_cmd

        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)

        monkeypatch.setattr(intake_cmd, "_svc", lambda: env.svc)
        rc = intake_cmd.cmd_check(SimpleNamespace(tool_id=tool_id, revision=SHA1))
        out = capsys.readouterr().out

        # Genuinely reached a valid next state: pending human approval.
        assert rc == 0
        assert "pending_approval" in out

    def test_cmd_check_blocked_returns_nonzero(
        self, env, monkeypatch, capsys,
    ) -> None:
        from factory.cli import intake_cmd

        # No declared license -> deterministic policy blocks.
        tool_id = env.svc.discover(SOURCE, SHA1).tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)

        monkeypatch.setattr(intake_cmd, "_svc", lambda: env.svc)
        rc = intake_cmd.cmd_check(SimpleNamespace(tool_id=tool_id, revision=SHA1))
        captured = capsys.readouterr()

        assert rc != 0
        assert "BLOCKED" in captured.out
        assert "not approvable" in captured.out
        assert env.svc.status(tool_id)[0]["status"] == "rejected"


# ---------------------------------------------------------------------------
# L4: non-interactive approve fails cleanly (no traceback, no side effect)
# ---------------------------------------------------------------------------

class TestApproveNonInteractive:
    def test_approve_without_interactive_stdin_fails_cleanly(
        self, env, monkeypatch, capsys,
    ) -> None:
        from factory.cli import intake_cmd

        tool_id = env.svc.discover(SOURCE, SHA1, license_declared="MIT").tool.tool_id
        env.svc.fetch(tool_id, SHA1)
        env.svc.scan(tool_id, SHA1)
        env.svc.check(tool_id, SHA1)

        monkeypatch.setattr(intake_cmd, "_svc", lambda: env.svc)

        def _closed_stdin(*_args, **_kwargs):
            raise EOFError

        monkeypatch.setattr("builtins.input", _closed_stdin)

        rc = intake_cmd.cmd_approve(SimpleNamespace(
            tool_id=tool_id, revision=SHA1, yes=False,
            manual_review=False, note="",
        ))
        captured = capsys.readouterr()

        # Concise actionable error, nonzero status, no traceback.
        assert rc != 0
        assert "--yes" in captured.err
        assert "not interactive" in captured.err
        assert "Traceback" not in captured.err

        # No approval side effect: still pending, nothing bound.
        from factory.intake import registry

        row = env.svc.status(tool_id)[0]
        assert row["status"] == "pending_approval"
        assert row["revision"]["approved_at"] is None
        rev = registry.get_revision(tool_id, SHA1, env.db)
        assert rev.approval_artifact_sha256 is None
        assert rev.approved_at is None
