"""
factory.cli.approvals_cmd
==========================
CLI sub-commands for the factory approval system.

Commands::

    factory approvals list   [--project <id>] [--status pending|approved|denied]
    factory approvals show   <request_id>
    factory approvals grant  <request_id> [--note "reason"]
    factory approvals deny   <request_id> [--note "reason"]

Design notes
------------
* Reuses the existing ApprovalManager and approvals table — no schema changes.
* Non-negotiable policies are enforced by PermissionBroker, not here.
  This module only surfaces what is already in the DB.
* Grant/deny are human-driven — never automated.
* No approval is silently created or bypassed here.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from factory.database import get_connection
from factory.config import get_settings, DB_PATH

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_row(row: dict[str, Any]) -> str:
    """Format one approval row as a readable block."""
    lines = [
        f"  ID:          {row.get('request_id', 'N/A')}",
        f"  Status:      {row.get('status', 'N/A')}",
        f"  Project:     {row.get('project_id', 'N/A')}",
        f"  Task:        {row.get('task_id', 'N/A')}",
        f"  Category:    {row.get('action_category', 'N/A')}",
        f"  Risk:        {row.get('risk_level', 'N/A')}",
        f"  Target:      {row.get('target', 'N/A')}",
        f"  Reason:      {row.get('reason', '')}",
        f"  Effect:      {row.get('expected_effect', '')}",
        f"  Consequence: {row.get('deny_consequence', '')}",
        f"  Requested:   {row.get('requested_at', '')}",
        f"  Decided at:  {row.get('decided_at') or '-'}",
        f"  Decided by:  {row.get('decided_by') or '-'}",
        f"  Note:        {row.get('decision_note') or '-'}",
    ]
    return "\n".join(lines)


def _query_approvals(
    status_filter: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Query the approvals table with optional filters."""
    query = "SELECT * FROM approvals WHERE 1=1"
    params: list[Any] = []
    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)
    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)
    query += " ORDER BY requested_at DESC"

    with get_connection(str(DB_PATH)) as conn:
        conn.row_factory = lambda c, r: {
            c.description[i][0]: r[i] for i in range(len(r))
        }
        rows = conn.execute(query, params).fetchall()
    return rows


def _get_one_approval(request_id: str) -> dict[str, Any] | None:
    """Fetch a single approval record by request_id."""
    with get_connection(str(DB_PATH)) as conn:
        conn.row_factory = lambda c, r: {
            c.description[i][0]: r[i] for i in range(len(r))
        }
        row = conn.execute(
            "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
        ).fetchone()
    return row


def _write_decision(
    request_id: str,
    status: str,
    decided_by: str,
    note: str,
) -> bool:
    """
    Write an approval decision to the DB.

    Returns True on success. Does NOT use ApprovalManager to avoid
    reconstructing the full broker stack just for a CLI write, but
    writes directly to the same approvals table with the same schema.
    """
    now = _now_iso()
    with get_connection(str(DB_PATH)) as conn:
        cursor = conn.execute(
            """
            UPDATE approvals
               SET status       = ?,
                   decided_at   = ?,
                   decided_by   = ?,
                   decision_note = ?
             WHERE request_id   = ?
               AND status       = 'pending'
            """,
            (status, now, decided_by, note, request_id),
        )
        conn.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def cmd_approvals_list(args: argparse.Namespace) -> int:
    """List approval requests."""
    status_filter = getattr(args, "status", None) or None
    project_id = getattr(args, "project", None) or None

    rows = _query_approvals(status_filter=status_filter, project_id=project_id)

    if not rows:
        label = status_filter or "all"
        print(f"No {label} approval requests found.")
        return 0

    print(f"{'-' * 60}")
    print(f"  APPROVAL REQUESTS ({len(rows)} found)")
    print(f"{'-' * 60}")
    for row in rows:
        print(_fmt_row(row))
        print(f"{'-' * 60}")
    return 0


def cmd_approvals_show(args: argparse.Namespace) -> int:
    """Show details of one approval request."""
    row = _get_one_approval(args.request_id)
    if row is None:
        print(f"No approval found with ID: {args.request_id}", file=sys.stderr)
        return 1
    print(_fmt_row(row))
    return 0


