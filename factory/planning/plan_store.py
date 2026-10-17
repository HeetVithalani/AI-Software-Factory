"""
factory.planning.plan_store
============================
Versioned plan persistence and hash-bound human approval.

Rules enforced here
-------------------
* Plans are immutable versions. Creating a new version never mutates an
  older one and never inherits its approval.
* Every plan has a canonical SHA-256 over its content. A human decision is
  stored with the exact hash the human reviewed; changing a single byte
  invalidates the decision.
* Validation is deterministic and fail-closed: a plan without a valid DAG,
  requirement traceability, acceptance criteria, or the mandatory security /
  QA / audit stages (for release-eligible pipelines) is refused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from factory.cli.plan_loader import BUILTIN_AGENT_TYPES, dag_from_plan_dict
from factory.database import get_connection
from factory.errors import FactoryError

logger = logging.getLogger(__name__)

PLAN_STATUSES = ("pending", "approved", "rejected", "superseded")
PIPELINES = ("standard", "smoke")
PROFILES = ("fast", "balanced", "hardened")

#: Capability names a plan may declare. These mirror the tools the built-in
#: agents already construct; declaring a capability never grants it — the
#: permission broker remains the enforcement point.
ALLOWED_TOOL_CAPABILITIES = frozenset(
    {"filesystem", "shell", "git", "testing", "security", "browser"}
)


class PlanNotFoundError(FactoryError):
    code = "PLAN_NOT_FOUND"


class PlanValidationError(FactoryError):
    code = "PLAN_VALIDATION_ERROR"


class PlanNotApprovedError(FactoryError):
    code = "PLAN_NOT_APPROVED"


class PlanApprovalError(FactoryError):
    code = "PLAN_APPROVAL_ERROR"


class PlanVersion(BaseModel):
    """An immutable, persisted plan version."""

    plan_id: str
    project_id: str
    version: int
    plan_hash: str
    plan: dict[str, Any]
    brief_id: str = ""
    research_id: str = ""
    status: str = "pending"
    pipeline: str = "standard"
    profile: str = "balanced"
    created_by: str = "deterministic-planner"
    parent_plan_id: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class PlanApproval(BaseModel):
    """A single human decision, bound to the exact plan hash reviewed."""

    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plan_id: str
    plan_hash: str
    decision: str
    actor: str
    note: str = ""
    decided_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


def canonical_plan_hash(plan: dict[str, Any]) -> str:
    """Canonical SHA-256 over every consequential plan field."""
    payload = json.dumps(
        plan,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_plan(plan: dict[str, Any]) -> list[str]:
    """
    Return a list of deterministic validation errors (empty == valid).

    This does not trust the plan author. It re-checks DAG structure and
    requirement traceability independently of the generator.
    """
    errors: list[str] = []

    if not isinstance(plan, dict):
        return ["plan must be an object"]
    if not _non_empty_str(plan.get("project_id")):
        errors.append("plan.project_id is required")

    pipeline = str(plan.get("pipeline", "standard"))
    if pipeline not in PIPELINES:
        errors.append(
            f"plan.pipeline must be one of {', '.join(PIPELINES)}"
        )
    profile = str(plan.get("profile", "balanced"))
    if profile not in PROFILES:
        errors.append(f"plan.profile must be one of {', '.join(PROFILES)}")

    if pipeline == "smoke":
        if plan.get("release_eligible") is not False:
            errors.append(
                "smoke pipelines must set release_eligible=false; a smoke "
                "plan can never be release eligible"
            )

    requirements = plan.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        errors.append("plan.requirements must be a non-empty list")
        requirements = []
    requirement_ids: set[str] = set()
    for index, req in enumerate(requirements):
        if not isinstance(req, dict):
            errors.append(f"requirements[{index}] must be an object")
            continue
        req_id = str(req.get("requirement_id", "")).strip()
        if not req_id:
            errors.append(f"requirements[{index}].requirement_id is required")
            continue
        if req_id in requirement_ids:
            errors.append(f"duplicate requirement_id {req_id!r}")
        requirement_ids.add(req_id)
        if not _non_empty_str(req.get("text")):
            errors.append(f"requirement {req_id!r} has no text")
        kind = str(req.get("kind", "functional"))
        if kind not in ("functional", "non_functional"):
            errors.append(
                f"requirement {req_id!r} has invalid kind {kind!r}"
            )

    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("plan.tasks must be a non-empty list")
        tasks = []

    task_ids: set[str] = set()
    agent_types: set[str] = set()
    covered: set[str] = set()

    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"tasks[{index}] must be an object")
            continue
        task_id = str(task.get("task_id", "")).strip()
        if not task_id:
            errors.append(f"tasks[{index}].task_id is required")
            continue
        if task_id in task_ids:
            errors.append(f"duplicate task_id {task_id!r}")
        task_ids.add(task_id)

        agent_type = str(task.get("agent_type", "")).strip()
        if agent_type not in BUILTIN_AGENT_TYPES:
            errors.append(
                f"task {task_id!r} has unknown agent_type "
                f"{agent_type!r}"
            )
        agent_types.add(agent_type)

        metadata = task.get("metadata")
        if not isinstance(metadata, dict):
            errors.append(f"task {task_id!r}.metadata must be an object")
            metadata = {}

        description = task.get("description") or metadata.get("description")
        if not _non_empty_str(description):
            errors.append(f"task {task_id!r} has no description")

        acceptance = task.get("acceptance_criteria") or metadata.get(
            "acceptance_criteria"
        )
        if not isinstance(acceptance, list) or not acceptance:
            errors.append(
                f"task {task_id!r} must declare non-empty acceptance_criteria"
            )

        req_ids = task.get("requirement_ids") or metadata.get(
            "requirement_ids"
        )
        if not isinstance(req_ids, list) or not req_ids:
            errors.append(
                f"task {task_id!r} must declare requirement_ids for "
                "traceability"
            )
        else:
            for req_id in req_ids:
                req_id = str(req_id)
                if req_id not in requirement_ids:
                    errors.append(
                        f"task {task_id!r} references unknown requirement "
                        f"{req_id!r}"
                    )
                else:
                    covered.add(req_id)

        tools = metadata.get("required_tools", [])
        if not isinstance(tools, list):
            errors.append(
                f"task {task_id!r}.metadata.required_tools must be a list"
            )
        else:
            for tool in tools:
                if str(tool) not in ALLOWED_TOOL_CAPABILITIES:
                    errors.append(
                        f"task {task_id!r} declares unknown tool capability "
                        f"{tool!r}"
                    )

        if agent_type == "qa":
            checks = metadata.get("required_checks")
            if not isinstance(checks, list) or not checks:
                errors.append(
                    f"QA task {task_id!r} must declare required_checks"
                )

    # Requirement coverage: every supplied requirement must be owned.
    for req_id in sorted(requirement_ids - covered):
        errors.append(f"requirement {req_id!r} is not covered by any task")

    # Mandatory pipeline stages for anything claiming release eligibility.
    if pipeline == "standard":
        for required in ("builder", "qa", "security", "auditor"):
            if required not in agent_types:
                errors.append(
                    f"standard plan is missing required '{required}' task"
                )

    if tasks:
        try:
            dag = dag_from_plan_dict(plan, source="<validation>")
            dag.validate()
        except Exception as exc:
            errors.append(f"DAG validation failed: {exc}")

    return errors


def require_valid_plan(plan: dict[str, Any]) -> None:
    errors = validate_plan(plan)
    if errors:
        raise PlanValidationError(
            "Plan validation failed:\n- " + "\n- ".join(errors)
        )


class PlanStore:
    """Persistence for plan versions and hash-bound decisions."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Creation / loading
    # ------------------------------------------------------------------

    def create(
        self,
        project_id: str,
        plan: dict[str, Any],
        *,
        brief_id: str = "",
        research_id: str = "",
        created_by: str = "deterministic-planner",
        parent_plan_id: str | None = None,
    ) -> PlanVersion:
        payload = json.loads(json.dumps(plan, default=str))
        payload["project_id"] = project_id
        payload.setdefault("plan_id", str(uuid.uuid4()))
        plan_id = str(payload["plan_id"])
        require_valid_plan(payload)
        plan_hash = canonical_plan_hash(payload)
        pipeline = str(payload.get("pipeline", "standard"))
        profile = str(payload.get("profile", "balanced"))
        now = datetime.now(timezone.utc).isoformat()

        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM plans "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            version = int(row["v"]) + 1
            # A new version always requires a fresh decision. Older
            # undecided versions are explicitly superseded.
            conn.execute(
                "UPDATE plans SET status = 'superseded', updated_at = ? "
                "WHERE project_id = ? AND status = 'pending'",
                (now, project_id),
            )
            conn.execute(
                """
                INSERT INTO plans (
                    plan_id, project_id, version, plan_hash, plan_json,
                    brief_id, research_id, status, pipeline, profile,
                    created_by, parent_plan_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    project_id,
                    version,
                    plan_hash,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    brief_id,
                    research_id,
                    pipeline,
                    profile,
                    created_by,
                    parent_plan_id,
                    now,
                    now,
                ),
            )
            conn.commit()

        logger.info(
            "Created plan %s v%d for project %s (hash=%s).",
            plan_id, version, project_id, plan_hash[:12],
        )
        return PlanVersion(
            plan_id=plan_id,
            project_id=project_id,
            version=version,
            plan_hash=plan_hash,
            plan=payload,
            brief_id=brief_id,
            research_id=research_id,
            status="pending",
            pipeline=pipeline,
            profile=profile,
            created_by=created_by,
            parent_plan_id=parent_plan_id,
            created_at=datetime.fromisoformat(now),
        )

    def load(self, plan_id: str) -> PlanVersion | None:
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return self._row_to_plan(row) if row is not None else None

    def latest(
        self, project_id: str, *, status: str | None = None
    ) -> PlanVersion | None:
        sql = "SELECT * FROM plans WHERE project_id = ?"
        params: list[Any] = [project_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY version DESC LIMIT 1"
        with get_connection(self.db_path) as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return self._row_to_plan(row) if row is not None else None

    def latest_approved(self, project_id: str) -> PlanVersion | None:
        return self.latest(project_id, status="approved")

    def list(
        self, project_id: str | None = None, *, limit: int = 50
    ) -> list[PlanVersion]:
        sql = "SELECT * FROM plans"
        params: tuple[Any, ...] = ()
        if project_id:
            sql += " WHERE project_id = ?"
            params = (project_id,)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with get_connection(self.db_path) as conn:
            rows = conn.execute(sql, (*params, limit)).fetchall()
        return [self._row_to_plan(row) for row in rows]

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def latest_decision(self, plan_id: str) -> PlanApproval | None:
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM plan_approvals WHERE plan_id = ? "
                "ORDER BY decided_at DESC, rowid DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        return PlanApproval(
            approval_id=row["approval_id"],
            plan_id=row["plan_id"],
            plan_hash=row["plan_hash"],
            decision=row["decision"],
            actor=row["actor"],
            note=row["note"],
            decided_at=datetime.fromisoformat(row["decided_at"]),
        )

    def decide(
        self,
        plan_id: str,
        decision: str,
        *,
        actor: str,
        expected_hash: str,
        note: str = "",
    ) -> PlanApproval:
        if decision not in ("approved", "rejected"):
            raise PlanApprovalError("decision must be 'approved' or 'rejected'")
        if not _non_empty_str(actor):
            raise PlanApprovalError("a human actor is required for decisions")
        if not _non_empty_str(expected_hash):
            raise PlanApprovalError(
                "expected_hash is required: decisions bind to exact content"
            )

        plan = self.load(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"plan {plan_id!r} does not exist")
        actual_hash = canonical_plan_hash(plan.plan)
        if actual_hash != plan.plan_hash:
            raise PlanApprovalError(
                "stored plan content does not match its recorded hash; "
                "refusing to record a decision on a tampered plan"
            )
        if expected_hash.strip() != actual_hash:
            raise PlanApprovalError(
                "the reviewed hash does not match the stored plan; "
                "show the plan again and approve the exact current hash"
            )

        approval = PlanApproval(
            plan_id=plan_id,
            plan_hash=actual_hash,
            decision=decision,
            actor=actor.strip(),
            note=note,
        )
        now = approval.decided_at.isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO plan_approvals (
                    approval_id, plan_id, plan_hash, decision,
                    actor, note, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.plan_id,
                    approval.plan_hash,
                    approval.decision,
                    approval.actor,
                    approval.note,
                    now,
                ),
            )
            conn.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE plan_id = ?",
                (decision, now, plan_id),
            )
            if decision == "approved":
                conn.execute(
                    "UPDATE plans SET status = 'superseded', updated_at = ? "
                    "WHERE project_id = ? AND plan_id != ? "
                    "AND status = 'pending'",
                    (now, plan.project_id, plan_id),
                )
            conn.commit()

        logger.info(
            "Plan %s decision=%s by %s (hash=%s).",
            plan_id, decision, approval.actor, actual_hash[:12],
        )
        return approval

    def require_approved(self, plan_id: str) -> PlanVersion:
        """Return an approved plan only if its content and decision match."""
        plan = self.load(plan_id)
        if plan is None:
            raise PlanNotFoundError(f"plan {plan_id!r} does not exist")
        actual_hash = canonical_plan_hash(plan.plan)
        if actual_hash != plan.plan_hash:
            raise PlanNotApprovedError(
                "stored plan content does not match its recorded hash; "
                "execution refused"
            )
        if plan.status != "approved":
            raise PlanNotApprovedError(
                f"plan {plan_id!r} is {plan.status!r}, not approved"
            )
        decision = self.latest_decision(plan_id)
        if decision is None:
            raise PlanNotApprovedError(
                f"plan {plan_id!r} has no recorded human decision"
            )
        if decision.decision != "approved":
            raise PlanNotApprovedError(
                f"plan {plan_id!r} was rejected by {decision.actor!r}"
            )
        if decision.plan_hash != actual_hash:
            raise PlanNotApprovedError(
                "the recorded approval is bound to a different plan hash; "
                "renewed approval is required"
            )
        return plan

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_plan(row: Any) -> PlanVersion:
        return PlanVersion(
            plan_id=row["plan_id"],
            project_id=row["project_id"],
            version=int(row["version"]),
            plan_hash=row["plan_hash"],
            plan=json.loads(row["plan_json"]),
            brief_id=row["brief_id"],
            research_id=row["research_id"],
            status=row["status"],
            pipeline=row["pipeline"],
            profile=row["profile"],
            created_by=row["created_by"],
            parent_plan_id=row["parent_plan_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
