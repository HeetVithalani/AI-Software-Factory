"""
factory.intake.service
=======================
Lifecycle orchestration for the trusted tool intake (Phase 2a).

    DISCOVERED -> QUARANTINED -> SCANNED -> PENDING_APPROVAL -> APPROVED -> INSTALLED

Failure/governance states: DISCOVERY_FAILED, SCAN_FAILED, REJECTED, STALE,
REVOKED. Installed skills are inert files: no code path in Phase 2a loads
or enables an installed skill into any agent.

Fail-closed invariants enforced here:
* Approvals are bound to (tool_id, kind, revision SHA, artifact hash,
  scan-report hash). Any byte change invalidates them (STALE + rescan).
* A completed clean scan is a hard precondition for approval — no flag
  can bypass missing/malformed/incomplete scan evidence.
* Policy-blocked revisions (license/capability/score) require an explicit
  manual-review override with a recorded reason; they are never silently
  approved.
* Every meaningful transition emits an audit event; scan reports are
    persisted verbatim as evidence artifacts with their SHA-256.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from factory.audit.evidence import EvidenceRecord, attach_evidence
from factory.errors import (
    IntakeError,
    IntakeFetchError,
    IntakeInstallError,
    IntakeIntegrityError,
    IntakePolicyError,
    IntakeScanError,
    IntakeStateError,
)
from factory.events import FactoryEvent
from factory.audit.logger import record_event
from factory.intake import registry
from factory.intake.archive import (
    ArchiveCaps,
    hash_file_sha256,
    hash_tree,
    safe_extract_tar_gz,
    tree_size_bytes,
)
from factory.intake.policy import PolicyResult, evaluate_policy
from factory.intake.scanner import ScanOutcome, SkillSpectorScanner
from factory.intake.source import GitHubSource, derive_tool_id, parse_github_source
from factory.intake.transport import HttpxTransport, Transport
from factory.models import (
    ApprovalDecision,
    ApprovalStatus,
    IntakeStatus,
    RiskLevel,
    ScanState,
    ToolKind,
    ToolRecord,
    ToolRevision,
)
from uuid import uuid4

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scanner_identity(scanner: SkillSpectorScanner, version: str | None) -> str | None:
    """
    Evidence identity for the scanner configuration behind a stored report.

    The concrete version is included only when THIS report carried one (the
    pinned SkillSpector CLI embeds ``metadata.skillspector_version`` in its
    JSON output — an already-established local interface). When no version
    is available it is omitted rather than guessed.
    """
    if not scanner.available():
        return None
    base = "skillspector(static,no-llm)"
    return f"{base},version={version}" if version else base


# ---------------------------------------------------------------------------
# Git-ignore verification (injectable)
# ---------------------------------------------------------------------------

class GitIgnoreChecker:
    """
    Verifies a path is actually ignored by Git before anything is written
    there. Fails closed if git itself cannot answer.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    def __call__(self, path: Path) -> bool:
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, local git only
                ["git", "-C", str(self._repo_root), "check-ignore", "-q", "--", str(path)],
                capture_output=True,
                shell=False,
                check=False,
            )
        except OSError as exc:
            raise IntakeFetchError(
                f"Cannot verify the quarantine directory is git-ignored: {exc}"
            ) from exc
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise IntakeFetchError(
            "git check-ignore could not determine ignore status "
            f"(exit {proc.returncode}); refusing to write."
        )


@dataclass
class IntakeSummary:
    """Result summary returned by lifecycle actions (for CLI rendering)."""
    tool: ToolRecord
    revision: ToolRevision
    detail: dict[str, Any] = field(default_factory=dict)


