"""
factory.cli.main
================
Main CLI for the AI Software Factory.

Commands:
  factory status
  factory run <project_path> [--plan <plan.json>] [--task-desc "..."] [--model M]
  factory run --plan-id <plan_id> [--model M]
  factory plan create|list|show|approve|reject
  factory tasks|evidence|audit [--project <id>]
  factory recovery status|resolve
  factory approvals list|show|grant|deny
  factory intake discover|fetch|scan|check|approve|install|status|disable
"""

import argparse
import logging
import sys
from pathlib import Path
from uuid import uuid4

from factory.config import get_settings
from factory.database import initialize_database
from factory.state import StateManager
from factory.orchestration.orchestrator import Orchestrator, OrchestratorConfig
from factory.orchestration.dag import TaskDAG
from factory.permissions.broker import PermissionBroker
from factory.permissions.policies import load_policies
from factory.audit.logger import AuditLogger
from factory.llm.router import LLMRouter
from factory.context.builder import ContextBuilder

from factory.cli.plan_loader import (
    load_dag_from_plan,
    find_plan_path,
    register_all_agents,
)
from factory.cli.approvals_cmd import (
    register_approvals_subparser,
    dispatch_approvals,
)
from factory.cli.intake_cmd import (
    register_intake_subparser,
    dispatch_intake,
)
from factory.cli.plan_cmd import (
    register_plan_subparser,
    dispatch_plan,
)
from factory.cli.inspect_cmd import (
    register_inspect_subparsers,
    dispatch_inspect,
)

logger = logging.getLogger(__name__)


def run_status() -> int:
    sm = StateManager()
    state = sm.boot_state()
    print(f"Status: {'PAUSED' if state.paused else 'IDLE'}")
    return 0


def run_project(
    project_path: str,
    task_desc: str,
    plan_arg: str | None,
    model: str | None = None,
) -> int:
    path = Path(project_path).resolve()
    print(f"Running factory on project at {path}")

    # ------------------------------------------------------------------
    # 1. Infrastructure setup
    # ------------------------------------------------------------------
    initialize_database()
    sm = StateManager()
    state = sm.boot_state()
    audit_logger = AuditLogger()
    broker = PermissionBroker(policies=load_policies())
    llm_router = LLMRouter()
    # Phase 2c: one explicit per-run integration point for project-scoped
    # skill context. The provider's project_key is derived from the SAME
    # resolved workspace path the enable/disable CLI uses, so enablement
    # and runtime retrieval can never disagree about project identity.
    from factory.skills.bindings import project_key_for
    from factory.skills.provider import SkillContextProvider
    skill_provider = SkillContextProvider(
        project_key=project_key_for(path),
        audit_logger=audit_logger,
    )
    if model:
        print(f"Per-run model override: {model}")
    context_builder = ContextBuilder(
        state=state, skill_provider=skill_provider, default_model=model,
    )
    settings = get_settings()

    # ------------------------------------------------------------------
    # 2. Load plan.json → TaskDAG
    # ------------------------------------------------------------------
    explicit_plan = Path(plan_arg).resolve() if plan_arg else None
    plan_path = find_plan_path(path, explicit_plan=explicit_plan)

    try:
        dag, plan_meta = load_dag_from_plan(plan_path)
        project_id = plan_meta.get("project_id", "")
        print(
            f"Plan loaded: {dag.node_count()} tasks from {plan_path}"
        )
    except Exception as exc:
        # Graceful fallback: single builder task (matches original behaviour)
        logger.warning(
            "Could not load plan.json (%s). Falling back to single builder task.",
            exc,
        )
        if task_desc:
            print(f"Task description: {task_desc}")
        else:
            task_desc = "Implement the main logic based on the project requirements."
            print("Using default build task.")

        dag = TaskDAG()
        from factory.orchestration.dag import TaskNode
        task_id = f"build-{uuid4().hex[:8]}"
        dag.add(TaskNode(
            task_id=task_id,
            name="Build Project",
            agent_type="builder",
            metadata={"description": task_desc},
        ))
        project_id = ""

    # ------------------------------------------------------------------
    # 3. Orchestrator
    # ------------------------------------------------------------------
    orch = Orchestrator(
        dag=dag,
        state_manager=sm,
        audit_logger=audit_logger,
        permission_broker=broker,
    )

    # ------------------------------------------------------------------
    # 4. Register all seven agents
    # ------------------------------------------------------------------
    agent_kwargs = dict(
        workspace_root=path,
        broker=broker,
        audit_logger=audit_logger,
        llm_router=llm_router,
        context_builder=context_builder,
        project_id=project_id,
    )
    registered = register_all_agents(orch, **agent_kwargs)
    print(f"Agents registered: {registered}")

    # ------------------------------------------------------------------
    # 5. Run
    # ------------------------------------------------------------------
    status = orch.run()
    print(f"Run completed with status: {status.value}")
    if status.value == "awaiting_approval":
        print(
            "Run paused: task(s) await a human approval decision "
            "(not a failure — no retry budget was consumed)."
        )

    # ------------------------------------------------------------------
    # 6. Approval gate notice
    # ------------------------------------------------------------------
    # If any tasks require approval, surface the IDs so the operator can act.
    try:
        from factory.database import get_connection
        from factory.config import DB_PATH
        with get_connection(str(DB_PATH)) as conn:
            pending = conn.execute(
                "SELECT request_id, action_category, target "
                "FROM approvals WHERE status = 'pending' "
                "ORDER BY requested_at DESC LIMIT 10"
            ).fetchall()
        if pending:
            print("\nAPPROVAL REQUIRED - tasks are blocked pending human review.")
            print("   Run the following to resolve:")
            for req_id, category, target in pending:
                print(f"     factory approvals show {req_id}")
                print(f"     factory approvals grant {req_id}  # or deny")
            print(
                "\n   After granting, re-run 'factory run' to requeue the "
                "parked tasks (status and operation fingerprint are "
                "re-verified first).\n"
            )
    except Exception:
        pass  # Best-effort; do not crash the run summary

    return 0 if status.value in ("idle", "running") else 1


