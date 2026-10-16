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

    def __init__(self, state: FactoryState, skill_provider: Any = None) -> None:
        self.state = state
        # Phase 2c: optional skill-context provider (explicit per-run
        # integration point). None keeps pre-2c behavior byte-for-byte.
        self.skill_provider = skill_provider
        self.persister = ContextPersister()
        
    def _estimate_tokens(self, text: str) -> int:
        # Very rough estimate for bounding
        return len(text) // 4

    def build_request(
        self,
        task_id: str | None = None,
        system_prompt: str = "",
        messages: list[dict[str, str]] | None = None,
        output_schema: dict[str, Any] | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        project_id: str | None = None,
        response_schema: Any | None = None,
        agent_id: str | None = None,
        **kwargs: Any,
    ) -> LLMRequest:
        # Resolve response_schema alias if provided by specialist agents
        if response_schema is not None and output_schema is None:
            if hasattr(response_schema, "model_json_schema"):
                output_schema = response_schema.model_json_schema()
            elif isinstance(response_schema, dict):
                output_schema = response_schema

        if messages is None:
            messages = []
        if task_id is None:
            task_id = kwargs.get("task_id") or f"task-{uuid.uuid4().hex[:8]}"

        # Model selection: caller override → settings.models.default_model
        # This is the single authoritative model resolution point.
        if model is None:
            from factory.config import get_settings
            model = get_settings().models.default_model
        """Build the LLMRequest and save the manifest."""

        # Phase 2c: append explicitly project-enabled skill reference
        # context (bounded, attributed, untrusted-framed). The agent's
        # system prompt always comes FIRST so injected reference material
        # can never precede or replace policy. A failing provider injects
        # nothing rather than breaking the request.
        if self.skill_provider is not None:
            try:
                selection = self.skill_provider.select(
                    system_prompt=system_prompt,
                    messages=messages,
                    task_id=task_id,
                    project_id=project_id,
                    agent_id=agent_id,
                )
            except Exception as exc:
                logger.warning(
                    "Skill context provider failed for task %s; continuing "
                    "without skill context: %s", task_id, exc,
                )
            else:
                if selection.block:
                    system_prompt = (
                        f"{system_prompt}\n\n{selection.block}"
                        if system_prompt else selection.block
                    )

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
