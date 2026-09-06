"""
factory.tools
=============
Public surface for the controlled tool layer.

Every tool in this package:
- Runs a permission check via PermissionBroker before executing side effects
- Returns a ToolResult (never raises on expected failure)
- Attaches deterministic evidence to the audit log
- Is sandboxed to the project workspace
"""

from factory.errors import ToolError
from factory.tools.base import ToolResult, BaseTool

__all__ = [
    "ToolError",
    "ToolResult",
    "BaseTool",
]
