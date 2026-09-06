"""
factory.model_registry
=======================
Model registry — discovers, registers, and retrieves AI model records.

At boot the registry queries Ollama for installed models and builds
ModelRecord entries. Remote providers (OpenCode) are registered if their
credentials are present in the environment.

Design note
-----------
No model name is hardcoded. If Ollama has no models installed the factory
boots in "degraded" mode with a warning. A stub model is registered for
testing purposes only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from factory.errors import ModelUnavailableError
from factory.models import (
    ModelCapability,
    ModelProvider,
    ModelRecord,
)
from factory.orchestration.routing import ModelRouter, RoutingContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default capability profiles by model size tag
# ---------------------------------------------------------------------------

_SIZE_PROFILES: dict[str, ModelCapability] = {
    # Small models (~3-7B params)
    "small": ModelCapability(
        task_types=["general", "code", "qa"],
        context_window=8192,
        speed_rating=8,
        quality_rating=6,
        vram_gb=4.0,
        ram_gb=5.0,
        privacy_safe=True,
    ),
    # Medium models (~13B params)
    "medium": ModelCapability(
        task_types=["general", "code", "planning", "review"],
        context_window=16384,
        speed_rating=5,
        quality_rating=8,
        vram_gb=5.5,
        ram_gb=8.0,
        privacy_safe=True,
    ),
    # Large models (>30B params) — unlikely to fit 6 GB VRAM
    "large": ModelCapability(
        task_types=["general", "code", "planning", "review", "security"],
        context_window=32768,
        speed_rating=2,
        quality_rating=9,
        vram_gb=24.0,  # won't fit on this machine — router will filter
        ram_gb=32.0,
        privacy_safe=True,
    ),
}


def _infer_capability(model_name: str) -> ModelCapability:
    """
    Infer a capability profile from the Ollama model name/tag.

    This is a best-effort heuristic. Actual capability metadata would
    require reading the model card, which is not always available.
    """
    name_lower = model_name.lower()

    # Parameter count heuristics
    if any(tag in name_lower for tag in ("70b", "34b", "32b", "30b")):
        return _SIZE_PROFILES["large"]
    if any(tag in name_lower for tag in ("13b", "14b", "15b")):
        return _SIZE_PROFILES["medium"]

    # Coder models
    if "coder" in name_lower or "code" in name_lower:
        cap = ModelCapability(
            task_types=["code", "review", "testing"],
            context_window=8192,
            speed_rating=7,
            quality_rating=7,
            vram_gb=4.0,
            ram_gb=5.0,
            privacy_safe=True,
        )
        return cap

    # Default to small
    return _SIZE_PROFILES["small"]


class ModelRegistry:
    """
    Central registry of available AI models.

    Usage::

        registry = ModelRegistry()
        registry.discover()          # queries Ollama + checks env
        model = registry.get_model_for_task(context)
    """

    def __init__(self) -> None:
        self._models: dict[str, ModelRecord] = {}
        self._router = ModelRouter()
        self._stub_registered = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, record: ModelRecord) -> None:
        """Register a model record."""
        self._models[record.model_id] = record
        logger.debug("Registry: registered model %s.", record.model_id)

    def deregister(self, model_id: str) -> None:
        self._models.pop(model_id, None)

    def get(self, model_id: str) -> ModelRecord | None:
        return self._models.get(model_id)

    def all_models(self) -> list[ModelRecord]:
        return list(self._models.values())

    def available_models(self) -> list[ModelRecord]:
        return [m for m in self._models.values() if m.available]

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self, *, ollama_base_url: str = "http://localhost:11434") -> dict[str, Any]:
        """
        Discover available models from all configured providers.

        Returns a summary dict with discovery results.
        """
        summary: dict[str, Any] = {
            "ollama_models": [],
            "stub_models": [],
            "errors": [],
        }

        # --- Ollama ---
        try:
            self._discover_ollama(ollama_base_url, summary)
        except Exception as exc:  # noqa: BLE001
            msg = f"Ollama discovery failed: {exc}"
            logger.warning(msg)
            summary["errors"].append(msg)

        # --- Stub model (always registered for testing) ---
        self._register_stub()
        summary["stub_models"].append("stub:noop")

        if not self.available_models():
            logger.warning(
                "Model registry: no real models available. "
                "Factory will operate in degraded mode."
            )
        else:
            logger.info(
                "Model registry: discovered %d model(s): %s",
                len(self.available_models()),
                [m.model_id for m in self.available_models()],
            )

        return summary

    def _discover_ollama(
        self, base_url: str, summary: dict[str, Any]
    ) -> None:
        """Query Ollama /api/tags and register discovered models."""
        from factory.config import get_settings
        settings = get_settings()
        timeout = settings.models.ollama.request_timeout_seconds

        try:
            resp = httpx.get(
                f"{base_url}/api/tags",
                timeout=min(timeout, 10),  # use short timeout for discovery
            )
            resp.raise_for_status()
        except httpx.ConnectError:
            raise ModelUnavailableError(
                "Ollama is not reachable — is the Ollama service running?"
            )
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"Ollama HTTP error: {exc}")

        data = resp.json()
        models_data = data.get("models", [])

        for m in models_data:
            tag = m.get("name", "")
            if not tag:
                continue

            model_id = f"ollama:{tag}"
            capability = _infer_capability(tag)

            record = ModelRecord(
                model_id=model_id,
                display_name=tag,
                provider=ModelProvider.OLLAMA,
                provider_model_name=tag,
                capability=capability,
                available=True,
                last_checked_at=datetime.now(timezone.utc),
                tags=["local", "ollama"],
            )
            self.register(record)
            summary["ollama_models"].append(model_id)
            logger.info("Discovered Ollama model: %s", model_id)

    def _register_stub(self) -> None:
        """Register a no-op stub model used in testing."""
        if "stub:noop" in self._models:
            return
        stub = ModelRecord(
            model_id="stub:noop",
            display_name="Stub (no-op)",
            provider=ModelProvider.STUB,
            provider_model_name="noop",
            capability=ModelCapability(
                task_types=["general"],
                context_window=99999,
                speed_rating=10,
                quality_rating=1,
                vram_gb=0.0,
                ram_gb=0.1,
                privacy_safe=True,
            ),
            available=True,
            tags=["stub", "testing"],
        )
        self.register(stub)
        self._stub_registered = True

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def get_model_for_task(
        self,
        context: RoutingContext,
        *,
        exclude_stubs: bool = True,
    ) -> ModelRecord:
        """
        Select the best available model for a given RoutingContext.

        Raises ModelRoutingError if no eligible model is found.
        """
        candidates = [
            m for m in self.available_models()
            if not (exclude_stubs and m.provider == ModelProvider.STUB)
        ]

        if not candidates:
            # Fall back to stub if nothing else available
            stub = self.get("stub:noop")
            if stub:
                logger.warning(
                    "No real models available — falling back to stub."
                )
                return stub
            from factory.errors import ModelRoutingError
            raise ModelRoutingError(
                "No models available (not even stub) — cannot route task."
            )

        fallback = self.get("stub:noop") if not exclude_stubs else None
        return self._router.route_with_fallback(
            context, candidates, fallback_model=fallback
        )

    def mark_unavailable(self, model_id: str, reason: str = "") -> None:
        """Mark a model as temporarily unavailable (e.g. after timeout)."""
        if model_id in self._models:
            self._models[model_id].available = False
            logger.warning(
                "Model %s marked unavailable: %s", model_id, reason
            )
