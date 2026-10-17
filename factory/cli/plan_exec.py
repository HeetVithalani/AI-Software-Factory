"""
factory.cli.plan_exec
======================
Execution of an approved, persisted plan (the only plan-based run path).

Rules
-----
* Execution requires an approved plan whose stored content still matches the
  hash the human approved (``PlanStore.require_approved`` — fail-closed).
* Startup state is reconciled before any work starts
  (``prepare_dag_for_execution``). Uncertain state refuses the run instead
  of guessing.
* The project's own workspace is the only filesystem root; the permission
  broker remains the enforcement point for every operation.
* Smoke plans are executed for bounded evaluation only and can never be
  release eligible (enforced at plan creation and validation).
"""

from __future__ import annotations

import logging
from pathlib import Path

from factory.audit.logger import AuditLogger
from factory.cli.plan_loader import dag_from_plan_dict, register_all_agents
from factory.context.builder import ContextBuilder
from factory.database import initialize_database
from factory.errors import ConfigurationError
from factory.llm.router import LLMRouter
from factory.models import ProjectStatus, TaskStatus
from factory.orchestration.boot import prepare_dag_for_execution
from factory.orchestration.orchestrator import Orchestrator, OrchestratorConfig
from factory.orchestration.recovery import RecoveryManager
from factory.orchestration.task_store import TaskStore
from factory.permissions.broker import PermissionBroker
from factory.permissions.policies import load_policies
from factory.planning.plan_store import (
    PlanNotApprovedError,
    PlanNotFoundError,
    PlanStore,
)
from factory.projects.intake import ProjectIntake
from factory.projects.profiles import profile_policy, resolve_profile
from factory.skills.bindings import project_key_for
from factory.skills.provider import SkillContextProvider
from factory.state import StateManager

logger = logging.getLogger(__name__)


def _print_approval_notice() -> None:
    try:
        from factory.database import get_connection
        from factory.config import DB_PATH
        with get_connection(str(DB_PATH)) as conn:
            pending = conn.execute(
                "SELECT request_id FROM approvals WHERE status = 'pending' "
                "ORDER BY requested_at DESC LIMIT 10"
            ).fetchall()
        if pending:
            print("")
            print("APPROVAL REQUIRED - tasks are blocked pending human review.")
            for row in pending:
                print(f"   factory approvals show {row['request_id']}")
                print(
                    f"   factory approvals grant {row['request_id']}  "
                    "# or deny"
                )
            print(
                "   After granting, re-run the same command; parked tasks "
                "are re-verified before resuming."
            )
    except Exception:  # noqa: BLE001 — best-effort notice only
        pass


