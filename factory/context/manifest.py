"""
factory.context.manifest
========================
Data structures for context manifests.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from factory.database import get_connection, to_json


@dataclass
class ContextManifest:
    """A bounded snapshot of the context provided to an LLM."""
    manifest_id: str
    task_id: str
    created_at: datetime
    system_prompt: str
    messages: list[dict[str, str]] = field(default_factory=list)
    included_files: list[str] = field(default_factory=list)
    token_estimate: int = 0


class ContextPersister:
    """Saves context manifests to the database."""

    def __init__(self, db_path: str | None = None) -> None:
        from factory.config import DB_PATH
        self._db_path = db_path or DB_PATH

    def save(self, manifest: ContextManifest) -> None:
        with get_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO context_manifests (
                    manifest_id, task_id, created_at,
                    system_prompt, messages, included_files, token_estimate
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.manifest_id,
                    manifest.task_id,
                    manifest.created_at.isoformat(),
                    manifest.system_prompt,
                    to_json(manifest.messages),
                    to_json(manifest.included_files),
                    manifest.token_estimate,
                )
            )
            conn.commit()
