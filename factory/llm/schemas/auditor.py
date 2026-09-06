"""
factory.llm.schemas.auditor
===========================
Output schema for the final Auditor agent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AuditorProposal(BaseModel):
    summary: str
    requirements_met: bool = Field(..., description="True if all baseline requirements were satisfied.")
    evidence: list[str] = Field(
        ...,
        description="Specific evidence (test logs, review approvals) supporting the decision."
    )
    missing_evidence: list[str] = Field(default_factory=list)
    approved: bool = Field(..., description="Final approval decision.")
