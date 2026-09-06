"""
factory.context.contract
========================
Task contracts to define requirements and success criteria.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskContract:
    """
    Defines the contract for an agent to fulfill a task.
    """
    task_id: str
    description: str
    inputs: dict[str, Any] = field(default_factory=dict)
    expected_outputs: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
