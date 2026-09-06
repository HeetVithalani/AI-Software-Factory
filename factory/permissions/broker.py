"""
factory.permissions.broker
============================
Permission broker — single entry-point for all action authorisation.

``PermissionBroker`` ties together the four permission subsystems:

1. **PolicySet** — which categories of action require approval by default.
2. **RiskEngine** — deterministically classifies every action's risk level.
3. **SensitiveDataDetector** — escalates risk for sensitive paths/content.
4. **ApprovalManager** — manages the approval lifecycle and scope inheritance.

``PermissionDecision`` is the return type: a lightweight value object that
tells the caller whether to proceed, request approval, or stop entirely.

Design
------
* The broker **never** makes a network call, consults an LLM, or performs
  any I/O beyond reading the DB.
* The broker is **stateless** from the caller's perspective — instantiate
  once per kernel startup and call ``check()`` for every action.
* Approval inheritance is automatic: if a prior equivalent action was
  approved with scope-inheritance, the broker returns APPROVED without
  raising ``ApprovalRequiredError``.

Outcome values
--------------
``PermissionOutcome.APPROVED``
    Action may proceed immediately (either inherently safe, or previously
    approved with scope inheritance).

``PermissionOutcome.APPROVAL_REQUIRED``
    A new ``ApprovalRequest`` has been persisted; the caller should surface
    it to a human and wait before proceeding.

``PermissionOutcome.DENIED``
    The action is forbidden by a non-negotiable policy and must not proceed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from factory.models import ApprovalRequest, RiskLevel
from factory.permissions.policies import PolicySet
from factory.permissions.risk import ActionContext, RiskEngine
from factory.permissions.sensitive_data import SensitiveDataDetector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outcome enumeration
# ---------------------------------------------------------------------------

class PermissionOutcome(str, Enum):
    """Result of a broker permission check."""
    APPROVED = "approved"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"


# ---------------------------------------------------------------------------
# PermissionDecision
# ---------------------------------------------------------------------------

@dataclass
class PermissionDecision:
    """
    Value object returned by ``PermissionBroker.check()``.

    Attributes
    ----------
    outcome:
        Whether the action is approved, requires approval, or is denied.
    risk_level:
        The classified risk level of the action.
    approval_request:
        Populated only when ``outcome == APPROVAL_REQUIRED``; the pending
        request that must be resolved before the action can proceed.
    reason:
        Human-readable explanation of the decision.
    inherited_from:
        If the decision is APPROVED via scope inheritance, the request_id of
        the original approval that was re-used.
    metadata:
        Supplementary data (sensitivity report, policy flags, etc.).
    """
    outcome: PermissionOutcome
    risk_level: RiskLevel
    approval_request: ApprovalRequest | None = None
    reason: str = ""
    inherited_from: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # Convenience predicates
    @property
    def approved(self) -> bool:
        return self.outcome == PermissionOutcome.APPROVED

    @property
    def requires_approval(self) -> bool:
        return self.outcome == PermissionOutcome.APPROVAL_REQUIRED

    @property
    def denied(self) -> bool:
        return self.outcome == PermissionOutcome.DENIED


# ---------------------------------------------------------------------------
# PermissionBroker
# ---------------------------------------------------------------------------

class PermissionBroker:
    """
    Central permission broker for the AI Software Factory.

    Usage::

        broker = PermissionBroker(policies=policy_set)

        decision = broker.check(
            ctx=ActionContext(
                category="git",
                command="git commit -m 'feat: add login'",
            ),
            action="git commit -m 'feat: add login'",
            target="repo:main",
            reason="Committing generated feature code",
            expected_effect="Creates a new commit on the main branch",
            consequence_if_denied="Changes stay uncommitted until next build",
        )

        if decision.requires_approval:
            # surface decision.approval_request to a human
            ...
        elif decision.denied:
            raise PermissionDeniedError(decision.reason)
        # else: proceed
    """

    def __init__(
        self,
        *,
        policies: PolicySet | None = None,
        db_path: Path | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._policies = policies or PolicySet()
        self._risk_engine = RiskEngine()
        self._sensitive = SensitiveDataDetector()

        # Lazy-initialise ApprovalManager to avoid circular imports
        self._db_path = db_path
        self._project_id = project_id
        self._agent_id = agent_id
        self._approval_mgr: Any = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def approval_manager(self):
        """Lazily construct and return the shared ApprovalManager."""
        if self._approval_mgr is None:
            from factory.permissions.approvals import ApprovalManager
            self._approval_mgr = ApprovalManager(
                db_path=self._db_path,
                project_id=self._project_id,
                agent_id=self._agent_id,
            )
        return self._approval_mgr

    def check(
        self,
        ctx: ActionContext,
        *,
        action: str,
        target: str,
        reason: str,
        expected_effect: str,
        consequence_if_denied: str,
        action_category: str = "",
        task_id: str | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> PermissionDecision:
        """
        Run the full permission pipeline for an action.

        Steps
        -----
        1. Classify risk via ``RiskEngine``.
        2. Apply ``SensitiveDataDetector`` to escalate risk if needed.
        3. Check if policies block the action outright.
        4. If approval required, check for valid scope inheritance first.
        5. If no inheritance, create a new pending ApprovalRequest.
        6. If risk is within safe threshold, return APPROVED directly.

        Returns a ``PermissionDecision`` — never raises.
        """
        # ---- 1. Risk classification ----
        risk_level = self._risk_engine.classify(ctx)

        meta: dict[str, Any] = {"classified_risk": risk_level.value}

        # ---- 2. Sensitive data escalation ----
        sensitivity_report = None
        if ctx.target_path is not None:
            sensitivity_report = self._sensitive.check_path(ctx.target_path)
            if sensitivity_report.is_sensitive:
                risk_level = RiskLevel.CRITICAL
                meta["sensitivity"] = {
                    "reasons": sensitivity_report.reasons,
                    "severity": sensitivity_report.severity,
                }
                logger.info(
                    "Risk escalated to CRITICAL due to sensitive path: %s",
                    ctx.target_path,
                )

        # ---- 3. Non-negotiable policy blocks ----
        deny_reason = self._check_policy_blocks(ctx, risk_level)
        if deny_reason:
            logger.warning(
                "Action DENIED by policy: %s | action=%s | risk=%s",
                deny_reason, action, risk_level,
            )
            return PermissionDecision(
                outcome=PermissionOutcome.DENIED,
                risk_level=risk_level,
                reason=deny_reason,
                metadata=meta,
            )

        # ---- 4. Does this risk/policy combo require approval? ----
        needs_approval = self._risk_engine.requires_approval(
            risk_level, self._policies
        )

        # Special policy overrides for specific categories
        needs_approval = needs_approval or self._category_requires_approval(
            action_category or ctx.category, risk_level
        )

        if not needs_approval:
            return PermissionDecision(
                outcome=PermissionOutcome.APPROVED,
                risk_level=risk_level,
                reason="Risk level within auto-approval threshold.",
                metadata=meta,
            )

        # ---- 5. Check scope inheritance ----
        if self._policies.auto_approve_inherited:
            inherited = self.approval_manager.find_inherited(
                action_category=action_category or ctx.category,
                target=target,
                risk_level=risk_level,
            )
            if inherited is not None:
                meta["inherited_from"] = inherited.request_id
                logger.info(
                    "Approval inherited from request_id=%s for action=%s",
                    inherited.request_id, action,
                )
                return PermissionDecision(
                    outcome=PermissionOutcome.APPROVED,
                    risk_level=risk_level,
                    reason="Approved via scope inheritance from prior equivalent action.",
                    inherited_from=inherited.request_id,
                    metadata=meta,
                )

        # ---- 6. New approval required ----
        approval_req = self.approval_manager.request_approval(
            action=action,
            target=target,
            reason=reason,
            expected_effect=expected_effect,
            risk_level=risk_level,
            consequence_if_denied=consequence_if_denied,
            action_category=action_category or ctx.category,
            task_id=task_id,
            project_id=project_id or self._project_id,
            agent_id=agent_id or self._agent_id,
        )

        logger.info(
            "Approval required: request_id=%s | action=%s | risk=%s",
            approval_req.request_id, action, risk_level,
        )
        return PermissionDecision(
            outcome=PermissionOutcome.APPROVAL_REQUIRED,
            risk_level=risk_level,
            approval_request=approval_req,
            reason=(
                f"Action '{action}' on '{target}' requires human approval "
                f"(risk={risk_level.value})."
            ),
            metadata=meta,
        )

    # ------------------------------------------------------------------
    # Policy helpers
    # ------------------------------------------------------------------

    def _check_policy_blocks(
        self, ctx: ActionContext, risk_level: RiskLevel
    ) -> str:
        """
        Return a non-empty denial reason if the action is outright blocked
        by a non-negotiable policy, or an empty string if allowed.
        """
        # Production deployment forbidden
        if self._policies.production_deployment_forbidden:
            if ctx.category in ("deploy", "deployment") or (
                "production" in ctx.command.lower()
                or "prod" in ctx.command.lower()
            ):
                return (
                    "Production deployments are forbidden by the active policy "
                    "(production_deployment_forbidden=True)."
                )

        # Sensitive data blocked from remote transmission
        if self._policies.block_sensitive_to_remote and ctx.target_is_sensitive:
            if ctx.category == "network" or "push" in ctx.command.lower():
                return (
                    "Transmitting sensitive data to a remote endpoint is blocked "
                    "by policy (block_sensitive_to_remote=True)."
                )

        return ""

    def _category_requires_approval(
        self, category: str, risk_level: RiskLevel
    ) -> bool:
        """
        Apply category-specific policy approval gates beyond the risk engine.
        """
        cat = category.lower()

        if (
            self._policies.require_approval_git_mutations
            and cat.startswith("git")
            and risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        ):
            return True

        if (
            self._policies.require_approval_sensitive_data
            and risk_level == RiskLevel.CRITICAL
        ):
            return True

        if (
            self._policies.require_approval_knowledge_high_impact
            and cat == "knowledge"
            and risk_level == RiskLevel.HIGH
        ):
            return True

        return False
