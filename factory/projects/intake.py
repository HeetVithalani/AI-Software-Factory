"""
factory.projects.intake
========================
Project intake — validates and registers new projects.

The intake phase:
1. Validates project name, description, workspace path.
2. Creates the project workspace directory.
3. Persists a ProjectRecord to the database.
4. Returns the created ProjectRecord.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from factory.models import ProjectRecord, ProjectStatus, QualityProfile
from factory.errors import ProjectError

logger = logging.getLogger(__name__)

# Valid project name: letters, digits, hyphens, underscores
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")


class IntakeError(ProjectError):
    """Raised when project intake validation fails."""
    code = "INTAKE_ERROR"


def _validate_name(name: str) -> None:
    if not name or not name.strip():
        raise IntakeError("Project name cannot be empty.", project_id=None)  # type: ignore[arg-type]
    if not _NAME_PATTERN.match(name):
        raise IntakeError(
            f"Invalid project name {name!r}. "
            "Use letters, digits, hyphens, underscores (1–64 chars, start with alphanumeric).",
            project_id=None,  # type: ignore[arg-type]
        )


def _validate_workspace(path: Path) -> None:
    if path.exists() and not path.is_dir():
        raise IntakeError(
            f"Workspace path {path!r} exists but is not a directory."
        )


class ProjectIntake:
    """
    Validates and registers new projects.

    Usage::

        intake = ProjectIntake(db_path=Path("factory.db"))
        record = intake.register(
            name="my-project",
            description="Build a REST API",
            workspace_root=Path("/projects/my-project"),
        )
    """

    def __init__(self, db_path: Path | None = None) -> None:
        from factory.config import DB_PATH
        self._db_path = db_path or DB_PATH

    def register(
        self,
        name: str,
        description: str = "",
        *,
        workspace_root: Path | None = None,
        quality_profile: QualityProfile = QualityProfile.BALANCED,
        metadata: dict[str, Any] | None = None,
        create_workspace: bool = True,
    ) -> ProjectRecord:
        """
        Validate and register a new project.

        Parameters
        ----------
        name:
            Project identifier. Must match [a-zA-Z0-9_-]{1,64}.
        description:
            Human-readable project description.
        workspace_root:
            Filesystem path for the project workspace.
            Created if it does not exist (when create_workspace=True).
        quality_profile:
            FAST, BALANCED, or HARDENED — controls agent aggressiveness.
        metadata:
            Additional project metadata.
        create_workspace:
            If True, create the workspace directory.

        Returns
        -------
        ProjectRecord
            The persisted project record.
        """
        name = name.strip()
        _validate_name(name)

        workspace_path = ""
        if workspace_root is not None:
            workspace_root = workspace_root.resolve()
            _validate_workspace(workspace_root)
            workspace_path = str(workspace_root)

            if create_workspace and not workspace_root.exists():
                workspace_root.mkdir(parents=True, exist_ok=True)
                logger.info("ProjectIntake: created workspace at %s", workspace_root)

        record = ProjectRecord(
            name=name,
            description=description,
            status=ProjectStatus.INTAKE,
            quality_profile=quality_profile,
            workspace_path=workspace_path,
            metadata=metadata or {},
        )

        self._persist(record)

        logger.info(
            "ProjectIntake: registered project %r (id=%s, workspace=%s).",
            name, record.project_id, workspace_path or "none",
        )
        return record

    def _persist(self, record: ProjectRecord) -> None:
        """Insert the project record into the DB."""
        import json
        from factory.database import get_connection

        with get_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO projects (
                    project_id, name, description, status, quality_profile,
                    created_at, updated_at, workspace_path, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.project_id,
                    record.name,
                    record.description,
                    record.status if isinstance(record.status, str) else record.status.value,
                    record.quality_profile if isinstance(record.quality_profile, str) else record.quality_profile.value,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    record.workspace_path,
                    json.dumps(record.metadata),
                ),
            )
            conn.commit()

    def load(self, project_id: str) -> ProjectRecord | None:
        """Load a project by ID. Returns None if not found."""
        import json
        from factory.database import get_connection
        from factory.models import ProjectStatus as PS, QualityProfile as QP
        from datetime import datetime

        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()

        if row is None:
            return None

        r = dict(row)
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(r.get("metadata", "{}"))
        except (json.JSONDecodeError, TypeError):
            pass

        return ProjectRecord(
            project_id=r["project_id"],
            name=r["name"],
            description=r.get("description", ""),
            status=r.get("status", PS.INTAKE),
            quality_profile=r.get("quality_profile", QP.BALANCED),
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
            workspace_path=r.get("workspace_path", ""),
            metadata=meta,
        )

    def update_status(self, project_id: str, status: ProjectStatus) -> None:
        """Update a project's status in the DB."""
        from factory.database import get_connection
        from datetime import datetime, timezone
        st = status.value if hasattr(status, "value") else str(status)
        now = datetime.now(timezone.utc).isoformat()
        with get_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE projects SET status = ?, updated_at = ? WHERE project_id = ?",
                (st, now, project_id),
            )
            conn.commit()

    def update_profile(
        self, project_id: str, profile: QualityProfile
    ) -> None:
        """Update a project's quality profile in the DB."""
        from factory.database import get_connection
        from datetime import datetime, timezone
        value = profile.value if hasattr(profile, "value") else str(profile)
        now = datetime.now(timezone.utc).isoformat()
        with get_connection(self._db_path) as conn:
            conn.execute(
                "UPDATE projects SET quality_profile = ?, updated_at = ? "
                "WHERE project_id = ?",
                (value, now, project_id),
            )
            conn.commit()

    def find_by_workspace(self, workspace_root: Path) -> ProjectRecord | None:
        """
        Find the project registered for an exact workspace path.

        Comparison uses the resolved path string, the same identity the
        skills provider derives its project key from.
        """
        from factory.database import get_connection

        candidate = str(Path(workspace_root).resolve())
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT project_id FROM projects WHERE workspace_path = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (candidate,),
            ).fetchone()
        if row is None:
            return None
        return self.load(row["project_id"])

    def latest_project(self) -> ProjectRecord | None:
        """Return the most recently updated project, if any."""
        from factory.database import get_connection

        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT project_id FROM projects ORDER BY updated_at DESC "
                "LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return self.load(row["project_id"])
