"""
factory.llm.schemas.security
============================
Output schema for the Security agent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SecurityVulnerability(BaseModel):
    file_path: str
    line_number: int | None = None
    severity: str = Field(..., description="'low', 'medium', 'high', 'critical'")
    description: str
    suggested_fix: str


class SecurityProposal(BaseModel):
    summary: str
    approved: bool = Field(..., description="True if no critical vulnerabilities found")
    vulnerabilities: list[SecurityVulnerability] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)