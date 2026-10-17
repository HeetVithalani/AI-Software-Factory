"""
tests/unit/test_permissions.py
================================
Deterministic unit tests for the factory.permissions subsystem.

Covers:
- ApprovalManager: creation, decision, scope matching, risk ceiling,
  inherited approval, rejection, expired/invalid
- PermissionBroker: full pipeline decisions
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import pytest

from factory.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)
from factory.permissions.approvals import (
    ApprovalManager,
    _make_scope_key,
    _risk_index,
    make_operation_fingerprint,
)
from factory.permissions.broker import (
    PermissionBroker,
    PermissionDecision,
    PermissionOutcome,
)
from factory.permissions.policies import PolicySet
from factory.permissions.risk import ActionContext, RiskEngine


# ---------------------------------------------------------------------------
# Fixtures — use an in-memory SQLite DB for isolation
# ---------------------------------------------------------------------------

def _make_db() -> Path:
    """Create a temp SQLite file with the approvals table and events table."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            request_id            TEXT PRIMARY KEY,
            project_id            TEXT,
            task_id               TEXT,
            agent_id              TEXT,
            action                TEXT NOT NULL,
            target                TEXT NOT NULL,
            reason                TEXT NOT NULL,
            expected_effect       TEXT NOT NULL DEFAULT '',
            risk_level            TEXT NOT NULL,
            consequence_if_denied TEXT NOT NULL DEFAULT '',
            action_category       TEXT NOT NULL DEFAULT '',
            scope_key             TEXT NOT NULL DEFAULT '',
            operation_fingerprint TEXT,
            requested_at          TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'pending',
            decided_at            TEXT,
            decided_by            TEXT NOT NULL DEFAULT 'human',
            decision_note         TEXT NOT NULL DEFAULT '',
            grants_scope_inheritance INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            event_type  TEXT    NOT NULL,
            project_id  TEXT,
            task_id     TEXT,
            agent_id    TEXT,
            payload     TEXT    NOT NULL DEFAULT '{}'
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """Provide a fresh isolated DB file for each test."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            request_id            TEXT PRIMARY KEY,
            project_id            TEXT,
            task_id               TEXT,
            agent_id              TEXT,
            action                TEXT NOT NULL,
            target                TEXT NOT NULL,
            reason                TEXT NOT NULL,
            expected_effect       TEXT NOT NULL DEFAULT '',
            risk_level            TEXT NOT NULL,
            consequence_if_denied TEXT NOT NULL DEFAULT '',
            action_category       TEXT NOT NULL DEFAULT '',
            scope_key             TEXT NOT NULL DEFAULT '',
            operation_fingerprint TEXT,
            requested_at          TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'pending',
            decided_at            TEXT,
            decided_by            TEXT NOT NULL DEFAULT 'human',
            decision_note         TEXT NOT NULL DEFAULT '',
            grants_scope_inheritance INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            event_type  TEXT    NOT NULL,
            project_id  TEXT,
            task_id     TEXT,
            agent_id    TEXT,
            payload     TEXT    NOT NULL DEFAULT '{}'
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mgr(db: Path) -> ApprovalManager:
    return ApprovalManager(db_path=db)


@pytest.fixture
def broker(db: Path) -> PermissionBroker:
    return PermissionBroker(db_path=db)


# ===========================================================================
# Utility function tests
# ===========================================================================

class TestScopeKey:
    def test_deterministic(self):
        k1 = _make_scope_key("git.commit", "repo:main")
        k2 = _make_scope_key("git.commit", "repo:main")
        assert k1 == k2

    def test_different_category_different_key(self):
        k1 = _make_scope_key("git.commit", "repo:main")
        k2 = _make_scope_key("git.push", "repo:main")
        assert k1 != k2

    def test_different_target_different_key(self):
        k1 = _make_scope_key("git.commit", "repo:main")
        k2 = _make_scope_key("git.commit", "repo:develop")
        assert k1 != k2

    def test_case_insensitive(self):
        k1 = _make_scope_key("Git.Commit", "Repo:Main")
        k2 = _make_scope_key("git.commit", "repo:main")
        assert k1 == k2

    def test_fixed_length(self):
        k = _make_scope_key("anything", "anywhere")
        assert len(k) == 16  # sha1 truncated to 16 hex chars


class TestRiskIndex:
    def test_ordering(self):
        assert _risk_index(RiskLevel.SAFE) < _risk_index(RiskLevel.LOW)
        assert _risk_index(RiskLevel.LOW) < _risk_index(RiskLevel.MEDIUM)
        assert _risk_index(RiskLevel.MEDIUM) < _risk_index(RiskLevel.HIGH)
        assert _risk_index(RiskLevel.HIGH) < _risk_index(RiskLevel.CRITICAL)

    def test_unknown_level_is_highest(self):
        assert _risk_index("unknown_level") > _risk_index(RiskLevel.CRITICAL)


# ===========================================================================
# ApprovalManager — creation
# ===========================================================================

class TestApprovalCreation:
    def test_request_returns_pending(self, mgr: ApprovalManager):
        req = mgr.request_approval(
            action="git commit -m 'init'",
            target="repo:main",
            reason="Commit generated code",
            expected_effect="New commit on main branch",
            risk_level=RiskLevel.HIGH,
            consequence_if_denied="Code stays uncommitted",
            action_category="git.commit",
        )
        assert req.status == ApprovalStatus.PENDING
        assert req.action == "git commit -m 'init'"
        assert req.risk_level == RiskLevel.HIGH
        assert req.action_category == "git.commit"
        assert len(req.scope_key) == 16

    def test_request_persisted_in_db(self, mgr: ApprovalManager, db: Path):
        req = mgr.request_approval(
            action="git push origin main",
            target="remote:origin",
            reason="Push changes",
            expected_effect="Pushes to remote",
            risk_level=RiskLevel.HIGH,
            consequence_if_denied="Changes won't reach remote",
            action_category="git.push",
        )
        fetched = mgr.get_request(req.request_id)
        assert fetched is not None
        assert fetched.request_id == req.request_id
        assert fetched.action == "git push origin main"

    def test_request_has_unique_id(self, mgr: ApprovalManager):
        req1 = mgr.request_approval(
            action="git commit -m 'a'",
            target="repo:main",
            reason="Commit A",
            expected_effect="Commit A",
            risk_level=RiskLevel.HIGH,
            consequence_if_denied="Nothing",
            action_category="git.commit",
        )
        req2 = mgr.request_approval(
            action="git commit -m 'b'",
            target="repo:main",
            reason="Commit B",
            expected_effect="Commit B",
            risk_level=RiskLevel.HIGH,
            consequence_if_denied="Nothing",
            action_category="git.commit",
        )
        assert req1.request_id != req2.request_id

    def test_list_pending(self, mgr: ApprovalManager):
        mgr.request_approval(
            action="shell cmd",
            target="localhost",
            reason="Run something",
            expected_effect="Side effects",
            risk_level=RiskLevel.MEDIUM,
            consequence_if_denied="Won't run",
            action_category="shell",
        )
        pending = mgr.list_pending()
        assert len(pending) >= 1
        assert all(p.status == ApprovalStatus.PENDING for p in pending)


# ===========================================================================
# ApprovalManager — decision
# ===========================================================================

class TestApprovalDecision:
    def _create_request(self, mgr: ApprovalManager, **kwargs) -> ApprovalRequest:
        defaults = dict(
            action="git commit -m 'x'",
            target="repo:main",
            reason="Reason",
            expected_effect="Commit",
            risk_level=RiskLevel.HIGH,
            consequence_if_denied="Nothing",
            action_category="git.commit",
        )
        defaults.update(kwargs)
        return mgr.request_approval(**defaults)

    def test_approve_decision(self, mgr: ApprovalManager):
        req = self._create_request(mgr)
        decision = ApprovalDecision(
            request_id=req.request_id,
            status=ApprovalStatus.APPROVED,
            decided_by="operator",
            decision_note="Looks good",
            grants_scope_inheritance=False,
            scope_key=req.scope_key,
        )
        mgr.record_decision(decision)
        fetched = mgr.get_request(req.request_id)
        assert fetched is not None
        assert fetched.status == ApprovalStatus.APPROVED
        assert fetched.decided_by == "operator"
        assert fetched.decision_note == "Looks good"

    def test_deny_decision(self, mgr: ApprovalManager):
        req = self._create_request(mgr, action="rm -rf /", risk_level=RiskLevel.CRITICAL)
        decision = ApprovalDecision(
            request_id=req.request_id,
            status=ApprovalStatus.DENIED,
            decided_by="human",
            decision_note="Absolutely not",
            grants_scope_inheritance=False,
            scope_key=req.scope_key,
        )
        mgr.record_decision(decision)
        fetched = mgr.get_request(req.request_id)
        assert fetched is not None
        assert fetched.status == ApprovalStatus.DENIED

    def test_approved_appears_in_list_approved(self, mgr: ApprovalManager):
        req = self._create_request(mgr)
        mgr.record_decision(ApprovalDecision(
            request_id=req.request_id,
            status=ApprovalStatus.APPROVED,
            grants_scope_inheritance=False,
            scope_key=req.scope_key,
        ))
        approved = mgr.list_approved()
        assert any(a.request_id == req.request_id for a in approved)

    def test_approved_removed_from_pending(self, mgr: ApprovalManager):
        req = self._create_request(mgr)
        mgr.record_decision(ApprovalDecision(
            request_id=req.request_id,
            status=ApprovalStatus.APPROVED,
            grants_scope_inheritance=False,
            scope_key=req.scope_key,
        ))
        pending = mgr.list_pending()
        assert not any(p.request_id == req.request_id for p in pending)


# ===========================================================================
# ApprovalManager — scope matching and risk ceiling
# ===========================================================================

class TestScopeMatching:
    def _approve_with_inheritance(
        self,
        mgr: ApprovalManager,
        action_category: str,
        target: str,
        risk_level: RiskLevel,
    ) -> ApprovalRequest:
        req = mgr.request_approval(
            action=f"{action_category} on {target}",
            target=target,
            reason="Setup approval",
            expected_effect="Effect",
            risk_level=risk_level,
            consequence_if_denied="Blocked",
            action_category=action_category,
        )
        mgr.record_decision(ApprovalDecision(
            request_id=req.request_id,
            status=ApprovalStatus.APPROVED,
            grants_scope_inheritance=True,
            scope_key=req.scope_key,
        ))
        return req

    def test_inherited_approval_same_category_and_target(self, mgr: ApprovalManager):
        req = self._approve_with_inheritance(mgr, "git.commit", "repo:main", RiskLevel.HIGH)
        inherited = mgr.find_inherited(
            action_category="git.commit",
            target="repo:main",
            risk_level=RiskLevel.HIGH,
            operation_fingerprint=req.operation_fingerprint,
        )
        assert inherited is not None

    def test_no_inheritance_different_category(self, mgr: ApprovalManager):
        req = self._approve_with_inheritance(mgr, "git.commit", "repo:main", RiskLevel.HIGH)
        inherited = mgr.find_inherited(
            action_category="git.push",
            target="repo:main",
            risk_level=RiskLevel.HIGH,
            operation_fingerprint=req.operation_fingerprint,
        )
        assert inherited is None

    def test_no_inheritance_different_target(self, mgr: ApprovalManager):
        req = self._approve_with_inheritance(mgr, "git.commit", "repo:main", RiskLevel.HIGH)
        inherited = mgr.find_inherited(
            action_category="git.commit",
            target="repo:develop",
            risk_level=RiskLevel.HIGH,
            operation_fingerprint=req.operation_fingerprint,
        )
        assert inherited is None

    def test_risk_ceiling_respected_lower_is_ok(self, mgr: ApprovalManager):
        """An action with MEDIUM risk may inherit an approval granted for HIGH."""
        req = self._approve_with_inheritance(mgr, "git.commit", "repo:main", RiskLevel.HIGH)
        inherited = mgr.find_inherited(
            action_category="git.commit",
            target="repo:main",
            risk_level=RiskLevel.MEDIUM,
            operation_fingerprint=req.operation_fingerprint,
        )
        assert inherited is not None

    def test_risk_ceiling_exceeded_critical_rejected(self, mgr: ApprovalManager):
        """A CRITICAL action cannot inherit an approval granted for HIGH."""
        req = self._approve_with_inheritance(mgr, "git.commit", "repo:main", RiskLevel.HIGH)
        inherited = mgr.find_inherited(
            action_category="git.commit",
            target="repo:main",
            risk_level=RiskLevel.CRITICAL,
            operation_fingerprint=req.operation_fingerprint,
        )
        assert inherited is None

    def test_no_inheritance_without_grant_flag(self, mgr: ApprovalManager):
        """If grants_scope_inheritance=False, find_inherited must return None."""
        req = mgr.request_approval(
            action="git.commit on repo:main",
            target="repo:main",
            reason="test",
            expected_effect="commit",
            risk_level=RiskLevel.HIGH,
            consequence_if_denied="blocked",
            action_category="git.commit",
        )
        mgr.record_decision(ApprovalDecision(
            request_id=req.request_id,
            status=ApprovalStatus.APPROVED,
            grants_scope_inheritance=False,  # explicitly no inheritance
            scope_key=req.scope_key,
        ))
        inherited = mgr.find_inherited(
            action_category="git.commit",
            target="repo:main",
            risk_level=RiskLevel.HIGH,
            operation_fingerprint=req.operation_fingerprint,
        )
        assert inherited is None

    def test_is_approved_convenience(self, mgr: ApprovalManager):
        req = self._approve_with_inheritance(mgr, "shell", "localhost", RiskLevel.MEDIUM)
        assert mgr.is_approved(
            action_category="shell",
            target="localhost",
            risk_level=RiskLevel.LOW,
            operation_fingerprint=req.operation_fingerprint,
        )
        assert not mgr.is_approved(
            action_category="shell",
            target="remote-host",
            risk_level=RiskLevel.LOW,
            operation_fingerprint=req.operation_fingerprint,
        )


# ===========================================================================
# ApprovalManager — rejection and invalid states
# ===========================================================================

class TestRejectionAndInvalid:
    def test_denied_approval_no_inheritance(self, mgr: ApprovalManager):
        req = mgr.request_approval(
            action="git push --force",
            target="repo:main",
            reason="Force push",
            expected_effect="Overwrites remote history",
            risk_level=RiskLevel.CRITICAL,
            consequence_if_denied="History preserved",
            action_category="git.push",
        )
        mgr.record_decision(ApprovalDecision(
            request_id=req.request_id,
            status=ApprovalStatus.DENIED,
            grants_scope_inheritance=True,  # even if flagged, DENIED means no
            scope_key=req.scope_key,
        ))
        # DENIED → must NOT appear in inheritance
        inherited = mgr.find_inherited(
            action_category="git.push",
            target="repo:main",
            risk_level=RiskLevel.CRITICAL,
            operation_fingerprint=req.operation_fingerprint,
        )
        assert inherited is None

    def test_get_request_nonexistent_returns_none(self, mgr: ApprovalManager):
        result = mgr.get_request("nonexistent-id-12345")
        assert result is None

    def test_pending_not_in_inheritance(self, mgr: ApprovalManager):
        """A request that is still PENDING must not be returned as inherited."""
        req = mgr.request_approval(
            action="git commit -m 'pending'",
            target="repo:main",
            reason="test",
            expected_effect="commit",
            risk_level=RiskLevel.HIGH,
            consequence_if_denied="blocked",
            action_category="git.commit",
        )
        # No decision recorded → still PENDING
        inherited = mgr.find_inherited(
            action_category="git.commit",
            target="repo:main",
            risk_level=RiskLevel.HIGH,
            operation_fingerprint=req.operation_fingerprint,
        )
        assert inherited is None


# ===========================================================================
# Operation-bound approvals (SEC-006)
# ===========================================================================

class TestOperationBinding:
    """An approval covers exactly the operation that was approved."""

    def _approve(
        self, mgr: ApprovalManager, action: str,
        *, category: str = "shell", target: str = "workspace",
        risk: RiskLevel = RiskLevel.MEDIUM,
    ) -> ApprovalRequest:
        req = mgr.request_approval(
            action=action,
            target=target,
            reason="test",
            expected_effect="effect",
            risk_level=risk,
            consequence_if_denied="blocked",
            action_category=category,
        )
        mgr.record_decision(ApprovalDecision(
            request_id=req.request_id,
            status=ApprovalStatus.APPROVED,
            grants_scope_inheritance=True,
            scope_key=req.scope_key,
        ))
        return req

    def test_fingerprint_deterministic_and_param_bound(self):
        a = make_operation_fingerprint("shell", "workspace", "python -m pytest")
        b = make_operation_fingerprint("shell", "workspace", "python -m pytest")
        c = make_operation_fingerprint("shell", "workspace", "python -m pytest -x")
        d = make_operation_fingerprint("shell", "other", "python -m pytest")
        # Category/target are case-insensitive; the action is exact.
        e = make_operation_fingerprint("Shell", "Workspace", "python -m pytest")
        f = make_operation_fingerprint("shell", "workspace", "Python -m pytest")
        assert a == b
        assert a != c          # argument change → different fingerprint
        assert a != d          # target change → different fingerprint
        assert a == e          # category/target casing is immaterial
        assert a != f          # action casing IS material (verbatim argv)
        assert len(a) == 32

    def test_unchanged_operation_inherits(self, mgr: ApprovalManager):
        self._approve(mgr, "python -m pytest tests/unit")
        fp = make_operation_fingerprint(
            "shell", "workspace", "python -m pytest tests/unit"
        )
        assert mgr.find_inherited(
            action_category="shell",
            target="workspace",
            risk_level=RiskLevel.MEDIUM,
            operation_fingerprint=fp,
        ) is not None

    def test_changed_arguments_rejected(self, mgr: ApprovalManager):
        self._approve(mgr, "python -m pytest tests/unit")
        fp_changed = make_operation_fingerprint(
            "shell", "workspace", "python -m pytest tests/unit --no-header -p no:cacheprovider"
        )
        assert mgr.find_inherited(
            action_category="shell",
            target="workspace",
            risk_level=RiskLevel.MEDIUM,
            operation_fingerprint=fp_changed,
        ) is None

    def test_changed_target_rejected(self, mgr: ApprovalManager):
        self._approve(mgr, "python -m pytest tests/unit")
        fp = make_operation_fingerprint(
            "shell", "other-workspace", "python -m pytest tests/unit"
        )
        assert mgr.find_inherited(
            action_category="shell",
            target="other-workspace",
            risk_level=RiskLevel.MEDIUM,
            operation_fingerprint=fp,
        ) is None

    def test_empty_fingerprint_refused(self, mgr: ApprovalManager):
        self._approve(mgr, "python -m pytest tests/unit")
        assert mgr.find_inherited(
            action_category="shell",
            target="workspace",
            risk_level=RiskLevel.MEDIUM,
            operation_fingerprint="",
        ) is None

    def test_legacy_null_fingerprint_never_inherits(self, mgr: ApprovalManager, db: Path):
        """Pre-v6 rows (NULL fingerprint) are insufficient: fresh approval."""
        req = self._approve(mgr, "python -m pytest tests/unit")
        # Simulate a legacy row written before schema v6
        from factory.database import get_connection
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE approvals SET operation_fingerprint = NULL "
                "WHERE request_id = ?",
                (req.request_id,),
            )
            conn.commit()

        # Fresh manager → index rebuilt from DB (NULL → "")
        fresh = ApprovalManager(db_path=db)
        fp = make_operation_fingerprint(
            "shell", "workspace", "python -m pytest tests/unit"
        )
        assert fresh.find_inherited(
            action_category="shell",
            target="workspace",
            risk_level=RiskLevel.MEDIUM,
            operation_fingerprint=fp,
        ) is None

    def test_cross_process_grant_found_via_db(self, mgr: ApprovalManager, db: Path):
        """A grant written by another process (approvals CLI) is honoured."""
        # Create a pending request without recording an in-memory decision
        pending = mgr.request_approval(
            action="python -m pytest",
            target="workspace",
            reason="test",
            expected_effect="effect",
            risk_level=RiskLevel.MEDIUM,
            consequence_if_denied="blocked",
            action_category="shell",
        )
        # Write the approval decision exactly like the CLI does — direct SQL,
        # bypassing record_decision() so the in-memory index never sees it.
        from factory.database import get_connection
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE approvals SET status='approved', grants_scope_inheritance=1, "
                "scope_key=?, decided_at=datetime('now'), decided_by='human' "
                "WHERE request_id=?",
                (pending.scope_key, pending.request_id),
            )
            conn.commit()

        fp = make_operation_fingerprint("shell", "workspace", "python -m pytest")
        inherited = mgr.find_inherited(
            action_category="shell",
            target="workspace",
            risk_level=RiskLevel.MEDIUM,
            operation_fingerprint=fp,
        )
        assert inherited is not None
        assert inherited.request_id == pending.request_id
        # And a DIFFERENT operation still does not inherit via that grant
        fp_changed = make_operation_fingerprint(
            "shell", "workspace", "python -m pytest --force"
        )
        assert mgr.find_inherited(
            action_category="shell",
            target="workspace",
            risk_level=RiskLevel.MEDIUM,
            operation_fingerprint=fp_changed,
        ) is None


# ===========================================================================
# PermissionBroker — full pipeline decision
# ===========================================================================

class TestBrokerDecision:
    def test_safe_action_approved_directly(self, broker: PermissionBroker):
        ctx = ActionContext(category="file.read", command="cat README.md")
        decision = broker.check(
            ctx,
            action="cat README.md",
            target="README.md",
            reason="Read docs",
            expected_effect="Reads the file",
            consequence_if_denied="Cannot read docs",
        )
        assert decision.approved
        assert decision.outcome == PermissionOutcome.APPROVED

    def test_high_risk_requires_approval(self, broker: PermissionBroker):
        ctx = ActionContext(category="git", command="git commit -m 'feat'")
        decision = broker.check(
            ctx,
            action="git commit -m 'feat'",
            target="repo:main",
            reason="Commit code",
            expected_effect="Creates a commit",
            consequence_if_denied="No commit",
            action_category="git.commit",
        )
        assert decision.requires_approval
        assert decision.approval_request is not None
        assert decision.approval_request.status == ApprovalStatus.PENDING
        assert decision.risk_level == RiskLevel.HIGH

    def test_critical_action_requires_approval(self, broker: PermissionBroker):
        ctx = ActionContext(category="git", command="git push --force origin main")
        decision = broker.check(
            ctx,
            action="git push --force origin main",
            target="remote:origin",
            reason="Force push",
            expected_effect="Rewrites remote history",
            consequence_if_denied="History preserved",
            action_category="git.push",
        )
        assert decision.requires_approval
        assert decision.risk_level == RiskLevel.CRITICAL

    def test_production_deployment_denied(self, broker: PermissionBroker):
        ctx = ActionContext(category="deploy", command="deploy to production")
        decision = broker.check(
            ctx,
            action="deploy to production",
            target="production-server",
            reason="Ship it",
            expected_effect="Live deployment",
            consequence_if_denied="No deploy",
        )
        assert decision.denied
        assert "production" in decision.reason.lower()

    def test_inherited_approval_reused(self, broker: PermissionBroker):
        """After one approval with inheritance, an IDENTICAL operation is auto-approved."""
        ctx = ActionContext(category="git", command="git commit -m 'initial'")
        # First check → should require approval
        d1 = broker.check(
            ctx,
            action="git commit -m 'initial'",
            target="repo:main",
            reason="First commit",
            expected_effect="Commit",
            consequence_if_denied="Nothing committed",
            action_category="git.commit",
        )
        assert d1.requires_approval
        assert d1.approval_request is not None

        # Simulate human approving with scope inheritance
        from factory.models import ApprovalDecision as AD
        broker.approval_manager.record_decision(AD(
            request_id=d1.approval_request.request_id,
            status=ApprovalStatus.APPROVED,
            grants_scope_inheritance=True,
            scope_key=d1.approval_request.scope_key,
        ))

        # Second IDENTICAL operation → should inherit (SEC-006: unchanged)
        d2 = broker.check(
            ctx,
            action="git commit -m 'initial'",
            target="repo:main",
            reason="First commit",
            expected_effect="Commit",
            consequence_if_denied="Nothing committed",
            action_category="git.commit",
        )
        assert d2.approved
        assert d2.inherited_from == d1.approval_request.request_id

    def test_changed_action_not_inherited(self, broker: PermissionBroker):
        """SEC-006: a materially altered operation never inherits approval.

        Approving ``git commit -m 'initial'`` must not authorise
        ``git commit -m 'second'`` merely because category and target
        are unchanged.
        """
        ctx = ActionContext(category="git", command="git commit -m 'initial'")
        d1 = broker.check(
            ctx,
            action="git commit -m 'initial'",
            target="repo:main",
            reason="First commit",
            expected_effect="Commit",
            consequence_if_denied="Nothing committed",
            action_category="git.commit",
        )
        assert d1.requires_approval
        assert d1.approval_request is not None

        from factory.models import ApprovalDecision as AD
        broker.approval_manager.record_decision(AD(
            request_id=d1.approval_request.request_id,
            status=ApprovalStatus.APPROVED,
            grants_scope_inheritance=True,
            scope_key=d1.approval_request.scope_key,
        ))

        # Different commit message → different operation → fresh approval
        d2 = broker.check(
            ctx,
            action="git commit -m 'second'",
            target="repo:main",
            reason="Second commit",
            expected_effect="Another commit",
            consequence_if_denied="Nothing committed",
            action_category="git.commit",
        )
        assert d2.requires_approval
        assert not d2.approved
        assert d2.inherited_from is None
        assert d2.approval_request is not None
        assert d2.approval_request.request_id != d1.approval_request.request_id

    def test_higher_risk_not_inherited(self, broker: PermissionBroker):
        """
        An approval for HIGH risk must NOT cover a CRITICAL risk action.
        """
        ctx_high = ActionContext(category="git", command="git commit -m 'feat'")
        d1 = broker.check(
            ctx_high,
            action="git commit -m 'feat'",
            target="repo:main",
            reason="Commit",
            expected_effect="Commit created",
            consequence_if_denied="Nothing committed",
            action_category="git.commit",
        )
        assert d1.requires_approval

        from factory.models import ApprovalDecision as AD
        broker.approval_manager.record_decision(AD(
            request_id=d1.approval_request.request_id,  # type: ignore[union-attr]
            status=ApprovalStatus.APPROVED,
            grants_scope_inheritance=True,
            scope_key=d1.approval_request.scope_key,  # type: ignore[union-attr]
        ))

        # Now try a CRITICAL push on the same target
        ctx_crit = ActionContext(category="git", command="git push --force origin main")
        d2 = broker.check(
            ctx_crit,
            action="git push --force origin main",
            target="repo:main",
            reason="Force push",
            expected_effect="Rewrites remote",
            consequence_if_denied="History safe",
            action_category="git.push",  # different category → no match anyway
        )
        assert d2.requires_approval

    def test_decision_metadata_populated(self, broker: PermissionBroker):
        ctx = ActionContext(category="file.read", command="cat config.yaml")
        decision = broker.check(
            ctx,
            action="cat config.yaml",
            target="config.yaml",
            reason="Read config",
            expected_effect="Config read",
            consequence_if_denied="Cannot read config",
        )
        assert "classified_risk" in decision.metadata

    def test_sensitive_path_escalates_to_critical(self, broker: PermissionBroker):
        ctx = ActionContext(
            category="file.read",
            command="cat .env",
            target_path=Path(".env"),
        )
        decision = broker.check(
            ctx,
            action="cat .env",
            target=".env",
            reason="Read env file",
            expected_effect="Reads secrets",
            consequence_if_denied="Cannot read secrets",
        )
        assert decision.risk_level == RiskLevel.CRITICAL
        assert not decision.approved  # critical always needs approval

    def test_approval_decision_approved_property(self, db: Path):
        broker = PermissionBroker(db_path=db)
        ctx = ActionContext(category="file.read", command="cat README.md")
        decision = broker.check(
            ctx,
            action="cat README.md",
            target="README.md",
            reason="Read docs",
            expected_effect="File read",
            consequence_if_denied="Cannot read",
        )
        assert decision.approved is True
        assert decision.requires_approval is False
        assert decision.denied is False


# ===========================================================================
# RiskEngine — sanity checks (ensure nothing was broken)
# ===========================================================================

class TestRiskEngineSanity:
    def setup_method(self):
        self.engine = RiskEngine()

    def test_git_commit_is_high(self):
        ctx = ActionContext(category="git", command="git commit -m 'test'")
        assert self.engine.classify(ctx) == RiskLevel.HIGH

    def test_git_force_push_is_critical(self):
        ctx = ActionContext(category="git", command="git push --force origin main")
        assert self.engine.classify(ctx) == RiskLevel.CRITICAL

    def test_file_read_is_safe(self):
        ctx = ActionContext(category="file.read", command="cat README.md",
                            target_path=Path("README.md"))
        assert self.engine.classify(ctx) == RiskLevel.SAFE

    def test_shell_rm_rf_is_critical(self):
        ctx = ActionContext(category="shell", command="rm -rf /tmp/data")
        assert self.engine.classify(ctx) == RiskLevel.CRITICAL

    def test_sensitive_target_always_critical(self):
        ctx = ActionContext(
            category="file.read", command="cat .env", target_is_sensitive=True
        )
        assert self.engine.classify(ctx) == RiskLevel.CRITICAL
