"""
factory.llm.schemas.qa
=====================
Output schema for the QA agent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class QAProposal(BaseModel):
    summary: str
    all_passed: bool = Field(..., description="True if all tests passed")
    failed_tests: list[str] = Field(default_factory=list, description="List of failed test names")
    suggestions: list[str] = Field(default_factory=list, description="Suggestions for test improvements")