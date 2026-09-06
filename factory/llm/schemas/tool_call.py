"""
factory.llm.schemas.tool_call
=============================
Structured schema for LLM tool-call proposals.
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class ToolCallProposal(BaseModel):
    """
    A specific action proposed by the LLM.
    Must pass through the PermissionBroker.
    """
    action_type: str = Field(
        ..., 
        description="Type of action, e.g. 'shell.run', 'filesystem.write', 'git.commit'"
    )
    tool: str = Field(
        ...,
        description="Tool name, e.g. 'shell', 'filesystem', 'git'"
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments for the tool call."
    )
    reason: str = Field(
        ...,
        description="Why the model is proposing this action."
    )
    expected_effect: str = Field(
        ...,
        description="What the model expects this action to accomplish."
    )
    
    def to_action_context(self) -> Any:
        """Convert this proposal to an ActionContext for the broker."""
        from factory.permissions.risk import ActionContext
        from pathlib import Path
        
        command = ""
        target_path = None
        
        if self.tool == "shell" and "command" in self.arguments:
            command = self.arguments["command"]
        elif self.tool == "git" and "command" in self.arguments:
            command = f"git {self.arguments['command']}"
        
        if "path" in self.arguments:
            target_path = Path(self.arguments["path"])
            
        return ActionContext(
            category=self.action_type,
            command=command,
            target_path=target_path,
        )