def run_approved_plan(args) -> int:
    """Execute the plan identified by ``args.plan_id``. Returns a CLI code."""
    initialize_database()
    store = PlanStore()

    try:
        plan = store.require_approved(args.plan_id)
    except (PlanNotFoundError, PlanNotApprovedError) as exc:
        print(f"Cannot execute: {exc}")
        print(
            "Inspect it with 'factory plan show <plan_id>' and approve the "
            "exact hash before running."
        )
        return 1

    intake = ProjectIntake()
    project = intake.load(plan.project_id)
    if project is None:
        print(
            f"Cannot execute: project {plan.project_id!r} is not registered."
        )
        return 1
    if not project.workspace_path:
        print(
            "Cannot execute: the project has no workspace path; refusing to "
            "run without a bounded workspace."
        )
        return 1
    workspace = Path(project.workspace_path).resolve()
    if not workspace.is_dir():
        print(f"Cannot execute: workspace {workspace} does not exist.")
        return 1

    try:
        policy = profile_policy(plan.profile)
    except ConfigurationError as exc:
        print(f"Cannot execute: stored plan has an invalid profile. {exc}")
        return 1

    project_profile = resolve_profile(project.quality_profile)
    if project_profile.value != policy.profile.value:
        print(
            f"Note: project profile is {project_profile.value!r} but the "
            f"approved plan uses {policy.profile.value!r}; the plan's "
            "approved profile is authoritative for this run."
        )

    try:
        dag = dag_from_plan_dict(
            plan.plan, source=f"plan {plan.plan_id} v{plan.version}"
        )
    except Exception as exc:
        print(f"Cannot execute: stored plan is not executable: {exc}")
        return 1

    audit = AuditLogger(project_id=plan.project_id)
    broker = PermissionBroker(policies=load_policies())
    llm_router = LLMRouter()
    state = StateManager().boot_state()
    model_override = getattr(args, "model", None) or None
    if model_override:
        print(f"Per-run model override: {model_override}")
    skill_provider = SkillContextProvider(
        project_key=project_key_for(workspace),
        audit_logger=audit,
    )
    context_builder = ContextBuilder(
        state=state,
        skill_provider=skill_provider,
        default_model=model_override,
    )
    recovery_manager = RecoveryManager()
    task_store = TaskStore()

    # ------------------------------------------------------------------
    # Startup reconstruction — refuse to run on uncertain state.
    # ------------------------------------------------------------------
    boot = prepare_dag_for_execution(
        dag,
        project_id=plan.project_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        recovery_manager=recovery_manager,
        audit_logger=audit,
        require_evidence_for_completed=(plan.pipeline != "smoke"),
    )
    if boot.recovery_required:
        print("Execution refused: previous state requires human recovery.")
        for issue in boot.issues:
            print(f"  - {issue}")
        if boot.uncertain_task_ids:
            print("")
            print("Resolve each recovery-required task explicitly:")
            for task_id in boot.uncertain_task_ids:
                print(
                    f"  factory recovery resolve {task_id} --action retry "
                    "--by \"<your name>\"   # or --action skip"
                )
        print("")
        print("Inspect state with: factory recovery status")
        audit.emit("plan.execution_refused", {
            "plan_id": plan.plan_id,
            "issues": boot.issues,
            "uncertain_task_ids": boot.uncertain_task_ids,
        })
        return 2

    restored = ", ".join(
        f"{task_id}={status}" for task_id, status in sorted(boot.restored.items())
        if status != "pending"
    )
    if restored:
        print(f"Restored from previous state: {restored}")

    audit.emit("plan.execution_started", {
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "pipeline": plan.pipeline,
        "profile": plan.profile,
        "model_override": model_override or "",
        "checkpoint_id": boot.checkpoint_id or "",
    })

    # ------------------------------------------------------------------
    # Orchestrator + agents
    # ------------------------------------------------------------------
    orch = Orchestrator(
        dag=dag,
        config=OrchestratorConfig(
            default_max_retries=int(policy.default_max_retries),
        ),
        state_manager=state,
        recovery_manager=recovery_manager,
        audit_logger=audit,
        permission_broker=broker,
        project_id=plan.project_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        task_store=task_store,
        workspace_root=workspace,
    )
    registered = register_all_agents(
        orch,
        workspace_root=workspace,
        broker=broker,
        audit_logger=audit,
        llm_router=llm_router,
        context_builder=context_builder,
        project_id=plan.project_id,
    )
    print(f"Agents registered: {registered}")

    intake.update_status(plan.project_id, ProjectStatus.BUILDING)
    status = orch.run()
    print(f"Run completed with status: {status.value}")

    # ------------------------------------------------------------------
    # Honest project status (no release claim — the final audit decides).
    # ------------------------------------------------------------------
    nodes = dag.all_nodes()
    all_completed = all(
        n.status == TaskStatus.COMPLETED for n in nodes
    ) if nodes else False
    any_terminal_negative = any(
        n.status in (TaskStatus.FAILED, TaskStatus.CANCELLED) for n in nodes
    )
    if all_completed:
        intake.update_status(plan.project_id, ProjectStatus.COMPLETED)
    elif any_terminal_negative:
        intake.update_status(plan.project_id, ProjectStatus.FAILED)
    # Otherwise the project stays BUILDING (parked / paused work remains).

    audit.emit("plan.execution_finished", {
        "plan_id": plan.plan_id,
        "orchestrator_status": status.value,
        "task_statuses": {
            n.task_id: (
                n.status.value if isinstance(n.status, TaskStatus)
                else str(n.status)
            )
            for n in nodes
        },
        "all_completed": all_completed,
    })

    if status.value == "awaiting_approval":
        print(
            "Run paused: task(s) await a human approval decision "
            "(not a failure — no retry budget was consumed)."
        )
        _print_approval_notice()

    if status.value == "recovery_required":
        print(
            "State became uncertain mid-run; human recovery is required "
            "before another attempt."
        )
        print("Inspect: factory recovery status")

    return 0 if status.value in ("idle", "running") else 1
