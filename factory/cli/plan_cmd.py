"""
factory.cli.plan_cmd
=====================
CLI sub-commands for the plan lifecycle (Research -> Plan -> Approve).

Commands::

    factory plan create <project_path> --spec <spec.md> [--profile ...] [--pipeline ...]
    factory plan list   [--project <id>] [--limit N]
    factory plan show   <plan_id> [--json]
    factory plan approve <plan_id> --hash <sha256> --by "<name>" [--note "..."]
    factory plan reject  <plan_id> --hash <sha256> --by "<name>" [--note "..."]

Design notes
------------
* Planning is deterministic: the generator derives tasks from the persisted
  brief. No model plan is accepted as proof anywhere in this path.
* Approval is bound to the exact canonical SHA-256 the human reviewed.
  Approving a different hash, or approving a tampered plan, is refused by
  ``PlanStore`` (fail-closed), not by this module.
* Nothing here executes work. Execution requires ``factory run --plan-id``
  with an approved plan.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from factory.audit.logger import AuditLogger
from factory.config import get_settings
from factory.database import initialize_database
from factory.errors import ConfigurationError
from factory.models import ProjectStatus
from factory.planning.brief import build_brief, save_brief
from factory.planning.generator import generate_plan
from factory.planning.plan_store import (
    PlanApprovalError,
    PlanNotFoundError,
    PlanNotApprovedError,
    PlanStore,
    PlanValidationError,
)
from factory.planning.research import run_local_research
from factory.projects.intake import IntakeError, ProjectIntake
from factory.projects.profiles import resolve_profile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_plan_subparser(subparsers: argparse._SubParsersAction) -> None:
    plan = subparsers.add_parser(
        "plan", help="Plan lifecycle (create/list/show/approve/reject)"
    )
    sub = plan.add_subparsers(dest="plan_command", required=True)

    create = sub.add_parser(
        "create", help="Create a plan from a project spec (deterministic)"
    )
    create.add_argument("project_path", type=str,
                        help="Project workspace directory")
    create.add_argument("--spec", type=str, required=True,
                        help="Path to the specification file (Markdown/text)")
    create.add_argument("--name", type=str, default="",
                        help="Project name (default: workspace folder name)")
    create.add_argument("--profile", type=str, default="",
                        choices=["", "fast", "balanced", "hardened"],
                        help="Override the project's quality profile")
    create.add_argument("--pipeline", type=str, default="standard",
                        choices=["standard", "smoke"],
                        help="standard = release-eligible stages; "
                             "smoke = single builder task, never release "
                             "eligible (for bounded model evaluation)")
    create.add_argument("--created-by", type=str, default="cli",
                        help="Recorded plan author tag")

    list_p = sub.add_parser("list", help="List plan versions")
    list_p.add_argument("--project", type=str, default="")
    list_p.add_argument("--limit", type=int, default=20)

    show = sub.add_parser("show", help="Show a plan version and its hash")
    show.add_argument("plan_id", type=str)
    show.add_argument("--json", action="store_true",
                      help="Print the full stored plan JSON")

    approve = sub.add_parser(
        "approve", help="Approve the exact reviewed plan content"
    )
    approve.add_argument("plan_id", type=str)
    approve.add_argument("--hash", type=str, required=True,
                         help="Exact hash printed by 'factory plan show'")
    approve.add_argument("--by", type=str, required=True,
                         help="Human actor recording the decision")
    approve.add_argument("--note", type=str, default="")

    reject = sub.add_parser("reject", help="Reject a plan")
    reject.add_argument("plan_id", type=str)
    reject.add_argument("--hash", type=str, required=True,
                        help="Exact hash printed by 'factory plan show'")
    reject.add_argument("--by", type=str, required=True,
                        help="Human actor recording the decision")
    reject.add_argument("--note", type=str, default="")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _print_plan_summary(plan) -> None:
    payload = plan.plan
    tasks = payload.get("tasks", [])
    requirements = payload.get("requirements", [])
    print(f"Plan:      {plan.plan_id} (v{plan.version}, {plan.status})")
    print(f"Project:   {plan.project_id}")
    print(f"Pipeline:  {plan.pipeline}  release_eligible={payload.get('release_eligible')}")
    print(f"Profile:   {plan.profile}")
    print(f"Created:   {plan.created_at.isoformat()}  by {plan.created_by}")
    print(f"Hash:      {plan.plan_hash}")
    if plan.brief_id:
        print(f"Brief:     {plan.brief_id}")
    if plan.research_id:
        print(f"Research:  {plan.research_id}")
    print(f"Requirements: {len(requirements)}   Tasks: {len(tasks)}")
    print("")
    print("Tasks:")
    for task in tasks:
        deps = ", ".join(task.get("depends_on", [])) or "-"
        meta = task.get("metadata", {}) or {}
        tools = ", ".join(str(t) for t in meta.get("required_tools", [])) or "-"
        print(
            f"  {task.get('task_id', '?'):<24} {task.get('agent_type', '?'):<14}"
            f" deps=[{deps}] tools=[{tools}]"
        )
        description = str(task.get("description", "") or "").strip()
        if description:
            print(f"      {description[:160]}")


def cmd_create(args: argparse.Namespace) -> int:
    initialize_database()
    get_settings()

    workspace = Path(args.project_path).resolve()
    if not workspace.is_dir():
        print(f"Error: project workspace {workspace} is not a directory.")
        return 1

    spec_path = Path(args.spec).resolve()
    if not spec_path.is_file():
        print(f"Error: specification file {spec_path} does not exist.")
        return 1
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: cannot read specification: {exc}")
        return 1
    if not spec_text.strip():
        print("Error: specification file is empty.")
        return 1

    intake = ProjectIntake()
    try:
        project = intake.find_by_workspace(workspace)
        if project is None:
            name = args.name.strip() or workspace.name
            first_lines = [
                ln.strip() for ln in spec_text.splitlines() if ln.strip()
            ]
            description = (first_lines[0] if first_lines else "")[:200]
            project = intake.register(
                name=name,
                description=description,
                workspace_root=workspace,
                quality_profile=resolve_profile(args.profile or None),
                metadata={"spec_file": str(spec_path)},
            )
            print(
                f"Registered project {project.name!r} "
                f"({project.project_id})."
            )
        elif args.profile:
            intake.update_profile(
                project.project_id, resolve_profile(args.profile)
            )
            project = intake.load(project.project_id)
    except (IntakeError, ConfigurationError) as exc:
        print(f"Error: {exc}")
        return 1

    profile = resolve_profile(project.quality_profile)

    brief = build_brief(
        project.project_id,
        spec_text,
        name=project.name,
        source_kind="file",
        source_reference=str(spec_path),
        metadata={"workspace_root": str(workspace)},
    )
    save_brief(brief)

    research = run_local_research(project.project_id, brief, workspace)

    try:
        plan_payload = generate_plan(
            project.project_id,
            brief,
            research,
            profile=profile,
            pipeline=args.pipeline,
        )
    except PlanValidationError as exc:
        print(f"Cannot create a plan: {exc}")
        print(
            "The plan was not stored. Resolve the issue in the spec or "
            "workspace and run 'factory plan create' again."
        )
        return 1

    store = PlanStore()
    try:
        version = store.create(
            project.project_id,
            plan_payload,
            brief_id=brief.brief_id,
            research_id=research.research_id,
            created_by=str(args.created_by),
        )
    except PlanValidationError as exc:
        print(f"Cannot store a plan: {exc}")
        return 1

    intake.update_status(project.project_id, ProjectStatus.PLANNING)

    audit = AuditLogger(project_id=project.project_id)
    audit.emit("plan.created", {
        "plan_id": version.plan_id,
        "version": version.version,
        "plan_hash": version.plan_hash,
        "pipeline": version.pipeline,
        "profile": version.profile,
        "brief_id": brief.brief_id,
        "research_id": research.research_id,
    })

    print("")
    print(f"Brief:    {brief.brief_id}  (spec sha256 {brief.source_sha256[:12]})")
    print(f"Research: {research.research_id}  (local only; no external research performed)")
    print(f"Plan created: {version.plan_id} (v{version.version}, pending)")
    print(f"Hash:     {version.plan_hash}")
    print("")
    print(f"Review the plan:              factory plan show {version.plan_id}")
    print(
        "Approve exactly this content: factory plan approve "
        f"{version.plan_id} --hash {version.plan_hash} --by \"<your name>\""
    )
    print(
        "Then execute it:              factory run "
        f"--plan-id {version.plan_id}"
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = PlanStore()
    plans = store.list(args.project or None, limit=max(1, int(args.limit)))
    if not plans:
        print("No plans found.")
        return 0
    print(f"{'VERSION':<8} {'STATUS':<11} {'PIPELINE':<9} {'PROFILE':<9} "
          f"{'CREATED':<26} {'HASH':<14} PLAN_ID")
    for plan in plans:
        print(
            f"{plan.version:<8} {plan.status:<11} {plan.pipeline:<9} "
            f"{plan.profile:<9} {plan.created_at.isoformat():<26} "
            f"{plan.plan_hash[:12]:<14} {plan.plan_id}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = PlanStore()
    plan = store.load(args.plan_id)
    if plan is None:
        print(f"Error: plan {args.plan_id!r} does not exist.")
        return 1

    _print_plan_summary(plan)

    if args.json:
        print("")
        print(json.dumps(plan.plan, indent=2, sort_keys=True))

    decision = store.latest_decision(plan.plan_id)
    if decision is not None:
        print("")
        print(
            f"Latest decision: {decision.decision} by {decision.actor} "
            f"at {decision.decided_at.isoformat()}"
            + (f" (note: {decision.note})" if decision.note else "")
        )
    if plan.status == "pending":
        print("")
        print("This plan has not been decided yet.")
        print(
            f"  factory plan approve {plan.plan_id} --hash {plan.plan_hash} "
            "--by \"<your name>\""
        )
        print(
            f"  factory plan reject  {plan.plan_id} --hash {plan.plan_hash} "
            "--by \"<your name>\""
        )
    elif plan.status == "superseded":
        print("")
        print("This version was superseded by a newer plan version.")
    return 0


def _decide(args: argparse.Namespace, decision: str) -> int:
    store = PlanStore()
    try:
        approval = store.decide(
            args.plan_id,
            decision,
            actor=args.by,
            expected_hash=args.hash,
            note=args.note,
        )
    except (PlanNotFoundError, PlanApprovalError, PlanNotApprovedError) as exc:
        print(f"Decision refused: {exc}")
        return 1

    plan = store.load(args.plan_id)
    audit = AuditLogger(project_id=plan.project_id if plan else None)
    audit.emit(f"plan.{decision}", {
        "plan_id": approval.plan_id,
        "plan_hash": approval.plan_hash,
        "actor": approval.actor,
        "note": approval.note,
    })

    if decision == "approved":
        print(
            f"Plan {approval.plan_id} approved by {approval.actor} "
            f"(hash {approval.plan_hash[:12]})."
        )
        print(f"Execute it: factory run --plan-id {approval.plan_id}")
    else:
        print(
            f"Plan {approval.plan_id} rejected by {approval.actor}. "
            "It will never execute."
        )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    return _decide(args, "approved")


def cmd_reject(args: argparse.Namespace) -> int:
    return _decide(args, "rejected")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch_plan(args: argparse.Namespace) -> int:
    command = getattr(args, "plan_command", "")
    if command == "create":
        return cmd_create(args)
    if command == "list":
        return cmd_list(args)
    if command == "show":
        return cmd_show(args)
    if command == "approve":
        return cmd_approve(args)
    if command == "reject":
        return cmd_reject(args)
    print(f"Unknown plan command {command!r}.")
    return 1
