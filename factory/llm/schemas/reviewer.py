"""
factory.llm.schemas.reviewer
============================
Output schema for the Reviewer agent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReviewDefect(BaseModel):
    file_path: str
    description: str
    severity: str = Field(..., description="'low', 'medium', 'high', 'critical'")
    suggested_fix: str


class ReviewerProposal(BaseModel):
    summary: str
    defects: list[ReviewDefect] = Field(default_factory=list)
    approved: bool = Field(..., description="True if code meets requirements and quality bars.")
    risks: list[str] = Field(default_factory=list)
