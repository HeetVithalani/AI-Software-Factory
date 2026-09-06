"""
factory.research.researcher
============================
Research coordinator — produces structured research summaries.

The researcher gathers information from sources, weighs evidence by
trust level and determinism, and produces a ResearchSummary that agents
can use to inform decisions. This is a data container — the actual
information gathering is done by agents.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from factory.research.decisions import DecisionRecord
from factory.research.sources import ResearchSource


class ResearchSummary(BaseModel):
    """
    A structured summary of research findings for a task.

    Used by agents to document what they investigated and what they found.
    """
    summary_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    task_id: str | None = None
    topic: str
    # Key findings (deterministic or agent-observed)
    findings: list[str] = Field(default_factory=list)
    # Sources cited
    sources: list[dict[str, Any]] = Field(default_factory=list)
    # Decisions triggered by this research
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    # Open questions that remain unresolved
    open_questions: list[str] = Field(default_factory=list)
    # Confidence: 0.0–1.0; must be backed by evidence, not AI guess
    evidence_confidence: float = 0.0
    confidence_basis: str = ""
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    def add_finding(self, finding: str) -> None:
        self.findings.append(finding)

    def add_source(self, source: ResearchSource) -> None:
        self.sources.append({
            "source_id": source.source_id,
            "name": source.name,
            "reference": source.reference,
            "trust_level": source.trust_level,
        })

    def add_decision(self, decision: DecisionRecord) -> None:
        self.decisions.append({
            "decision_id": decision.decision_id,
            "title": decision.title,
            "decision": decision.decision,
        })

    def add_open_question(self, question: str) -> None:
        self.open_questions.append(question)