def _setup_logging(level: str = "WARNING") -> None:
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s",
        level=getattr(logging, level.upper(), logging.WARNING),
        stream=sys.stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="factory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show factory status")

    run_parser = subparsers.add_parser("run", help="Run factory on a project")
    run_parser.add_argument("project_path", type=str, nargs="?", default="",
                            help="Project workspace (required unless --plan-id)")
    run_parser.add_argument("--task-desc", type=str, default="",
                            help="Override task description (used in fallback mode)")
    run_parser.add_argument("--plan", type=str, default=None,
                            help="Path to plan.json (default: <project>/plan.json)")
    run_parser.add_argument("--plan-id", type=str, default=None,
                            help="Execute an approved persisted plan "
                                 "(created with 'factory plan create')")
    run_parser.add_argument("--model", type=str, default=None,
                            help="Per-run model override (e.g. "
                                 "ollama:qwen2.5-coder:7b-agent). Applies to "
                                 "this run only; the configured default is "
                                 "unchanged.")

    # Approvals sub-command
    register_approvals_subparser(subparsers)

    # Tool/skill intake sub-command (Phase 2a)
    register_intake_subparser(subparsers)

    # Plan lifecycle (RPI): create/list/show/approve/reject
    register_plan_subparser(subparsers)

    # Read-only inspection + recovery resolution
    register_inspect_subparsers(subparsers)

    args = parser.parse_args()

    settings = get_settings()
    _setup_logging(settings.factory.log_level)

    if args.command == "status":
        return run_status()
    elif args.command == "run":
        if args.plan_id:
            from factory.cli.plan_exec import run_approved_plan
            return run_approved_plan(args)
        if not args.project_path:
            parser.error("project_path is required unless --plan-id is given")
        return run_project(
            args.project_path, args.task_desc, args.plan, model=args.model,
        )
    elif args.command == "approvals":
        return dispatch_approvals(args)
    elif args.command == "intake":
        return dispatch_intake(args)
    elif args.command == "plan":
        return dispatch_plan(args)
    elif args.command in ("tasks", "evidence", "audit", "recovery"):
        return dispatch_inspect(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