def cmd_approvals_grant(args: argparse.Namespace) -> int:
    """Grant an approval request."""
    row = _get_one_approval(args.request_id)
    if row is None:
        print(f"No approval found with ID: {args.request_id}", file=sys.stderr)
        return 1
    if row.get("status") != "pending":
        print(
            f"Approval {args.request_id} is already {row['status']}. "
            "Only pending approvals can be granted.",
            file=sys.stderr,
        )
        return 1

    note = getattr(args, "note", "") or ""
    updated = _write_decision(args.request_id, "approved", "human-cli", note)
    if updated:
        print(
            f"Approval {args.request_id} GRANTED.\n"
            f"  Action:   {row.get('action_category', 'N/A')}\n"
            f"  Target:   {row.get('target', 'N/A')}\n"
            f"  Note:     {note or '(none)'}\n"
            f"  Resume:   re-run 'factory run' to requeue tasks parked on\n"
            f"            this approval. Nothing resumes automatically:\n"
            f"            status AND operation fingerprint are re-verified\n"
            f"            in the DB before any task is requeued."
        )
        return 0
    else:
        print(
            f"Failed to grant approval {args.request_id}. "
            "It may have already been decided or not exist.",
            file=sys.stderr,
        )
        return 1


def cmd_approvals_deny(args: argparse.Namespace) -> int:
    """Deny an approval request."""
    row = _get_one_approval(args.request_id)
    if row is None:
        print(f"No approval found with ID: {args.request_id}", file=sys.stderr)
        return 1
    if row.get("status") != "pending":
        print(
            f"Approval {args.request_id} is already {row['status']}. "
            "Only pending approvals can be denied.",
            file=sys.stderr,
        )
        return 1

    note = getattr(args, "note", "") or ""
    updated = _write_decision(args.request_id, "denied", "human-cli", note)
    if updated:
        print(
            f"Approval {args.request_id} DENIED.\n"
            f"  Action:   {row.get('action_category', 'N/A')}\n"
            f"  Target:   {row.get('target', 'N/A')}\n"
            f"  Note:     {note or '(none)'}\n"
            f"  Resume:   tasks parked on this approval stay parked; a\n"
            f"            later run will request a FRESH approval for any\n"
            f"            retry of this operation."
        )
        return 0
    else:
        print(
            f"Failed to deny approval {args.request_id}.",
            file=sys.stderr,
        )
        return 1


# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------

def register_approvals_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the 'approvals' command and its sub-subcommands."""
    approvals_parser = subparsers.add_parser(
        "approvals",
        help="Manage factory approval requests",
    )
    approvals_sub = approvals_parser.add_subparsers(
        dest="approvals_command", required=True,
    )

    # --- list ---
    list_p = approvals_sub.add_parser("list", help="List approval requests")
    list_p.add_argument(
        "--status",
        choices=["pending", "approved", "denied"],
        default=None,
        help="Filter by status (default: all)",
    )
    list_p.add_argument(
        "--project",
        default=None,
        metavar="PROJECT_ID",
        help="Filter by project ID",
    )

    # --- show ---
    show_p = approvals_sub.add_parser("show", help="Show one approval request")
    show_p.add_argument("request_id", help="Approval request ID")

    # --- grant ---
    grant_p = approvals_sub.add_parser("grant", help="Grant an approval request")
    grant_p.add_argument("request_id", help="Approval request ID")
    grant_p.add_argument("--note", default="", help="Optional decision note")

    # --- deny ---
    deny_p = approvals_sub.add_parser("deny", help="Deny an approval request")
    deny_p.add_argument("request_id", help="Approval request ID")
    deny_p.add_argument("--note", default="", help="Optional decision note")


def dispatch_approvals(args: argparse.Namespace) -> int:
    """Route to the correct approvals sub-command handler."""
    from factory.database import initialize_database
    initialize_database()

    cmd = args.approvals_command
    if cmd == "list":
        return cmd_approvals_list(args)
    elif cmd == "show":
        return cmd_approvals_show(args)
    elif cmd == "grant":
        return cmd_approvals_grant(args)
    elif cmd == "deny":
        return cmd_approvals_deny(args)
    else:
        print(f"Unknown approvals command: {cmd}", file=sys.stderr)
        return 1
