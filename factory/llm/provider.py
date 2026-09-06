"""
factory.llm.provider
====================
Provider abstraction for LLM calls.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel


@dataclass
class LLMRequest:
    """Request payload sent to an LLM provider."""
    model: str
    system_prompt: str
    messages: list[dict[str, str]]
    # Optional JSON schema to enforce
    output_schema: dict[str, Any] | None = None
    
    # Metadata for auditing/limits
    max_tokens: int = 4096
    timeout: int = 120
    temperature: float = 0.1
    
    task_id: str | None = None
    manifest_id: str | None = None
    project_id: str | None = None


@dataclass
class LLMResponse:
    """Response returned from an LLM provider."""
    request_id: str
    content: str
    
    # Telemetry
    start_time: datetime
    end_time: datetime
    duration_ms: float
    
    tokens_in: int = 0
    tokens_out: int = 0
    
    # If the provider natively rejected/failed the request
    success: bool = True
    error: str = ""
    
    # If using structured output
    structured_data: dict[str, Any] = field(default_factory=dict)


class LLMProvider(abc.ABC):
    """Abstract base class for all LLM providers."""

    @abc.abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Execute a synchronous completion request."""
        pass
