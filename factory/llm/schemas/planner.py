"""
factory.llm.schemas.planner
===========================
Output schema for the Planner agent.
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class PlannerTask(BaseModel):
    task_id: str
    name: str
    agent_type: str = Field(..., description="E.g., 'builder', 'reviewer', 'qa'")
    depends_on: list[str] = Field(default_factory=list)
    description: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlannerProposal(BaseModel):
    summary: str
    decisions: list[str] = Field(
        default_factory=list,
        description="Architecture and technology decisions."
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Assumptions made by the planner."
    )
    tasks: list[PlannerTask] = Field(
        ...,
        description="The task DAG for the orchestrator."
    )
