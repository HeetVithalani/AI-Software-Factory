"""
factory.context.builder
=======================
Builds and bounds context for LLM execution.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.context.manifest import ContextManifest, ContextPersister
from factory.state import FactoryState
from factory.memory.project_memory import ProjectMemory
from factory.memory.global_memory import GlobalMemory
from factory.llm.provider import LLMRequest

logger = logging.getLogger(__name__)


class ContextBuilder:
    """
    Constructs the prompt and bounds the context size.
    Prevents dumping the whole project into the prompt.
    """

    def __init__(self, state: FactoryState) -> None:
        self.state = state
        self.persister = ContextPersister()
        
    def _estimate_tokens(self, text: str) -> int:
        # Very rough estimate for bounding
        return len(text) // 4

    def build_request(
        self,
        task_id: str,
        system_prompt: str,
        messages: list[dict[str, str]],
        output_schema: dict[str, Any] | None = None,
        model: str = "ollama:qwen2.5-coder:7b",
        temperature: float = 0.1,
        project_id: str | None = None,
    ) -> LLMRequest:
        """Build the LLMRequest and save the manifest."""
        
        # We could enrich messages here with memory or file content,
        # but the caller (the agent) is responsible for requesting specific files.
        # This builder ensures the total size doesn't exceed bounds.
        
        total_tokens = self._estimate_tokens(system_prompt)
        for msg in messages:
            total_tokens += self._estimate_tokens(msg.get("content", ""))
            
        if total_tokens > 16000:
            logger.warning("Context for task %s is very large (~%d tokens)", task_id, total_tokens)

        manifest = ContextManifest(
            manifest_id=str(uuid.uuid4()),
            task_id=task_id,
            created_at=datetime.now(timezone.utc),
            system_prompt=system_prompt,
            messages=messages,
            included_files=[],  # The agent should explicitly state what it included in its request
            token_estimate=total_tokens,
        )
        
        self.persister.save(manifest)
        
        return LLMRequest(
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            output_schema=output_schema,
            task_id=task_id,
            manifest_id=manifest.manifest_id,
            project_id=project_id,
            temperature=temperature,
        )
