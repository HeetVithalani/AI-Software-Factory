"""
factory.validation.final_audit
===============================
Deterministic final audit — the release-eligibility gate.

Everything here is derived from persisted facts (approved plan, task rows,
evidence rows, workspace file hashes). No LLM output participates in any
check. Two distinct outcomes are reported:

* ``passed`` — every check that COULD be executed passed. This decides
  whether the auditor task succeeds.
* ``release_eligible`` — ``passed`` AND no verification was skipped. A run
  whose state could not be fully verified (legacy plan file, unknown
  workspace, unverifiable evidence paths) is never claimed release
  eligible, even though its task may succeed.

Evidence integrity uses the *latest* recorded hash per path: evidence is a
history, and only the most recent recorded fact about a file must still be
true. Stale facts from earlier repair cycles are history, not failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.audit.evidence import hash_file
from factory.database import get_connection

STATUS_COMPLETED = "completed"


@dataclass
class AuditCheck:
    """One deterministic check with an explicit pass/fail and detail."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class FinalAuditResult:
    """Aggregate deterministic audit outcome."""

    checks: list[AuditCheck] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def release_eligible(self) -> bool:
        return self.passed and not self.limitations

    def failed_checks(self) -> list[AuditCheck]:
        return [check for check in self.checks if not check.passed]

    def failure_summary(self) -> str:
        return "; ".join(
            f"{check.name}: {check.detail}" for check in self.failed_checks()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
            "limitations": list(self.limitations),
            "passed": self.passed,
            "release_eligible": self.release_eligible,
        }


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _load_metadata(row: Any) -> dict[str, Any]:
    import json

    raw = _row_get(row, "metadata", "{}")
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def run_final_audit(
    *,
    project_id: str,
    task_rows: list[Any] | None = None,
    exclude_task_id: str = "",
    plan: dict[str, Any] | None = None,
    plan_id: str = "",
    plan_hash: str = "",
    pipeline: str = "",
    workspace_root: Path | None = None,
    db_path: Path | None = None,
) -> FinalAuditResult:
    """Run every executable deterministic check and report the outcome."""
    result = FinalAuditResult()
    task_rows = list(task_rows or [])
    if workspace_root is not None:
        workspace_root = Path(workspace_root).resolve()

    # ------------------------------------------------------------------
    # 1. Approved plan identity and integrity
    # ------------------------------------------------------------------
    if plan_id:
        try:
            from factory.planning.plan_store import (
                PlanStore,
                canonical_plan_hash,
            )
            version = PlanStore(db_path).load(plan_id)
        except Exception as exc:  # noqa: BLE001 — audit must not crash
            version = None
            result.checks.append(AuditCheck(
                "plan_approval", False,
                f"plan store unavailable: {exc}",
            ))
        else:
            if version is None:
                result.checks.append(AuditCheck(
                    "plan_approval", False,
                    f"plan {plan_id!r} not found in the plan store",
                ))
            elif version.status != "approved":
                result.checks.append(AuditCheck(
                    "plan_approval", False,
                    f"plan status is {version.status!r}, not approved",
                ))
            elif canonical_plan_hash(version.plan) != version.plan_hash:
                result.checks.append(AuditCheck(
                    "plan_approval", False,
                    "stored plan content does not match its recorded hash",
                ))
            elif plan_hash and version.plan_hash != plan_hash:
                result.checks.append(AuditCheck(
                    "plan_approval", False,
                    "the plan hash changed since the run started",
                ))
            else:
                result.checks.append(AuditCheck(
                    "plan_approval", True,
                    f"approved plan {plan_id} (hash {version.plan_hash[:12]})",
                ))
    else:
        result.limitations.append(
            "no persisted plan id: approval and plan integrity could not be "
            "verified for this run"
        )

    # ------------------------------------------------------------------
    # 2. Release pipeline
    # ------------------------------------------------------------------
    if pipeline == "standard":
        release_eligible_pipeline = True
        detail = "standard (release-eligible) pipeline"
        if plan is not None and plan.get("release_eligible") is False:
            release_eligible_pipeline = False
            detail = "plan payload sets release_eligible=false"
        result.checks.append(AuditCheck(
            "release_pipeline", release_eligible_pipeline, detail,
        ))
    elif pipeline == "smoke":
        result.checks.append(AuditCheck(
            "release_pipeline", False,
            "smoke pipeline runs are never release eligible",
        ))
    else:
        result.limitations.append(
            "pipeline not recorded; release eligibility cannot be claimed"
        )

    # ------------------------------------------------------------------
    # 3. Completion, uncertainty, and evidence presence
    # ------------------------------------------------------------------
    if task_rows:
        scoped = [
            row for row in task_rows
            if str(_row_get(row, "task_id", "")) != exclude_task_id
        ]
        incomplete = [
            f"{_row_get(row, 'task_id', '?')}={_row_get(row, 'status', '?')}"
            for row in scoped
            if str(_row_get(row, "status", "")) != STATUS_COMPLETED
        ]
        result.checks.append(AuditCheck(
            "tasks_completed", not incomplete,
            "all tasks completed"
            if not incomplete
            else "not completed: " + ", ".join(incomplete),
        ))

        uncertain = [
            str(_row_get(row, "task_id", "?"))
            for row in scoped
            if _load_metadata(row).get("recovery_required")
        ]
        result.checks.append(AuditCheck(
            "no_uncertain_state", not uncertain,
            "no uncertain (recovery-required) tasks"
            if not uncertain
            else "recovery-required: " + ", ".join(uncertain),
        ))

        evidence_rows = _load_evidence_rows(project_id, db_path)
        counts: dict[str, int] = {}
        for row in evidence_rows:
            tid = str(_row_get(row, "task_id", ""))
            counts[tid] = counts.get(tid, 0) + 1
        missing = [
            str(_row_get(row, "task_id", "?"))
            for row in scoped
            if counts.get(str(_row_get(row, "task_id", "")), 0) == 0
        ]
        result.checks.append(AuditCheck(
            "evidence_present", not missing,
            "every completed task recorded evidence"
            if not missing
            else "no evidence recorded for: " + ", ".join(missing),
        ))

        # ------------------------------------------------------------------
        # 4. Evidence file integrity (latest recorded hash per path)
        # ------------------------------------------------------------------
        if workspace_root is None:
            result.limitations.append(
                "workspace root unknown; evidence file integrity could not "
                "be verified"
            )
        elif evidence_rows:
            latest: dict[str, str] = {}
            for row in sorted(
                evidence_rows,
                key=lambda r: (
                    str(_row_get(r, "recorded_at", "")),
                    int(_row_get(r, "rid", 0) or 0),
                ),
            ):
                latest[str(_row_get(row, "path", ""))] = str(
                    _row_get(row, "hash_sha256", "")
                )

            mismatches: list[str] = []
            outside = 0
            for path_str, expected in latest.items():
                if not path_str:
                    continue
                candidate = Path(path_str)
                if not candidate.is_absolute():
                    candidate = workspace_root / candidate
                try:
                    resolved = candidate.resolve()
                    resolved.relative_to(workspace_root)
                except (OSError, ValueError):
                    outside += 1
                    continue
                if not resolved.is_file():
                    mismatches.append(f"{path_str} (missing)")
                    continue
                actual = hash_file(resolved)
                if actual != expected:
                    mismatches.append(f"{path_str} (content changed)")
            result.checks.append(AuditCheck(
                "evidence_integrity", not mismatches,
                "all latest evidence hashes match the workspace"
                if not mismatches
                else "integrity failures: " + ", ".join(mismatches[:8]),
            ))
            if outside:
                result.limitations.append(
                    f"{outside} evidence path(s) outside the workspace were "
                    "not verified"
                )
    else:
        result.limitations.append(
            "no persisted task rows for this project; completion and "
            "evidence checks were not available for this run"
        )

    return result


def _load_evidence_rows(project_id: str, db_path: Path | None) -> list[Any]:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT rowid AS rid, * FROM evidence WHERE project_id = ?",
            (project_id,),
        ).fetchall()
