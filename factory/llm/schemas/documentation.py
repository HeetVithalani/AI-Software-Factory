"""
factory.llm.schemas.documentation
===================================
Output schema for the Documentation agent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentationProposal(BaseModel):
    summary: str
    readme_content: str = Field(
        ...,
        description="Content to write to README.md",
    )