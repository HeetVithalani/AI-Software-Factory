"""
factory.research.decisions
===========================
Architectural and technical decision records.

Every significant technical decision made during a project is logged here
with its context, alternatives considered, rationale, and consequences.
This is analogous to ADRs (Architecture Decision Records).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class DecisionRecord(BaseModel):
    """
    A recorded architectural or technical decision.

    Fields match the standard ADR format with factory-specific additions.
    """
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    title: str
    # Context: what is the issue or situation driving this decision?
    context: str
    # Decision: what was chosen?
    decision: str
    # Alternatives that were considered but not chosen
    alternatives: list[str] = Field(default_factory=list)
    # Rationale: why was this decision made?
    rationale: str = ""
    # Consequences: what are the known trade-offs?
    consequences: str = ""
    # Status: proposed | accepted | deprecated | superseded
    status: str = "proposed"
    # Which agent or human made this decision
    decided_by: str = "agent"
    # Link to superseding decision if deprecated
    superseded_by: str | None = None
    decided_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionLog:
    """
    In-memory log of decisions for a project session.

    For persistence, decisions should be serialized and stored in
    project memory or the audit log.
    """

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self._decisions: list[DecisionRecord] = []

    def record(
        self,
        title: str,
        context: str,
        decision: str,
        *,
        alternatives: list[str] | None = None,
        rationale: str = "",
        consequences: str = "",
        decided_by: str = "agent",
        tags: list[str] | None = None,
    ) -> DecisionRecord:
        """Record and store a decision."""
        dr = DecisionRecord(
            project_id=self.project_id,
            title=title,
            context=context,
            decision=decision,
            alternatives=alternatives or [],
            rationale=rationale,
            consequences=consequences,
            decided_by=decided_by,
            status="accepted",
            tags=tags or [],
        )
        self._decisions.append(dr)
        return dr

    def all(self) -> list[DecisionRecord]:
        return list(self._decisions)

    def count(self) -> int:
        return len(self._decisions)
