"""
factory.permissions.approvals
==============================
Scoped, persistent, auditable approval lifecycle manager.

Design principles
-----------------
* **Approval requests explain everything**: every request carries what is
  happening, why, the target, the expected effect, the risk level, and the
  consequence if denied.
* **Approvals are scoped**: each approval is anchored by an ``action_category``
  and a ``scope_key`` (a deterministic string derived from the action).
* **Inheritance**: a subsequent action may re-use a prior APPROVED decision
  *only when* both its ``action_category`` and ``scope_key`` fall within the
  same scope AND its risk level does not exceed the approved risk ceiling.
  Materially different or higher-risk actions require a fresh approval.
* **Persistence**: decisions are written to the ``approvals`` table that
  ``factory.database`` already creates. An in-memory index is rebuilt on first
  use and kept in sync during the manager's lifetime.
* **Auditability**: every state transition (request, approve, deny, inherit)
  emits a structured audit event via ``AuditLogger``.
* **Restart safety**: decisions survive restart because they are stored in
  the DB. On boot the manager reloads all APPROVED decisions into its
  inheritance index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.models import ApprovalDecision, ApprovalRequest, ApprovalStatus, RiskLevel

logger = logging.getLogger(__name__)

# Ordered risk levels for ceiling comparison — stored as plain value strings
_RISK_ORDER: list[str] = [
    RiskLevel.SAFE.value,
    RiskLevel.LOW.value,
    RiskLevel.MEDIUM.value,
    RiskLevel.HIGH.value,
    RiskLevel.CRITICAL.value,
]


def _risk_index(level: RiskLevel | str) -> int:
    """Return the ordinal index of a RiskLevel (higher = riskier)."""
    # Accept both enum members and raw value strings (e.g. "high")
    if isinstance(level, RiskLevel):
        s = level.value
    else:
        s = str(level)
    try:
        return _RISK_ORDER.index(s)
    except ValueError:
        return len(_RISK_ORDER) + 1  # unknown → strictly beyond CRITICAL


def _make_scope_key(action_category: str, target: str) -> str:
    """
    Produce a deterministic scope key from category + target.

    The key is a short hex digest so it is safe to store in SQLite and index.
    """
    raw = f"{action_category.lower()}::{target.lower()}"
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]


# ---------------------------------------------------------------------------
# In-memory inheritance index entry
# ---------------------------------------------------------------------------

class _ScopeEntry:
    """Tracks one approved scope for fast inheritance lookup."""

    __slots__ = (
        "request_id",
        "action_category",
        "scope_key",
        "approved_risk_index",
        "grants_scope_inheritance",
    )

    def __init__(
        self,
        *,
        request_id: str,
        action_category: str,
        scope_key: str,
        approved_risk_index: int,
        grants_scope_inheritance: bool,
    ) -> None:
        self.request_id = request_id
        self.action_category = action_category
        self.scope_key = scope_key
        self.approved_risk_index = approved_risk_index
        self.grants_scope_inheritance = grants_scope_inheritance

    def covers(self, action_category: str, scope_key: str, risk_index: int) -> bool:
        """
        Return True if this scope entry covers the given action.

        Conditions that must ALL hold:
        1. The action category matches (exact).
        2. The scope_key matches (same deterministic hash → same target+category).
        3. The requested risk index does not exceed the approved ceiling.
        4. This approval grants scope inheritance.
        """
        return (
            self.grants_scope_inheritance
            and self.action_category == action_category
            and self.scope_key == scope_key
            and risk_index <= self.approved_risk_index
        )


# ---------------------------------------------------------------------------
# ApprovalManager
# ---------------------------------------------------------------------------

class ApprovalManager:
    """
    Lifecycle manager for human-approval requests.

    Usage::

        mgr = ApprovalManager()

        # Create a new pending request
        req = mgr.request_approval(
            action="git commit -m 'add feature'",
            target="repo:main",
            reason="Committing generated code",
            expected_effect="Creates a new commit on main",
            risk_level=RiskLevel.HIGH,
            consequence_if_denied="Changes remain uncommitted",
            action_category="git.commit",
        )

        # Check for an existing inherited approval first
        inherited = mgr.find_inherited(
            action_category="git.commit",
            target="repo:main",
            risk_level=RiskLevel.HIGH,
        )
        if inherited:
            # No fresh approval needed
            pass

        # Record a human decision
        decision = ApprovalDecision(
            request_id=req.request_id,
            status=ApprovalStatus.APPROVED,
            grants_scope_inheritance=True,
            scope_key=req.scope_key,
        )
        mgr.record_decision(decision)
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        from factory.config import DB_PATH as _DB_PATH

        self._db_path = db_path or _DB_PATH
        self._project_id = project_id
        self._agent_id = agent_id

        # In-memory scope index: scope_key → list[_ScopeEntry]
        self._scope_index: dict[str, list[_ScopeEntry]] = {}
        self._lock = threading.Lock()

        # Lazy index — built on first use
        self._index_loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_approval(
        self,
        *,
        action: str,
        target: str,
        reason: str,
        expected_effect: str,
        risk_level: RiskLevel,
        consequence_if_denied: str,
        action_category: str = "",
        task_id: str | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """
        Create and persist a new PENDING ApprovalRequest.

        Returns the persisted request. Does NOT block — the caller is
        responsible for checking the status before proceeding.
        """
        scope_key = _make_scope_key(action_category or action, target)

        req = ApprovalRequest(
            project_id=project_id or self._project_id,
            task_id=task_id,
            agent_id=agent_id or self._agent_id,
            action=action,
            target=target,
            reason=reason,
            expected_effect=expected_effect,
            risk_level=risk_level,
            consequence_if_denied=consequence_if_denied,
            action_category=action_category,
            scope_key=scope_key,
            status=ApprovalStatus.PENDING,
        )

        self._persist_request(req, extra_meta=metadata)
        self._audit("approval.requested", req.request_id, {
            "action": action,
            "target": target,
            "risk_level": risk_level.value if isinstance(risk_level, RiskLevel) else str(risk_level),
            "action_category": action_category,
            "scope_key": scope_key,
        })
        logger.info(
            "Approval requested: %s | category=%s | risk=%s | id=%s",
            action, action_category, risk_level, req.request_id,
        )
        return req

    def record_decision(self, decision: ApprovalDecision) -> None:
        """
        Persist a human decision (APPROVED or DENIED) for a request.

        If approved with ``grants_scope_inheritance=True``, the scope entry
        is added to the in-memory index for future inheritance checks.
        """
        self._ensure_index()
        self._persist_decision(decision)

        if decision.status == ApprovalStatus.APPROVED:
            self._audit("approval.approved", decision.request_id, {
                "decided_by": decision.decided_by,
                "grants_scope_inheritance": decision.grants_scope_inheritance,
                "scope_key": decision.scope_key,
                "note": decision.decision_note,
            })
            if decision.grants_scope_inheritance and decision.scope_key:
                req = self.get_request(decision.request_id)
                if req is not None:
                    entry = _ScopeEntry(
                        request_id=decision.request_id,
                        action_category=req.action_category,
                        scope_key=decision.scope_key,
                        approved_risk_index=_risk_index(req.risk_level),
                        grants_scope_inheritance=True,
                    )
                    with self._lock:
                        self._scope_index.setdefault(decision.scope_key, []).append(entry)
                    logger.debug(
                        "Scope inheritance granted for scope_key=%s | category=%s",
                        decision.scope_key, req.action_category,
                    )
        else:
            self._audit("approval.denied", decision.request_id, {
                "decided_by": decision.decided_by,
                "note": decision.decision_note,
            })

        logger.info(
            "Approval decision recorded: request_id=%s status=%s",
            decision.request_id, decision.status,
        )

    def find_inherited(
        self,
        *,
        action_category: str,
        target: str,
        risk_level: RiskLevel,
    ) -> ApprovalRequest | None:
        """
        Return the first active inherited approval that covers this action,
        or ``None`` if no valid inherited approval exists.

        An inherited approval is valid when:
        - The previously approved action_category matches exactly.
        - The scope_key (derived from category + target) matches.
        - The risk level does not exceed the approved ceiling.
        - The original decision had ``grants_scope_inheritance=True``.

        Materially different actions (different category or target) or
        higher-risk actions always return ``None`` — fresh approval required.
        """
        self._ensure_index()
        scope_key = _make_scope_key(action_category, target)
        risk_idx = _risk_index(risk_level)

        with self._lock:
            entries = self._scope_index.get(scope_key, [])
            for entry in entries:
                if entry.covers(action_category, scope_key, risk_idx):
                    req = self.get_request(entry.request_id)
                    if req is not None:
                        self._audit("approval.inherited", entry.request_id, {
                            "action_category": action_category,
                            "target": target,
                            "risk_level": str(risk_level),
                            "scope_key": scope_key,
                        })
                        logger.debug(
                            "Approval inherited from request_id=%s for category=%s",
                            entry.request_id, action_category,
                        )
                        return req
        return None

    def get_request(self, request_id: str) -> ApprovalRequest | None:
        """Fetch a single ApprovalRequest by ID from the database."""
        from factory.database import get_connection
        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_request(dict(row))

    def list_pending(
        self, *, project_id: str | None = None
    ) -> list[ApprovalRequest]:
        """Return all PENDING approval requests, optionally filtered by project."""
        return self._list_by_status(ApprovalStatus.PENDING, project_id=project_id)

    def list_approved(
        self, *, project_id: str | None = None
    ) -> list[ApprovalRequest]:
        """Return all APPROVED approval requests."""
        return self._list_by_status(ApprovalStatus.APPROVED, project_id=project_id)

    def is_approved(
        self,
        *,
        action_category: str,
        target: str,
        risk_level: RiskLevel,
    ) -> bool:
        """
        Convenience method: return True if a valid inherited approval exists.

        Equivalent to ``find_inherited(...) is not None``.
        """
        return self.find_inherited(
            action_category=action_category,
            target=target,
            risk_level=risk_level,
        ) is not None

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _persist_request(
        self, req: ApprovalRequest, extra_meta: dict[str, Any] | None = None
    ) -> None:
        """Insert a new ApprovalRequest row into the DB."""
        from factory.database import get_connection
        rl_val = req.risk_level.value if isinstance(req.risk_level, RiskLevel) else str(req.risk_level)
        st_val = req.status.value if isinstance(req.status, ApprovalStatus) else str(req.status)
        with get_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    request_id, project_id, task_id, agent_id,
                    action, target, reason, expected_effect,
                    risk_level, consequence_if_denied,
                    action_category, scope_key,
                    requested_at, status,
                    decided_at, decided_by, decision_note,
                    grants_scope_inheritance
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?
                )
                """,
                (
                    req.request_id,
                    req.project_id,
                    req.task_id,
                    req.agent_id,
                    req.action,
                    req.target,
                    req.reason,
                    req.expected_effect,
                    rl_val,
                    req.consequence_if_denied,
                    req.action_category,
                    req.scope_key,
                    req.requested_at.isoformat(),
                    st_val,
                    req.decided_at.isoformat() if req.decided_at else None,
                    req.decided_by,
                    req.decision_note,
                    0,
                ),
            )
            conn.commit()

    def _persist_decision(self, decision: ApprovalDecision) -> None:
        """Update the DB row for the given request with the decision outcome."""
        from factory.database import get_connection
        st_val = decision.status.value if isinstance(decision.status, ApprovalStatus) else str(decision.status)
        with get_connection(self._db_path) as conn:
            conn.execute(
                """
                UPDATE approvals
                   SET status                  = ?,
                       decided_at              = ?,
                       decided_by              = ?,
                       decision_note           = ?,
                       grants_scope_inheritance = ?,
                       scope_key               = ?
                 WHERE request_id = ?
                """,
                (
                    st_val,
                    decision.decided_at.isoformat(),
                    decision.decided_by,
                    decision.decision_note,
                    1 if decision.grants_scope_inheritance else 0,
                    decision.scope_key,
                    decision.request_id,
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Scope index bootstrap
    # ------------------------------------------------------------------

    def _ensure_index(self) -> None:
        """Load approved scope entries from DB into the in-memory index."""
        with self._lock:
            if self._index_loaded:
                return
            self._load_scope_index()
            self._index_loaded = True

    def _load_scope_index(self) -> None:
        """
        Rebuild the in-memory scope index from the DB.

        Called once on first use; subsequent updates are applied in-place
        by ``record_decision()``.
        """
        from factory.database import get_connection
        try:
            with get_connection(self._db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT request_id, action_category, scope_key,
                           risk_level, grants_scope_inheritance
                      FROM approvals
                     WHERE status = 'approved'
                       AND grants_scope_inheritance = 1
                    """
                ).fetchall()
        except Exception as exc:  # DB might not exist yet in tests
            logger.debug("Could not load scope index from DB: %s", exc)
            return

        for row in rows:
            r = dict(row)
            entry = _ScopeEntry(
                request_id=r["request_id"],
                action_category=r["action_category"] or "",
                scope_key=r["scope_key"] or "",
                approved_risk_index=_risk_index(r["risk_level"]),
                grants_scope_inheritance=bool(r["grants_scope_inheritance"]),
            )
            if entry.scope_key:
                self._scope_index.setdefault(entry.scope_key, []).append(entry)

        logger.debug(
            "Scope index loaded: %d scope key(s) with inheritance grants.",
            len(self._scope_index),
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def _list_by_status(
        self, status: ApprovalStatus, *, project_id: str | None = None
    ) -> list[ApprovalRequest]:
        from factory.database import get_connection
        st_val = status.value if isinstance(status, ApprovalStatus) else str(status)
        params: list[Any] = [st_val]
        sql = "SELECT * FROM approvals WHERE status = ?"
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY requested_at DESC"
        with get_connection(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_request(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Model mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_request(row: dict[str, Any]) -> ApprovalRequest:
        """Convert a raw DB row dict to an ApprovalRequest."""
        def _parse_dt(val: str | None) -> datetime | None:
            if val is None:
                return None
            try:
                return datetime.fromisoformat(val)
            except (ValueError, TypeError):
                return None

        requested_at_raw = row.get("requested_at") or datetime.now(timezone.utc).isoformat()
        try:
            requested_at = datetime.fromisoformat(requested_at_raw)
        except (ValueError, TypeError):
            requested_at = datetime.now(timezone.utc)

        return ApprovalRequest(
            request_id=row["request_id"],
            project_id=row.get("project_id"),
            task_id=row.get("task_id"),
            agent_id=row.get("agent_id"),
            action=row.get("action", ""),
            target=row.get("target", ""),
            reason=row.get("reason", ""),
            expected_effect=row.get("expected_effect", ""),
            risk_level=RiskLevel(row.get("risk_level", "medium")),
            consequence_if_denied=row.get("consequence_if_denied", ""),
            action_category=row.get("action_category", ""),
            scope_key=row.get("scope_key", ""),
            requested_at=requested_at,
            status=ApprovalStatus(row.get("status", "pending")),
            decided_at=_parse_dt(row.get("decided_at")),
            decided_by=row.get("decided_by", "human"),
            decision_note=row.get("decision_note", ""),
        )

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------

    def _audit(
        self,
        event_type: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Emit a structured audit event without hard-failing on errors."""
        try:
            from factory.audit.logger import AuditLogger
            AuditLogger(
                project_id=self._project_id,
                agent_id=self._agent_id,
                db_path=self._db_path,
            ).approval(event_type, request_id, payload)
        except Exception as exc:  # never let audit failure crash execution
            logger.warning("Audit emit failed for %s: %s", event_type, exc)
