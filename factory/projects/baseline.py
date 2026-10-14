"""
factory.projects.baseline
==========================
Baseline capture — hashes the workspace before build starts.

A baseline snapshot records the SHA-256 hash of every file in the workspace
at a known-good point (usually just after intake, before any agent writes).
It is stored as a checkpoint via RecoveryManager so that post-build diff
and crash recovery can compare against it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
_SKIP_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".pyd", ".db", ".sqlite", ".sqlite3",
})
_MAX_BASELINE_FILES = 2000


class BaselineCapture:
    """
    Captures a cryptographic baseline of the project workspace.

    Usage::

        bc = BaselineCapture(db_path=Path("factory.db"))
        hashes = bc.capture(
            workspace=Path("/projects/my-project"),
            project_id="proj-123",
        )
        # hashes: {"src/main.py": "abc123...", ...}
    """

    def __init__(self, db_path: Path | None = None) -> None:
        from factory.config import DB_PATH
        self._db_path = db_path or DB_PATH

    def capture(
        self,
        workspace: Path,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        save_checkpoint: bool = True,
    ) -> dict[str, str]:
        """
        Hash all files in the workspace and optionally persist as a checkpoint.

        Parameters
        ----------
        workspace:
            Root directory to hash.
        project_id:
            Project context for the checkpoint.
        task_id:
            Task context for the checkpoint.
        save_checkpoint:
            If True, persist the hash dict as a RecoveryManager checkpoint.

        Returns
        -------
        dict[str, str]
            Mapping of relative path string → SHA-256 hex digest.
        """
        from factory.audit.evidence import hash_file

        workspace = workspace.resolve()
        if not workspace.exists() or not workspace.is_dir():
            logger.warning(
                "BaselineCapture: workspace %s does not exist.", workspace
            )
            return {}

        hashes: dict[str, str] = {}
        file_count = 0

        for file_path in sorted(workspace.rglob("*")):
            if not file_path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in file_path.parts):
                continue
            if file_path.suffix.lower() in _SKIP_EXTENSIONS:
                continue
            if file_count >= _MAX_BASELINE_FILES:
                logger.warning(
                    "BaselineCapture: file limit (%d) reached.", _MAX_BASELINE_FILES
                )
                break

            rel_path = str(file_path.relative_to(workspace))
            digest = hash_file(file_path)
            if digest:
                hashes[rel_path] = digest
                file_count += 1

        logger.info(
            "BaselineCapture: hashed %d files in %s.", len(hashes), workspace
        )

        if save_checkpoint and hashes:
            self._save_checkpoint(hashes, project_id=project_id, task_id=task_id,
                                  workspace=workspace)

        return hashes

    def diff(
        self,
        baseline: dict[str, str],
        workspace: Path,
    ) -> dict[str, Any]:
        """
        Compare a baseline hash dict against the current workspace state.

        Returns a dict with:
        - added: files present now but not in baseline
        - removed: files in baseline but not now
        - modified: files where hash changed
        - unchanged: files with identical hashes
        """
        from factory.audit.evidence import hash_file

        workspace = workspace.resolve()
        current: dict[str, str] = {}

        for file_path in sorted(workspace.rglob("*")):
            if not file_path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in file_path.parts):
                continue
            if file_path.suffix.lower() in _SKIP_EXTENSIONS:
                continue
            rel = str(file_path.relative_to(workspace))
            current[rel] = hash_file(file_path)

        baseline_keys = set(baseline)
        current_keys = set(current)

        added = sorted(current_keys - baseline_keys)
        removed = sorted(baseline_keys - current_keys)
        modified = sorted(
            k for k in baseline_keys & current_keys
            if baseline[k] != current[k]
        )
        unchanged = sorted(
            k for k in baseline_keys & current_keys
            if baseline[k] == current[k]
        )

        return {
            "added": added,
            "removed": removed,
            "modified": modified,
            "unchanged": unchanged,
            "summary": {
                "added": len(added),
                "removed": len(removed),
                "modified": len(modified),
                "unchanged": len(unchanged),
            },
        }

    def _save_checkpoint(
        self,
        hashes: dict[str, str],
        *,
        project_id: str | None,
        task_id: str | None,
        workspace: Path,
    ) -> str:
        """Persist the baseline as a recovery checkpoint."""
        try:
            from factory.orchestration.recovery import RecoveryManager
            rm = RecoveryManager(db_path=self._db_path)
            checkpoint_id = rm.save_checkpoint(
                {"baseline_hash_count": len(hashes)},
                project_id=project_id,
                task_id=task_id,
                workspace_paths=None,  # hashes are in orchestrator_state
            )
            # Also store hashes directly in the checkpoint by updating it
            from factory.database import get_connection
            import json
            with get_connection(self._db_path) as conn:
                conn.execute(
                    "UPDATE checkpoints SET workspace_hashes = ? WHERE checkpoint_id = ?",
                    (json.dumps(hashes), checkpoint_id),
                )
                conn.commit()
            logger.info(
                "BaselineCapture: saved checkpoint %s with %d hashes.",
                checkpoint_id, len(hashes),
            )
            return checkpoint_id
        except Exception as exc:
            logger.warning("BaselineCapture: could not save checkpoint: %s", exc)
            return ""
