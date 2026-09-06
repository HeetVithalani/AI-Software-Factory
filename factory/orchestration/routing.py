"""
factory.orchestration.routing
==============================
Model routing — selects the best available model for a given task context.

The router considers:
- task type and complexity
- context window requirements
- privacy level (local-only if sensitive)
- model capability profile
- hardware resource availability
- speed vs. quality trade-off
- model availability (Ollama reachable?)
- fallback chain

Design note
-----------
Routing decisions are deterministic rule-based selections, NOT AI choices.
An LLM cannot route itself. The router uses the ModelRegistry to find
candidates that pass all hard constraints, then scores them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from factory.errors import ModelRoutingError
from factory.models import ModelRecord, ModelProvider, RiskLevel

logger = logging.getLogger(__name__)


@dataclass
class RoutingContext:
    """Context provided to the router for model selection."""
    # Type hint for the kind of task being run
    task_type: str = "general"
    # Estimated tokens needed (prompt + expected response)
    estimated_tokens: int = 2048
    # If True, must use a privacy-safe (local) model
    requires_privacy: bool = True
    # Minimum quality rating 1–10
    min_quality: int = 5
    # Maximum VRAM to commit
    max_vram_gb: float = 5.5
    # Maximum RAM to commit
    max_ram_gb: float = 10.0
    # Prefer faster models (lower quality ok)
    prefer_speed: bool = False
    # Risk level of the associated task
    risk_level: RiskLevel = RiskLevel.SAFE
    # Extra metadata for future routing extensions
    metadata: dict[str, Any] | None = None


class ModelRouter:
    """
    Selects the best available model for a given RoutingContext.

    Receives a list of ModelRecord candidates from the ModelRegistry and
    applies hard constraints + scoring to pick one.
    """

    def route(
        self,
        context: RoutingContext,
        candidates: list[ModelRecord],
    ) -> ModelRecord:
        """
        Select a model from candidates.

        Hard constraints (eliminate candidates):
        - model must be available
        - if requires_privacy → provider must be Ollama (local)
        - VRAM must not exceed context.max_vram_gb
        - RAM must not exceed context.max_ram_gb
        - quality_rating must meet min_quality
        - context_window must be >= estimated_tokens

        Scoring (among remaining):
        - if prefer_speed: rank by speed_rating DESC
        - else: rank by quality_rating DESC

        Falls back through candidates in preference order.
        """
        if not candidates:
            raise ModelRoutingError("No model candidates provided to router.")

        eligible: list[ModelRecord] = []

        for model in candidates:
            if not model.available:
                logger.debug(
                    "Router: skipping %s — not available.", model.model_id
                )
                continue

            if context.requires_privacy and model.provider != ModelProvider.OLLAMA:
                logger.debug(
                    "Router: skipping %s — privacy required, provider=%s.",
                    model.model_id, model.provider,
                )
                continue

            if model.capability.vram_gb > context.max_vram_gb:
                logger.debug(
                    "Router: skipping %s — VRAM %.1f > limit %.1f.",
                    model.model_id,
                    model.capability.vram_gb,
                    context.max_vram_gb,
                )
                continue

            if model.capability.ram_gb > context.max_ram_gb:
                logger.debug(
                    "Router: skipping %s — RAM %.1f > limit %.1f.",
                    model.model_id,
                    model.capability.ram_gb,
                    context.max_ram_gb,
                )
                continue

            if model.capability.quality_rating < context.min_quality:
                logger.debug(
                    "Router: skipping %s — quality %d < min %d.",
                    model.model_id,
                    model.capability.quality_rating,
                    context.min_quality,
                )
                continue

            if model.capability.context_window < context.estimated_tokens:
                logger.debug(
                    "Router: skipping %s — context %d < needed %d.",
                    model.model_id,
                    model.capability.context_window,
                    context.estimated_tokens,
                )
                continue

            eligible.append(model)

        if not eligible:
            raise ModelRoutingError(
                f"No eligible model found for task_type={context.task_type!r}, "
                f"privacy={context.requires_privacy}, "
                f"vram_limit={context.max_vram_gb}GB. "
                f"Checked {len(candidates)} candidate(s)."
            )

        # Score and sort
        def score(m: ModelRecord) -> int:
            if context.prefer_speed:
                return m.capability.speed_rating
            return m.capability.quality_rating

        eligible.sort(key=score, reverse=True)
        chosen = eligible[0]

        logger.info(
            "Router selected model %s (quality=%d, speed=%d, vram=%.1fGB) "
            "for task_type=%s.",
            chosen.model_id,
            chosen.capability.quality_rating,
            chosen.capability.speed_rating,
            chosen.capability.vram_gb,
            context.task_type,
        )
        return chosen

    def route_with_fallback(
        self,
        context: RoutingContext,
        candidates: list[ModelRecord],
        *,
        fallback_model: ModelRecord | None = None,
    ) -> ModelRecord:
        """
        Route with optional fallback if no candidate passes constraints.

        If routing fails and fallback_model is provided, return fallback.
        """
        try:
            return self.route(context, candidates)
        except ModelRoutingError:
            if fallback_model is not None:
                logger.warning(
                    "Router: using fallback model %s.", fallback_model.model_id
                )
                return fallback_model
            raise
