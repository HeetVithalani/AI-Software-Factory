"""
factory.llm.schemas.builder
===========================
Output schema for the Builder agent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from factory.llm.schemas.tool_call import ToolCallProposal


class BuilderProposal(BaseModel):
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    proposed_actions: list[ToolCallProposal] = Field(
        default_factory=list,
        description="Tool calls proposed by the builder to fulfill the task."
    )
    blockers: list[str] = Field(default_factory=list)
    remaining_work: list[str] = Field(default_factory=list)
    completed: bool = Field(
        ...,
        description="True if the task is complete, False if more actions are needed."
    )