class IntakeService:
    """
    Orchestrates the intake lifecycle. All external boundaries (download,
    scanner, git-ignore check) are injectable so tests run fully offline.
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        quarantine_root: Path | None = None,
        install_root: Path | None = None,
        transport: Transport | None = None,
        scanner: SkillSpectorScanner | None = None,
        ignore_checker: Callable[[Path], bool] | None = None,
        caps: ArchiveCaps | None = None,
        total_cap_bytes: int | None = None,
        fetch_timeout_s: float | None = None,
        scan_timeout_s: float | None = None,
    ) -> None:
        from factory.config import FACTORY_ROOT, get_settings

        settings = get_settings().intake
        self._db_path = db_path
        self.quarantine_root = Path(quarantine_root or settings.quarantine_root)
        self.install_root = Path(install_root or settings.install_root)
        self._transport = transport or HttpxTransport()
        self._scanner = scanner or SkillSpectorScanner(
            executable=settings.skillspector_executable
        )
        self._ignore_checker = ignore_checker or GitIgnoreChecker(FACTORY_ROOT)
        self._caps = caps or ArchiveCaps(
            max_archive_bytes=settings.max_archive_bytes,
            max_extracted_bytes=settings.max_extracted_bytes,
            max_file_count=settings.max_file_count,
            max_path_length=settings.max_path_length,
        )
        self._total_cap = total_cap_bytes or settings.total_cap_bytes
        self._fetch_timeout_s = fetch_timeout_s or float(settings.fetch_timeout_seconds)
        self._scan_timeout_s = scan_timeout_s or float(settings.scan_timeout_seconds)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Structured audit event. Payloads carry identifiers, hashes, and
        reason codes — never raw report contents or credentials."""
        record_event(
            FactoryEvent(event_type=event_type, payload=payload),
            db_path=self._db_path,
        )

    def _require_tool_revision(
        self, tool_id: str, revision_sha: str
    ) -> tuple[ToolRecord, ToolRevision]:
        tool = registry.get_tool(tool_id, self._db_path)
        if tool is None:
            raise IntakeStateError(f"Unknown tool id: {tool_id!r} (run discover first).")
        rev = registry.get_revision(tool_id, revision_sha, self._db_path)
        if rev is None:
            raise IntakeStateError(
                f"Unknown revision {revision_sha!r} for tool {tool_id!r}."
            )
        # Central kill-switch: every lifecycle operation (fetch, scan, check,
        # approve, install) enters through here, so a REVOKED tool is
        # rejected BEFORE any write, status mutation, scanner invocation, or
        # file copy. Reactivation requires a separate auditable command.
        if tool.status == IntakeStatus.REVOKED:
            raise IntakeStateError(
                f"Tool {tool_id!r} is REVOKED (terminal kill-switch); "
                "fetch/scan/check/approve/install are permanently rejected. "
                "Reactivation requires a separate auditable command "
                "(not available in this release)."
            )
        return tool, rev

    def _artifact_path(self, rev: ToolRevision) -> Path:
        return self.quarantine_root / rev.tool_id / rev.revision_sha / "source.tar.gz"

    def _content_dir(self, rev: ToolRevision) -> Path:
        return self.quarantine_root / rev.tool_id / rev.revision_sha / "content"

    def _report_path(self, rev: ToolRevision) -> Path:
        """
        Unique per-invocation stored-report path.

        Each scan names a fresh destination file, so the stored evidence is
        only ever the report the scan that named it produced; earlier
        reports remain on disk as audit history instead of being
        overwritten as though newly produced.
        """
        return (
            self.quarantine_root / rev.tool_id / rev.revision_sha
            / f"scan-report-{uuid4().hex}.json"
        )

    def _invalidate_approval(
        self,
        rev: ToolRevision,
        *,
        new_status: IntakeStatus,
        event: str,
        reason: str,
    ) -> None:
        """Clear the approval binding, demote status, and audit the reason."""
        rev.clear_approval()
        rev.policy_decision = None
        registry.save_revision(rev, self._db_path)
        registry.set_tool_status(rev.tool_id, new_status, self._db_path)
        self._emit(event, {
            "tool_id": rev.tool_id,
            "revision_sha": rev.revision_sha,
            "status": new_status.value,
            "reason": reason,
        })

    def _check_quarantine_ignored(self) -> None:
        if not self._ignore_checker(self.quarantine_root):
            raise IntakeFetchError(
                f"Quarantine root {self.quarantine_root} is not git-ignored; "
                "refusing to write downloaded artifacts there."
            )

    def _check_total_cap(self, incoming_bytes: int) -> None:
        used = tree_size_bytes(self.quarantine_root)
        if used + incoming_bytes > self._total_cap:
            raise IntakeFetchError(
                f"Quarantine total cap exceeded "
                f"({used}+{incoming_bytes} > {self._total_cap} bytes)."
            )

    def _verify_content_tree(self, rev: ToolRevision) -> Path:
        """Recompute the tree hash; raise (and mark STALE) on mismatch."""
        content_dir = Path(rev.quarantine_path) if rev.quarantine_path else self._content_dir(rev)
        if not content_dir.is_dir():
            raise IntakeIntegrityError(
                f"Quarantined content missing for {rev.tool_id}@{rev.revision_sha}."
            )
        current = hash_tree(content_dir)
        if rev.tree_sha256 and current != rev.tree_sha256:
            self._invalidate_approval(
                rev,
                new_status=IntakeStatus.STALE,
                event="tool.intake.integrity_failed",
                reason="quarantined content tree hash changed (tamper/drift)",
            )
            raise IntakeIntegrityError(
                f"Content tree hash mismatch for {rev.tool_id}@{rev.revision_sha}: "
                "quarantined bytes changed after scan. Approval invalidated; "
                "rescan and re-approval required."
            )
        return content_dir

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    def discover(
        self,
        source_url: str,
        revision: str,
        *,
        kind: str = ToolKind.AGENT_SKILL.value,
        name: str = "",
        license_declared: str | None = None,
        capabilities: list[str] | None = None,
    ) -> IntakeSummary:
        """
        Validate and record source identity + pinned revision (offline).

        Phase 2a supports only ``agent_skill``; other kinds fail closed.
        """
        if kind != ToolKind.AGENT_SKILL.value:
            raise IntakeError(
                f"Kind {kind!r} is not supported in Phase 2a "
                f"(only {ToolKind.AGENT_SKILL.value!r})."
            )
        source: GitHubSource = parse_github_source(source_url, revision)
        tool_id = derive_tool_id(kind, source.canonical_url)

        existing = registry.get_tool(tool_id, self._db_path)
        if existing is not None and existing.status == IntakeStatus.REVOKED:
            # Revoked tools are immutable: rediscovery must not silently
            # un-revoke or rewrite name/license/capabilities/source. Any
            # future reactivation must be a separate auditable command.
            raise IntakeStateError(
                f"Tool {tool_id!r} is REVOKED (terminal kill-switch); "
                "rediscovery cannot reactivate or modify it. Reactivation "
                "requires a separate auditable command (not available in "
                "this release)."
            )
        now = datetime.now(timezone.utc)
        tool = ToolRecord(
            tool_id=tool_id,
            kind=ToolKind(kind),
            name=name.strip() or source.repo,
            source_url=source.canonical_url,
            license_declared=license_declared,
            license_source="declared" if license_declared else "unknown",
            capabilities=list(capabilities or []),
            status=existing.status if existing else IntakeStatus.DISCOVERED,
            install_path=existing.install_path if existing else None,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        registry.upsert_tool(tool, self._db_path)

        rev = registry.get_revision(tool_id, source.revision_sha, self._db_path)
        if rev is None:
            rev = ToolRevision(
                tool_id=tool_id,
                revision_sha=source.revision_sha,
                created_at=now,
                updated_at=now,
            )
            registry.insert_revision(rev, self._db_path)

        self._emit("tool.intake.discovered", {
            "tool_id": tool_id,
            "kind": kind,
            "source_url": source.canonical_url,
            "revision_sha": source.revision_sha,
            "license_recorded": bool(license_declared),
        })
        return IntakeSummary(tool=tool, revision=rev)

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    def fetch(self, tool_id: str, revision_sha: str) -> IntakeSummary:
        """
        Download the pinned revision via the injectable transport, vet and
        extract it into the git-ignored quarantine, and record hashes.

        Revisions are immutable: if recorded hashes no longer match disk (or
        new bytes differ from a previously recorded artifact), the approval
        is invalidated, the state becomes STALE, and the error is raised —
        nothing is silently overwritten.
        """
        tool, rev = self._require_tool_revision(tool_id, revision_sha)
        source = parse_github_source(tool.source_url, rev.revision_sha)

        # Already fetched: verify in place, never re-download silently.
        if rev.artifact_sha256:
            artifact = self._artifact_path(rev)
            if artifact.is_file():
                current = hash_file_sha256(artifact)
                if current != rev.artifact_sha256:
                    self._invalidate_approval(
                        rev,
                        new_status=IntakeStatus.STALE,
                        event="tool.intake.integrity_failed",
                        reason="quarantined archive bytes changed (tamper/drift)",
                    )
                    raise IntakeIntegrityError(
                        "Quarantined archive does not match its recorded hash. "
                        "Approval invalidated; rescan and re-approval required."
                    )
                if rev.tree_sha256:
                    self._verify_content_tree(rev)
                return IntakeSummary(tool=tool, revision=rev, detail={"already": "quarantined"})

        self._check_quarantine_ignored()
        try:
            payload = self._transport.fetch(
                source.archive_url,
                timeout_s=self._fetch_timeout_s,
                max_bytes=self._caps.max_archive_bytes,
            )
        except IntakeFetchError as exc:
            registry.set_tool_status(tool_id, IntakeStatus.DISCOVERY_FAILED, self._db_path)
            self._emit("tool.intake.fetch_failed", {
                "tool_id": tool_id,
                "revision_sha": revision_sha,
                "reason": str(exc),
            })
            raise

        if len(payload) > self._caps.max_archive_bytes:
            raise IntakeFetchError("Downloaded archive exceeds the archive-size cap.")
        new_hash = hashlib.sha256(payload).hexdigest()

        if rev.artifact_sha256 and new_hash != rev.artifact_sha256:
            self._invalidate_approval(
                rev,
                new_status=IntakeStatus.STALE,
                event="tool.intake.integrity_failed",
                reason="newly fetched bytes differ from previously recorded artifact",
            )
            raise IntakeIntegrityError(
                "Fetched bytes differ from the previously recorded artifact hash. "
                "Approval invalidated; nothing was overwritten."
            )

        self._check_total_cap(len(payload))

        tool_dir = self.quarantine_root / tool_id / rev.revision_sha
        tool_dir.mkdir(parents=True, exist_ok=True)
        artifact = tool_dir / "source.tar.gz"
        tmp = tool_dir / "source.tar.gz.part"
        try:
            tmp.write_bytes(payload)
            tmp.replace(artifact)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise IntakeFetchError(f"Cannot write quarantine artifact: {exc}") from exc

        # L6: extract into a fresh, OWNED temporary directory. Any
        # pre-existing content directory is never deleted or modified
        # because an archive might be invalid — only this extraction's own
        # temporary directory is cleaned on failure. The new tree is
        # published into place exclusively after extraction validates.
        content_dir = tool_dir / "content"
        tmp_content = tool_dir / f"content.partial-{uuid4().hex}"
        if tmp_content.exists():  # owned temp namespace (prior crashed run)
            shutil.rmtree(tmp_content, ignore_errors=True)
        try:
            safe_extract_tar_gz(artifact, tmp_content, self._caps)
        except (IntakeFetchError, OSError) as exc:
            shutil.rmtree(tmp_content, ignore_errors=True)
            registry.set_tool_status(tool_id, IntakeStatus.DISCOVERY_FAILED, self._db_path)
            self._emit("tool.intake.fetch_failed", {
                "tool_id": tool_id,
                "revision_sha": revision_sha,
                "reason": "archive extraction rejected (fail-closed)",
            })
            if isinstance(exc, IntakeFetchError):
                raise
            raise IntakeFetchError(f"Cannot extract archive: {exc}") from exc

        # Publish: move any prior content aside first (rename, not delete),
        # swap the validated tree into place, and roll the old content back
        # if publishing fails — no failure path destroys prior content.
        old_content = tool_dir / f"content.replaced-{uuid4().hex}"
        had_old = content_dir.exists()
        try:
            if had_old:
                content_dir.replace(old_content)
            tmp_content.replace(content_dir)
        except OSError as exc:
            if had_old and old_content.exists() and not content_dir.exists():
                try:
                    old_content.replace(content_dir)
                except OSError as rollback_exc:  # pragma: no cover
                    logger.error(
                        "rollback of prior quarantine content failed: %s",
                        rollback_exc,
                    )
            shutil.rmtree(tmp_content, ignore_errors=True)
            registry.set_tool_status(tool_id, IntakeStatus.DISCOVERY_FAILED, self._db_path)
            self._emit("tool.intake.fetch_failed", {
                "tool_id": tool_id,
                "revision_sha": revision_sha,
                "reason": "publishing extracted content failed (fail-closed)",
            })
            raise IntakeFetchError(
                f"Cannot publish extracted content into quarantine: {exc}"
            ) from exc
        if had_old:
            shutil.rmtree(old_content, ignore_errors=True)

        rev.artifact_sha256 = new_hash
        rev.tree_sha256 = hash_tree(content_dir)
        rev.quarantine_path = str(content_dir)
        rev.updated_at = datetime.now(timezone.utc)
        registry.save_revision(rev, self._db_path)
        registry.set_tool_status(tool_id, IntakeStatus.QUARANTINED, self._db_path)
        tool.status = IntakeStatus.QUARANTINED

        self._emit("tool.intake.fetch_completed", {
            "tool_id": tool_id,
            "revision_sha": rev.revision_sha,
            "artifact_sha256": rev.artifact_sha256,
            "tree_sha256": rev.tree_sha256,
        })
        return IntakeSummary(tool=tool, revision=rev)

    # ------------------------------------------------------------------
    # scan
    # ------------------------------------------------------------------

    def scan(self, tool_id: str, revision_sha: str) -> IntakeSummary:
        """
        Run the deterministic scanner over the quarantined content.

        Fails closed (SCAN_FAILED / SCAN_FAILED status + raised
        IntakeScanError) on: scanner unavailable, timeout, nonzero exit,
        missing/malformed/incomplete report. A completed scan with zero
        findings is CLEAN; a completed scan with findings is FINDINGS
        (a policy outcome, not a scan failure).
        """
        tool, rev = self._require_tool_revision(tool_id, revision_sha)
        if rev.artifact_sha256 is None:
            raise IntakeStateError(
                f"Revision {tool_id}@{revision_sha} is not quarantined yet (run fetch)."
            )
        content_dir = self._verify_content_tree(rev)

        # One unique stored-report destination per invocation: the file at
        # report_path is only ever written by THIS scan (run_scan atomically
        # publishes after validating a fresh report); earlier reports stay
        # on disk as audit history instead of being reused or overwritten as
        # though newly produced.
        report_path = self._report_path(rev)

        outcome: ScanOutcome = self._scanner.run_scan(
            content_dir, report_path, timeout_s=self._scan_timeout_s
        )

        # Note: scanner_identity is NOT touched here. A failed rescan must
        # preserve the previously recorded identity (and a revision that
        # never produced a successful scan must keep None) — the identity
        # is bound only below, from a completed fresh report.
        rev.updated_at = datetime.now(timezone.utc)

        if not outcome.completed:
            unavailable = "not available" in (outcome.reason or "")
            rev.scan_state = (
                ScanState.UNAVAILABLE if unavailable else ScanState.FAILED
            )
            # No report was produced by THIS invocation: retain the
            # historical report reference (the last evidence this store
            # actually produced — its path and hash still match the bytes
            # on disk) rather than pointing at bytes that were never made.
            # The FAILED/UNAVAILABLE scan state plus SCAN_FAILED status make
            # the revision unapprovable, so an earlier clean scan can never
            # authorize approval after a failed rescan.
            if rev.approval_bound:
                rev.clear_approval()
            registry.save_revision(rev, self._db_path)
            registry.set_tool_status(tool_id, IntakeStatus.SCAN_FAILED, self._db_path)
            self._emit("tool.intake.scan_failed", {
                "tool_id": tool_id,
                "revision_sha": revision_sha,
                "scan_state": rev.scan_state,
                "reason": outcome.reason,
            })
            raise IntakeScanError(
                f"Scan failed closed for {tool_id}@{revision_sha}: {outcome.reason}"
            )

        # report_path now holds the fresh, validated report of THIS run —
        # hash the stored evidence file (bytes identical to the validated
        # report run_scan just published).
        report_hash = hash_file_sha256(report_path)

        # Evidence identity is bound only on a COMPLETED scan, derived from
        # this report's own version metadata (never guessed, never a separate
        # invocation): a failed rescan leaves the previous identity intact.
        rev.scanner_identity = _scanner_identity(
            self._scanner, outcome.scanner_version
        )

        # A re-scan changes the report bytes: any prior approval bound to a
        # different report hash is no longer valid.
        if rev.approval_bound and rev.approval_report_sha256 != report_hash:
            rev.clear_approval()

        findings = (outcome.report or {}).get("findings") or []
        rev.scan_state = ScanState.FINDINGS if findings else ScanState.CLEAN
        rev.scan_report_path = str(report_path)
        rev.scan_report_sha256 = report_hash
        rev.risk_score = outcome.risk_score
        rev.severity = outcome.severity
        rev.recommendation = outcome.recommendation
        rev.unresolved_findings = outcome.unresolved_findings
        registry.save_revision(rev, self._db_path)
        registry.set_tool_status(tool_id, IntakeStatus.SCANNED, self._db_path)
        tool.status = IntakeStatus.SCANNED

        attach_evidence(
            EvidenceRecord(
                kind="scan_result",
                path=str(report_path),
                hash_sha256=report_hash or "",
                metadata={
                    "tool_id": tool_id,
                    "revision_sha": revision_sha,
                    "scanner": rev.scanner_identity,
                    "scan_state": rev.scan_state,
                },
            ),
            db_path=self._db_path,
        )
        self._emit("tool.intake.scan_completed", {
            "tool_id": tool_id,
            "revision_sha": revision_sha,
            "scan_state": rev.scan_state,
            "risk_score": rev.risk_score,
            "unresolved_findings": rev.unresolved_findings,
            "scan_report_sha256": report_hash,
        })
        return IntakeSummary(tool=tool, revision=rev)

    # ------------------------------------------------------------------
    # check
    # ------------------------------------------------------------------

    def check(self, tool_id: str, revision_sha: str) -> tuple[IntakeSummary, PolicyResult]:
        """
        Evaluate deterministic policy and, when everything passes, move the
        revision to PENDING_APPROVAL with a bound approval request.

        Only a completed CLEAN scan + permissive-eligible license + allowed
        capabilities + local score policy can reach PENDING_APPROVAL.
        Operator-declared (unverified) provenance is recorded as a policy
        reason but does not block here; approval enforces the attestation.
        """
        from factory.permissions.approvals import ApprovalManager

        tool, rev = self._require_tool_revision(tool_id, revision_sha)
        result = evaluate_policy(
            scan_state=rev.scan_state,
            risk_score=rev.risk_score,
            recommendation=rev.recommendation,
            license_declared=tool.license_declared,
            license_source=tool.license_source,
            requested_capabilities=tool.capabilities,
        )
        rev.policy_decision = result.decision
        rev.policy_reasons = result.as_dicts()
        rev.updated_at = datetime.now(timezone.utc)

        if rev.scan_state == ScanState.CLEAN and result.passed:
            registry.save_revision(rev, self._db_path)
            registry.set_tool_status(tool_id, IntakeStatus.PENDING_APPROVAL, self._db_path)
            # Create (or reuse) the revision-bound approval request.
            mgr = ApprovalManager(db_path=self._db_path)
            req = None
            if rev.approval_request_id:
                existing = mgr.get_request(rev.approval_request_id)
                if existing is not None and existing.status == ApprovalStatus.PENDING:
                    req = existing  # reuse the still-pending request
            if req is None:
                req = mgr.request_approval(
                    action=f"Install {tool.kind} '{tool.name}'",
                    target=f"{tool_id}@{revision_sha}",
                    reason="Tool intake: human approval required before install",
                    expected_effect=(
                        "Copies quarantined content into the isolated per-tool "
                        "install directory. Installed skills remain disabled and "
                        "are never loaded into any agent."
                    ),
                    risk_level=RiskLevel.HIGH,
                    consequence_if_denied="Tool stays quarantined and unusable.",
                    action_category="tool.install",
                )
            rev.approval_request_id = req.request_id
            registry.save_revision(rev, self._db_path)
            self._emit("tool.intake.pending_approval", {
                "tool_id": tool_id,
                "revision_sha": revision_sha,
                "approval_request_id": req.request_id,
                "policy_decision": result.decision,
            })
        else:
            # Distinct semantics for the blocked branch:
            # * NOT_RUN / FAILED / UNAVAILABLE → fail-closed scan evidence;
            #   SCAN_FAILED (never approvable).
            # * FINDINGS → the scan COMPLETED and found content; policy is
            #   what rejects. Status is REJECTED, and the label explicitly
            #   says "completed with findings" — never "scan failed".
            # * CLEAN + policy block → REJECTED (policy rejection).
            if rev.scan_state in (
                ScanState.NOT_RUN, ScanState.FAILED, ScanState.UNAVAILABLE
            ):
                new_status = IntakeStatus.SCAN_FAILED
            else:
                new_status = IntakeStatus.REJECTED
            registry.save_revision(rev, self._db_path)
            registry.set_tool_status(tool_id, new_status, self._db_path)
            self._emit("tool.intake.check_blocked", {
                "tool_id": tool_id,
                "revision_sha": revision_sha,
                "status": new_status.value,
                "scan_state": rev.scan_state,
                "policy_decision": result.decision,
                "reason_codes": [r["code"] for r in result.as_dicts()],
            })

        tool = registry.get_tool(tool_id, self._db_path) or tool
        rev = registry.get_revision(tool_id, revision_sha, self._db_path) or rev
        return IntakeSummary(tool=tool, revision=rev), result

    # ------------------------------------------------------------------
    # approve
    # ------------------------------------------------------------------

    def approve(
        self,
        tool_id: str,
        revision_sha: str,
        *,
        manual_review: bool = False,
        note: str = "",
        actor: str = "human-cli",
    ) -> IntakeSummary:
        """
        Record a human approval bound to the exact revision + hashes.

        * Requires a completed CLEAN scan — no flag bypasses scan evidence.
        * Policy-blocked revisions require ``manual_review=True`` with a
          non-empty ``note``; the override reason is persisted.
        * Operator-declared (unverified) license provenance additionally
          requires that same explicit attestation even when policy passes
          — declared provenance is never silently treated as verified.
        * Any hash drift between scan and approve invalidates and fails.
        """
        from factory.permissions.approvals import ApprovalManager

        tool, rev = self._require_tool_revision(tool_id, revision_sha)
        # Note: the REVOKED kill-switch was already enforced centrally in
        # _require_tool_revision above — a revoked tool never reaches here.

        if rev.scan_state != ScanState.CLEAN:
            raise IntakePolicyError(
                "Cannot approve without a completed clean scan "
                f"(scan_state={rev.scan_state!r}). No flag can make a "
                "failed or incomplete scan approvable."
            )
        current = registry.get_tool(tool_id, self._db_path)
        if current is None or current.status not in (
            IntakeStatus.PENDING_APPROVAL, IntakeStatus.REJECTED
        ):
            raise IntakeStateError(
                f"Approval requires pending/rejected state, currently "
                f"{current.status if current else 'unknown'!r} "
                "(run check first)."
            )

        # Integrity binding: on-disk bytes must match recorded hashes.
        artifact = self._artifact_path(rev)
        if not artifact.is_file() or hash_file_sha256(artifact) != rev.artifact_sha256:
            self._invalidate_approval(
                rev,
                new_status=IntakeStatus.STALE,
                event="tool.intake.integrity_failed",
                reason="archive missing or hash changed between scan and approve",
            )
            raise IntakeIntegrityError(
                "Artifact hash mismatch at approval time; approval invalidated."
            )
        if rev.scan_report_path:
            rp = Path(rev.scan_report_path)
            if not rp.is_file() or hash_file_sha256(rp) != rev.scan_report_sha256:
                self._invalidate_approval(
                    rev,
                    new_status=IntakeStatus.STALE,
                    event="tool.intake.integrity_failed",
                    reason="scan report missing or hash changed at approval time",
                )
                raise IntakeIntegrityError(
                    "Scan report hash mismatch at approval time; approval invalidated."
                )
        self._verify_content_tree(rev)

        # Stale-policy guard: the decision recorded by 'check' must still
        # match a deterministic evaluation of CURRENT metadata — exactly
        # the inputs evaluate_policy() receives: scan state/score/
        # recommendation, license, license provenance, and requested
        # capabilities. Rediscovery may have changed license or
        # capabilities after check — that recorded decision can no longer
        # authorize approval, and no manual-review override can bypass
        # this comparison (an override only covers a still-current blocked
        # decision, never a stale one). The exact revision, quarantined
        # content, and stored report bytes are bound separately by the
        # integrity checks above, not by this policy comparison.
        fresh = evaluate_policy(
            scan_state=rev.scan_state,
            risk_score=rev.risk_score,
            recommendation=rev.recommendation,
            license_declared=tool.license_declared,
            license_source=tool.license_source,
            requested_capabilities=tool.capabilities,
        )
        if fresh.decision != rev.policy_decision:
            raise IntakePolicyError(
                "Policy inputs changed since check (recorded decision "
                f"{rev.policy_decision!r} no longer matches current metadata "
                f"{fresh.decision!r}); the recorded decision cannot authorize "
                "approval. Re-run 'factory intake check' to re-evaluate "
                "license/capabilities/scan evidence before approving."
            )

        # Policy / provenance gate (Finding 2):
        # * A blocked deterministic policy decision needs a manual override.
        # * An operator-declared (unverified) license needs an explicit
        #   attestation EVEN when policy passes — 'declared' provenance is
        #   never silently treated as verified, so the permissive path
        #   cannot complete without --manual-review --note.
        policy_blocked = rev.policy_decision != "pass"
        provenance_unverified = bool(tool.license_declared) and (
            tool.license_source != "verified"
        )
        if policy_blocked or provenance_unverified:
            if not manual_review or not note.strip():
                if policy_blocked:
                    raise IntakePolicyError(
                        "Policy decision is blocked: "
                        + "; ".join(
                            f"{r['code']}: {r['detail']}"
                            for r in rev.policy_reasons
                        )
                        + ". Resolve the policy issue, or pass "
                        "--manual-review with --note for an explicitly "
                        "recorded human override."
                    )
                raise IntakePolicyError(
                    f"License {tool.license_declared!r} is operator-declared "
                    f"(source={tool.license_source!r}), NOT independently "
                    "verified; approval requires an explicit attestation — "
                    "pass --manual-review with --note recording your license "
                    "review."
                )
            additions: list[dict[str, str]] = []
            if policy_blocked:
                additions.append({
                    "code": "MANUAL_REVIEW_OVERRIDE",
                    "detail": (
                        f"Explicit manual review override by {actor}: "
                        f"{note.strip()}"
                    ),
                })
            if provenance_unverified:
                additions.append({
                    "code": "LICENSE_PROVENANCE_ATTESTED",
                    "detail": (
                        f"Operator attestation by {actor}: license "
                        f"{tool.license_declared!r} has provenance "
                        f"source={tool.license_source!r} (NOT independently "
                        f"verified), attested via --manual-review --note: "
                        f"{note.strip()}"
                    ),
                })
            rev.policy_reasons = list(rev.policy_reasons) + additions

        mgr = ApprovalManager(db_path=self._db_path)
        req_id = rev.approval_request_id
        if req_id:
            existing = mgr.get_request(req_id)
            if existing is not None and existing.status != ApprovalStatus.PENDING:
                req_id = None  # denied/decided: create a fresh request
        if req_id is None:
            req = mgr.request_approval(
                action=f"Install {tool.kind} '{tool.name}'",
                target=f"{tool_id}@{revision_sha}",
                reason="Tool intake: human approval required before install",
                expected_effect="Copies quarantined content into the isolated install dir.",
                risk_level=RiskLevel.HIGH,
                consequence_if_denied="Tool stays quarantined and unusable.",
                action_category="tool.install",
            )
            req_id = req.request_id
        mgr.record_decision(ApprovalDecision(
            request_id=req_id,
            status=ApprovalStatus.APPROVED,
            decided_by=actor,
            decision_note=note.strip() or "approved via intake CLI",
            grants_scope_inheritance=False,
        ))

        now = datetime.now(timezone.utc)
        rev.approval_request_id = req_id
        rev.approved_by = actor
        rev.approved_at = now
        rev.approval_artifact_sha256 = rev.artifact_sha256
        rev.approval_report_sha256 = rev.scan_report_sha256
        rev.updated_at = now
        registry.save_revision(rev, self._db_path)
        registry.set_tool_status(tool_id, IntakeStatus.APPROVED, self._db_path)

        self._emit("tool.intake.approved", {
            "tool_id": tool_id,
            "kind": tool.kind,
            "revision_sha": revision_sha,
            "approval_request_id": req_id,
            "approved_by": actor,
            "artifact_sha256": rev.artifact_sha256,
            "scan_report_sha256": rev.scan_report_sha256,
            "manual_review": bool(manual_review),
        })
        tool = registry.get_tool(tool_id, self._db_path) or tool
        return IntakeSummary(tool=tool, revision=rev)

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    def install(self, tool_id: str, revision_sha: str) -> IntakeSummary:
        """
        Copy approved quarantined content into the isolated per-tool
        install directory. Content is never executed, and installed skills
        are never enabled or loaded into any agent (Phase 2a has no loader).

        Fail-closed pre-write check: the install root must be git-ignored
        (verified via the injectable checker) before any bytes are written.
        """
        tool, rev = self._require_tool_revision(tool_id, revision_sha)
        # Note: the REVOKED kill-switch was already enforced centrally in
        # _require_tool_revision above — a revoked tool never reaches here.
        if tool.status != IntakeStatus.APPROVED or not rev.approval_bound:
            raise IntakeStateError(
                f"Install requires an approved revision (status="
                f"{tool.status!r}); run check + approve first."
            )
        if rev.scan_state != ScanState.CLEAN:
            raise IntakePolicyError(
                "Install requires a completed clean scan; current scan state "
                f"is {rev.scan_state!r}."
            )

        # Re-verify the approval binding against current on-disk bytes.
        artifact = self._artifact_path(rev)
        current_artifact = (
            hashlib.sha256(artifact.read_bytes()).hexdigest()
            if artifact.is_file() else None
        )
        if current_artifact != rev.approval_artifact_sha256:
            self._invalidate_approval(
                rev,
                new_status=IntakeStatus.STALE,
                event="tool.intake.integrity_failed",
                reason="artifact hash differs from approval binding at install",
            )
            raise IntakeIntegrityError(
                "Artifact no longer matches the approved hash; approval "
                "invalidated. Rescan and re-approval required."
            )
        if rev.scan_report_path:
            rp = Path(rev.scan_report_path)
            report_now = hashlib.sha256(rp.read_bytes()).hexdigest() if rp.is_file() else None
            if report_now != rev.approval_report_sha256:
                self._invalidate_approval(
                    rev,
                    new_status=IntakeStatus.STALE,
                    event="tool.intake.integrity_failed",
                    reason="scan report hash differs from approval binding at install",
                )
                raise IntakeIntegrityError(
                    "Scan report no longer matches the approved hash; approval "
                    "invalidated. Rescan and re-approval required."
                )

        content_dir = self._verify_content_tree(rev)

        dest = self.install_root / tool_id / revision_sha
        if dest.exists():
            if hash_tree(dest) == rev.tree_sha256:
                # Idempotent: identical bytes already installed.
                pass
            else:
                raise IntakeIntegrityError(
                    f"Install directory {dest} exists with different content; "
                    "refusing to overwrite."
                )
        else:
            # Pre-write safety check (Finding 4): the destination must be
            # git-ignored before ANY installed bytes land there — same
            # fail-closed posture as the quarantine root before fetch.
            if not self._ignore_checker(self.install_root):
                self._emit("tool.intake.install_blocked", {
                    "tool_id": tool_id,
                    "revision_sha": revision_sha,
                    "install_root": str(self.install_root),
                    "reason": "install root is not git-ignored (fail-closed)",
                })
                raise IntakeInstallError(
                    f"Install root {self.install_root} is not git-ignored; "
                    "refusing to write installed content. Add the install "
                    "root to .gitignore (e.g. 'knowledge/tools/'), then "
                    "retry. Nothing was written."
                )
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # L7: root-cause OS failures (permissions, read-only volume,
                # path too long) surface as the typed intake error with the
                # cause preserved — clean nonzero CLI behavior, no traceback.
                raise IntakeInstallError(
                    f"Cannot create install directory {dest.parent}: {exc}"
                ) from exc
            try:
                shutil.copytree(
                    content_dir, dest,
                    copy_function=shutil.copyfile,  # bytes only; no mode/owner/link copy
                )
                copied_hash = hash_tree(dest)
                if copied_hash != rev.tree_sha256:
                    shutil.rmtree(dest, ignore_errors=True)
                    raise IntakeIntegrityError(
                        "Installed content hash mismatch; install directory "
                        "removed and install aborted."
                    )
            except IntakeIntegrityError:
                raise
            except OSError as exc:
                shutil.rmtree(dest, ignore_errors=True)
                raise IntakeFetchError(f"Install copy failed: {exc}") from exc

        now = datetime.now(timezone.utc)
        rev.installed_at = now
        rev.updated_at = now
        registry.save_revision(rev, self._db_path)
        registry.set_tool_status(
            tool_id, IntakeStatus.INSTALLED, self._db_path, install_path=str(dest)
        )
        self._emit("tool.intake.installed", {
            "tool_id": tool_id,
            "revision_sha": revision_sha,
            "install_path": str(dest),
            "tree_sha256": rev.tree_sha256,
            "enabled": False,   # installed skills stay disabled/unloaded
        })
        tool = registry.get_tool(tool_id, self._db_path) or tool
        return IntakeSummary(tool=tool, revision=rev, detail={"install_path": str(dest)})

    # ------------------------------------------------------------------
    # disable / status
    # ------------------------------------------------------------------

    def disable(self, tool_id: str, *, actor: str = "human-cli") -> ToolRecord:
        """Revoke registry eligibility. Evidence and rows are kept."""
        tool = registry.get_tool(tool_id, self._db_path)
        if tool is None:
            raise IntakeStateError(f"Unknown tool id: {tool_id!r}.")
        previous = tool.status
        registry.set_tool_status(tool_id, IntakeStatus.REVOKED, self._db_path)
        self._emit("tool.intake.revoked", {
            "tool_id": tool_id,
            "previous_status": previous,
            "actor": actor,
        })
        return registry.get_tool(tool_id, self._db_path) or tool

    def status(self, tool_id: str | None = None) -> list[dict[str, Any]]:
        """Human-readable status rows distinguishing every lifecycle state."""
        tools = (
            [registry.get_tool(tool_id, self._db_path)]
            if tool_id else registry.list_tools(self._db_path)
        )
        rows: list[dict[str, Any]] = []
        for tool in tools:
            if tool is None:
                raise IntakeStateError(f"Unknown tool id: {tool_id!r}.")
            revs = registry.list_revisions(tool.tool_id, self._db_path)
            latest = revs[-1] if revs else None
            rows.append({
                "tool_id": tool.tool_id,
                "kind": tool.kind,
                "name": tool.name,
                "source_url": tool.source_url,
                "status": tool.status,
                "label": self._label(tool.status, latest),
                "license_declared": tool.license_declared,
                "license_source": tool.license_source,
                "capabilities": tool.capabilities,
                "install_path": tool.install_path,
                "revision": None if latest is None else {
                    "revision_sha": latest.revision_sha,
                    "scan_state": latest.scan_state,
                    "risk_score": latest.risk_score,
                    "severity": latest.severity,
                    "recommendation": latest.recommendation,
                    "unresolved_findings": latest.unresolved_findings,
                    "policy_decision": latest.policy_decision,
                    "policy_reasons": latest.policy_reasons,
                    "artifact_sha256": latest.artifact_sha256,
                    "scan_report_sha256": latest.scan_report_sha256,
                    "approved_by": latest.approved_by,
                    "approved_at": latest.approved_at.isoformat() if latest.approved_at else None,
                    "approval_request_id": latest.approval_request_id,
                    "installed_at": latest.installed_at.isoformat() if latest.installed_at else None,
                },
            })
        return rows

    @staticmethod
    def _label(status: IntakeStatus | str, rev: ToolRevision | None) -> str:
        """Explicit human labels for every state (used by the CLI)."""
        status = IntakeStatus(status)  # normalize str (use_enum_values) → enum
        if rev is None:
            return status.value
        if status == IntakeStatus.SCAN_FAILED:
            if rev.scan_state == ScanState.UNAVAILABLE:
                return "scan failed: scanner unavailable (not approvable)"
            if rev.scan_state == ScanState.NOT_RUN:
                return "scan not run (not approvable)"
            return "scan failed/incomplete (not approvable)"
        if status == IntakeStatus.SCANNED:
            if rev.scan_state == ScanState.FINDINGS:
                return "scan complete: findings present (policy blocks auto-approval)"
            return "scan complete: clean (0 findings)"
        if status == IntakeStatus.REJECTED:
            if rev.scan_state == ScanState.FINDINGS:
                # The scan itself SUCCEEDED; policy is what rejected.
                return (
                    "scan completed with findings; rejected by deterministic "
                    "policy (not a scanner failure)"
                )
            return "rejected by deterministic policy"
        return {
            IntakeStatus.DISCOVERED: "discovered (not fetched)",
            IntakeStatus.DISCOVERY_FAILED: "fetch/discovery failed",
            IntakeStatus.QUARANTINED: "quarantined (awaiting scan)",
            IntakeStatus.PENDING_APPROVAL: "pending human approval",
            IntakeStatus.APPROVED: "approved (hash-bound; not installed)",
            IntakeStatus.INSTALLED: "installed (disabled/unloaded)",
            IntakeStatus.REJECTED: "rejected by deterministic policy",
            IntakeStatus.STALE: "stale (content drift; approval invalidated)",
            IntakeStatus.REVOKED: "revoked (evidence retained)",
        }.get(status, status.value)
