"""
factory.audit.reports
=====================
Generates structured audit reports for a project.

Reports aggregate the full event trail, evidence, approval decisions,
task outcomes, and known failures into a single JSON-serializable summary.
The final auditor agent uses these reports as input.

Design note
-----------
Reports are generated deterministically from the database — they are NOT
AI-generated summaries. AI interpretation happens at the agent layer on
top of this structured data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.database import get_connection

logger = logging.getLogger(__name__)


def generate_summary_report(
    project_id: str,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Generate a structured JSON-serializable audit summary for a project.

    The report includes:
    - project metadata
    - all tasks with their final status
    - all events (newest first, capped at 1000)
    - all approval decisions
    - all evidence records
    - open/unresolved issues
    """
    from factory.config import DB_PATH as _DB_PATH
    path = db_path or _DB_PATH

    with get_connection(path) as conn:
        # Project
        project_row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()

        # Tasks
        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        # Events
        event_rows = conn.execute(
            """
            SELECT * FROM events WHERE project_id = ?
            ORDER BY id DESC LIMIT 1000
            """,
            (project_id,),
        ).fetchall()

        # Approvals
        approval_rows = conn.execute(
            "SELECT * FROM approvals WHERE project_id = ? ORDER BY requested_at",
            (project_id,),
        ).fetchall()

        # Evidence
        evidence_rows = conn.execute(
            "SELECT * FROM evidence WHERE project_id = ? ORDER BY recorded_at",
            (project_id,),
        ).fetchall()

    def _row_to_dict(row: Any) -> dict[str, Any]:
        d = dict(row)
        for k, v in d.items():
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, (dict, list)):
                        d[k] = parsed
                except (json.JSONDecodeError, ValueError):
                    pass
        return d

    tasks = [_row_to_dict(r) for r in task_rows]
    failed_tasks = [t for t in tasks if t.get("status") == "failed"]
    completed_tasks = [t for t in tasks if t.get("status") == "completed"]
    pending_approvals = [
        _row_to_dict(r) for r in approval_rows
        if r["status"] == "pending"
    ]

    report: dict[str, Any] = {
        "report_generated_at": datetime.now(timezone.utc).isoformat(),
        "project": _row_to_dict(project_row) if project_row else {},
        "summary": {
            "total_tasks": len(tasks),
            "completed_tasks": len(completed_tasks),
            "failed_tasks": len(failed_tasks),
            "pending_tasks": len([t for t in tasks if t.get("status") == "pending"]),
            "total_events": len(event_rows),
            "total_evidence_records": len(evidence_rows),
            "pending_approvals": len(pending_approvals),
        },
        "tasks": tasks,
        "events": [_row_to_dict(r) for r in event_rows],
        "approvals": [_row_to_dict(r) for r in approval_rows],
        "evidence": [_row_to_dict(r) for r in evidence_rows],
        "open_issues": {
            "failed_tasks": failed_tasks,
            "pending_approvals": pending_approvals,
        },
    }

    logger.info(
        "Audit report generated for project %s: %d tasks, %d events.",
        project_id, len(tasks), len(event_rows),
    )
    return report
