"""
factory.cli.inspect_cmd
========================
Read-only inspection commands plus explicit recovery resolution.

Commands::

    factory tasks    [--project <id>] [--status <status>] [--limit N]
    factory evidence [--project <id>] [--task <task_id>] [--limit N]
    factory audit    [--project <id>] [--type <event_type>] [--limit N]
    factory recovery status  [--project <id>]
    factory recovery resolve <task_id> --action retry|skip --by "<name>" [--note "..."]

Design notes
------------
* Every command reports exactly what is persisted — no inferred success.
* ``recovery resolve`` is the only mutating command here, and it only
  applies to tasks explicitly marked ``recovery_required`` (uncertain side
  effects). A human actor is mandatory.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from factory.audit.evidence import hash_file
from factory.audit.logger import AuditLogger
from factory.database import get_connection, initialize_database
from factory.orchestration.recovery import RecoveryManager
from factory.orchestration.task_store import TaskStore
from factory.projects.intake import ProjectIntake

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_inspect_subparsers(subparsers: argparse._SubParsersAction) -> None:
    tasks = subparsers.add_parser("tasks", help="List persisted task rows")
    tasks.add_argument("--project", type=str, default="")
    tasks.add_argument("--status", type=str, default="")
    tasks.add_argument("--limit", type=int, default=50)

    evidence = subparsers.add_parser(
        "evidence", help="List recorded evidence"
    )
    evidence.add_argument("--project", type=str, default="")
    evidence.add_argument("--task", type=str, default="")
    evidence.add_argument("--limit", type=int, default=50)

    audit = subparsers.add_parser("audit", help="List recorded events")
    audit.add_argument("--project", type=str, default="")
    audit.add_argument("--type", type=str, default="")
    audit.add_argument("--limit", type=int, default=50)

    recovery = subparsers.add_parser(
        "recovery", help="Startup recovery state and resolution"
    )
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)

    status = recovery_sub.add_parser("status", help="Show recovery state")
    status.add_argument("--project", type=str, default="")

    resolve = recovery_sub.add_parser(
        "resolve", help="Resolve a recovery-required task explicitly"
    )
    resolve.add_argument("task_id", type=str)
    resolve.add_argument("--action", type=str, required=True,
                         choices=["retry", "skip"])
    resolve.add_argument("--by", type=str, required=True,
                         help="Human actor resolving the uncertainty")
    resolve.add_argument("--note", type=str, default="")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_project(project_id: str):
    """Return the requested project, or the most recent one."""
    intake = ProjectIntake()
    if project_id:
        project = intake.load(project_id)
        if project is None:
            print(f"Error: project {project_id!r} does not exist.")
            return None
        return project
    project = intake.latest_project()
    if project is None:
        print("No projects are registered.")
        return None
    return project


def _load_metadata(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_tasks(args: argparse.Namespace) -> int:
    initialize_database()
    project = _resolve_project(args.project)
    if project is None:
        return 1
    print(f"TASKS for {project.name} ({project.project_id})")
    print("")

    sql = "SELECT * FROM tasks WHERE project_id = ?"
    params: list[Any] = [project.project_id]
    if args.status:
        sql += " AND status = ?"
        params.append(args.status)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(max(1, int(args.limit)))

    with get_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    if not rows:
        print("No task rows.")
        return 0

    for row in rows:
        error = (row["error"] or "").splitlines()
        error_txt = f"  error={error[0][:80]}" if error else ""
        print(
            f"  {row['task_id']:<28} {row['status']:<16} "
            f"{row['agent_type']:<14} retries={row['retry_count']}"
            f"{error_txt}"
        )

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("")
    print("Summary: " + ", ".join(
        f"{status}={count}" for status, count in sorted(counts.items())
    ))
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    initialize_database()
    project = _resolve_project(args.project)
    if project is None:
        return 1
    print(f"EVIDENCE for {project.name} ({project.project_id})")
    print("")

    sql = "SELECT * FROM evidence WHERE project_id = ?"
    params: list[Any] = [project.project_id]
    if args.task:
        sql += " AND task_id = ?"
        params.append(args.task)
    sql += " ORDER BY recorded_at DESC, rowid DESC LIMIT ?"
    params.append(max(1, int(args.limit)))

    with get_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    if not rows:
        print("No evidence recorded.")
        return 0

    for row in rows:
        digest = (row["hash_sha256"] or "")[:12]
        print(
            f"  {row['task_id']:<24} {row['kind']:<14} "
            f"{row['path']:<44} {digest:<14} {row['recorded_at']}"
        )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    initialize_database()
    project = _resolve_project(args.project)
    if project is None:
        return 1
    print(f"EVENTS for {project.name} ({project.project_id})")
    print("")

    sql = "SELECT * FROM events WHERE project_id = ?"
    params: list[Any] = [project.project_id]
    if args.type:
        sql += " AND event_type LIKE ?"
        params.append(f"%{args.type}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(args.limit)))

    with get_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    if not rows:
        print("No events recorded.")
        return 0

    for row in rows:
        task = row["task_id"] or "-"
        agent = row["agent_id"] or "-"
        print(
            f"  {row['timestamp']:<32} {row['event_type']:<34} "
            f"task={task:<22} agent={agent}"
        )
    return 0


def cmd_recovery_status(args: argparse.Namespace) -> int:
    initialize_database()
    project = _resolve_project(args.project)
    if project is None:
        return 1
    print(f"RECOVERY STATUS for {project.name} ({project.project_id})")
    print("")

    store = TaskStore()
    rows = store.list_rows(project.project_id)
    recovery_rows = [
        row for row in rows
        if _load_metadata(row["metadata"]).get("recovery_required")
    ]
    if recovery_rows:
        print(f"Recovery-required tasks: {len(recovery_rows)}")
        for row in recovery_rows:
            meta = _load_metadata(row["metadata"])
            reason = str(meta.get("recovery_reason", "uncertain state"))
            print(f"  - {row['task_id']}  status={row['status']}")
            print(f"      reason: {reason[:160]}")
    else:
        print("Recovery-required tasks: none")

    checkpoint = RecoveryManager().load_last_checkpoint(project.project_id)
    print("")
    if checkpoint is None:
        print("Last checkpoint: none")
    else:
        print(
            f"Last checkpoint: {checkpoint.checkpoint_id} "
            f"(task={checkpoint.task_id or '-'}, "
            f"created={checkpoint.created_at.isoformat()})"
        )
        hashes = checkpoint.workspace_hashes or {}
        changed: list[str] = []
        missing: list[str] = []
        for path_str, expected in hashes.items():
            path = Path(path_str)
            if not path.exists():
                missing.append(path_str)
            elif hash_file(path) != expected:
                changed.append(path_str)
        print(
            f"  workspace hashes: {len(hashes)} recorded, "
            f"{len(changed)} changed, {len(missing)} missing"
        )
        for path_str in changed:
            print(f"    ! changed: {path_str}")
        for path_str in missing:
            print(f"    ! missing: {path_str}")

    # Parked approvals are part of honest recovery status.
    with get_connection() as conn:
        parked = conn.execute(
            "SELECT COUNT(*) AS n FROM approvals "
            "WHERE project_id = ? AND status = 'pending'",
            (project.project_id,),
        ).fetchone()
    print("")
    print(f"Pending approvals: {int(parked['n'])} (factory approvals list)")
    return 0


def cmd_recovery_resolve(args: argparse.Namespace) -> int:
    initialize_database()
    store = TaskStore()
    row = store.get_row(args.task_id)
    if row is None:
        print(f"Error: task {args.task_id!r} does not exist.")
        return 1

    try:
        metadata = store.resolve_recovery(
            args.task_id,
            args.action,
            actor=args.by,
            note=args.note,
        )
    except ValueError as exc:
        print(f"Resolution refused: {exc}")
        return 1

    audit = AuditLogger(project_id=row["project_id"])
    audit.emit("recovery.resolved", {
        "task_id": args.task_id,
        "action": args.action,
        "actor": args.by.strip(),
        "note": args.note,
    })

    print(
        f"Task {args.task_id} resolved: action={args.action} "
        f"by {args.by.strip()}."
    )
    plan_id = str(metadata.get("plan_id", "") or "")
    if args.action == "retry":
        if plan_id:
            print(
                "Resume with: factory run --plan-id "
                f"{plan_id}"
            )
        print(
            "The task will be re-dispatched on the next run; its side "
            "effects will be produced from a clean state."
        )
    else:
        print(
            "The task is permanently skipped; dependent work may not run. "
            "The plan can no longer be considered complete."
        )
    return 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch_inspect(args: argparse.Namespace) -> int:
    command = args.command
    if command == "tasks":
        return cmd_tasks(args)
    if command == "evidence":
        return cmd_evidence(args)
    if command == "audit":
        return cmd_audit(args)
    if command == "recovery":
        recovery_command = getattr(args, "recovery_command", "")
        if recovery_command == "status":
            return cmd_recovery_status(args)
        if recovery_command == "resolve":
            return cmd_recovery_resolve(args)
        print(f"Unknown recovery command {recovery_command!r}.")
        return 1
    print(f"Unknown inspect command {command!r}.")
    return 1
