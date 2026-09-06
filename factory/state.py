"""
factory.state
=============
Factory kernel state machine.

``FactoryState`` holds the live runtime snapshot of the factory kernel:
which project is active, which tasks are in-flight, whether an emergency
stop has been triggered, etc.

``StateManager`` persists this snapshot to the database and reloads it on
boot, so that a clean restart can inspect actual workspace state before
deciding whether to continue.

Design note
-----------
The state machine intentionally does NOT trust stored state blindly.  On
boot, ``StateManager.boot_state()`` loads the last snapshot and then flags
any task that was marked RUNNING as PAUSED (unknown) — the RecoveryManager
is responsible for reconciling against the actual workspace.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.config import FACTORY_ROOT, get_settings
from factory.database import fetch_one, execute_write, get_connection, to_json
from factory.errors import StateError, EmergencyStopError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime state dataclass
# ---------------------------------------------------------------------------

@dataclass
class FactoryState:
    """
    Mutable, in-memory snapshot of the factory kernel's runtime state.

    Instances are created by StateManager; do not construct directly.
    """
    active_project_id: str | None = None
    active_task_ids: list[str] = field(default_factory=list)
    emergency_stop: bool = False
    paused: bool = False
    kernel_started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    last_checkpoint_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # Thread safety — the lock is NOT serialized to the DB
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict (without the lock)."""
        return {
            "active_project_id": self.active_project_id,
            "active_task_ids": list(self.active_task_ids),
            "emergency_stop": self.emergency_stop,
            "paused": self.paused,
            "kernel_started_at": self.kernel_started_at.isoformat(),
            "last_checkpoint_id": self.last_checkpoint_id,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FactoryState":
        """Reconstruct a FactoryState from a serialized dict."""
        started = data.get("kernel_started_at")
        started_dt: datetime
        if isinstance(started, str):
            started_dt = datetime.fromisoformat(started)
        else:
            started_dt = datetime.now(timezone.utc)

        return cls(
            active_project_id=data.get("active_project_id"),
            active_task_ids=list(data.get("active_task_ids", [])),
            emergency_stop=bool(data.get("emergency_stop", False)),
            paused=bool(data.get("paused", False)),
            kernel_started_at=started_dt,
            last_checkpoint_id=data.get("last_checkpoint_id"),
            extra=dict(data.get("extra", {})),
        )

    # ------------------------------------------------------------------
    # Guarded mutations
    # ------------------------------------------------------------------

    def assert_not_stopped(self) -> None:
        """Raise EmergencyStopError if the emergency stop flag is set."""
        if self.emergency_stop:
            raise EmergencyStopError(
                "Emergency stop is active — all dangerous operations are "
                "blocked until the stop is manually cleared."
            )

    def trigger_emergency_stop(self, reason: str = "") -> None:
        with self._lock:
            self.emergency_stop = True
            self.paused = True
            self.extra["emergency_stop_reason"] = reason
            self.extra["emergency_stop_at"] = datetime.now(timezone.utc).isoformat()
        logger.critical("EMERGENCY STOP triggered: %s", reason)

    def clear_emergency_stop(self) -> None:
        with self._lock:
            self.emergency_stop = False
            self.extra.pop("emergency_stop_reason", None)
            self.extra.pop("emergency_stop_at", None)
        logger.warning("Emergency stop cleared.")

    def pause(self) -> None:
        with self._lock:
            self.paused = True
        logger.info("Factory paused.")

    def resume(self) -> None:
        with self._lock:
            if self.emergency_stop:
                raise StateError(
                    "Cannot resume while emergency stop is active. "
                    "Clear the emergency stop first."
                )
            self.paused = False
        logger.info("Factory resumed.")

    def set_active_project(self, project_id: str | None) -> None:
        with self._lock:
            self.active_project_id = project_id

    def add_active_task(self, task_id: str) -> None:
        with self._lock:
            if task_id not in self.active_task_ids:
                self.active_task_ids.append(task_id)

    def remove_active_task(self, task_id: str) -> None:
        with self._lock:
            try:
                self.active_task_ids.remove(task_id)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# State manager
# ---------------------------------------------------------------------------

_STATE_KEY = "factory_kernel_state"


class StateManager:
    """
    Persists FactoryState snapshots to the DB and loads them on boot.

    The state is stored as a single JSON blob in a dedicated ``kernel_state``
    table (created lazily on first use).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        from factory.config import DB_PATH as _DB_PATH
        self._db_path = db_path or _DB_PATH
        self._ensure_table()

    def _ensure_table(self) -> None:
        with get_connection(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kernel_state (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)
            conn.commit()

    def save(self, state: FactoryState) -> None:
        """Persist the current state snapshot to the database."""
        now = datetime.now(timezone.utc).isoformat()
        data = to_json(state.to_dict())
        with get_connection(self._db_path) as conn:
            conn.execute("""
                INSERT INTO kernel_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """, (_STATE_KEY, data, now))
            conn.commit()
        logger.debug("FactoryState saved to DB.")

    def load(self) -> FactoryState | None:
        """Load the last persisted state snapshot, or None if none exists."""
        row = fetch_one(
            "SELECT value FROM kernel_state WHERE key = ?",
            (_STATE_KEY,),
            self._db_path,
        )
        if row is None:
            return None
        try:
            data = json.loads(row["value"])
            return FactoryState.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to load kernel state: %s", exc)
            return None

    def boot_state(self) -> FactoryState:
        """
        Load state for a fresh boot.

        Any task that was RUNNING at last save is considered UNKNOWN —
        the RecoveryManager will reconcile against the actual workspace.
        Tasks are removed from active_task_ids and must be re-queued or
        recovered after workspace inspection.
        """
        state = self.load()
        if state is None:
            logger.info("No previous kernel state found — starting fresh.")
            return FactoryState()

        if state.active_task_ids:
            logger.warning(
                "Boot: found %d in-flight task(s) from previous session. "
                "These will be reviewed by RecoveryManager before continuing: %s",
                len(state.active_task_ids),
                state.active_task_ids,
            )
            # Do NOT trust running state — flag for recovery
            state.extra["unresolved_task_ids"] = list(state.active_task_ids)
            state.active_task_ids = []
            state.paused = True  # hold until recovery clears

        if state.emergency_stop:
            logger.critical(
                "Boot: previous session ended with EMERGENCY STOP active. "
                "Reason: %s",
                state.extra.get("emergency_stop_reason", "unknown"),
            )

        return state

    def check_emergency_stop_file(self) -> bool:
        """
        Check whether the filesystem emergency-stop sentinel file exists.

        The file path is ``<FACTORY_ROOT>/<emergency_stop_file>`` from config.
        Returns True if the file exists (stop should be triggered).
        """
        settings = get_settings()
        stop_file = FACTORY_ROOT / settings.factory.emergency_stop_file
        return stop_file.exists()
