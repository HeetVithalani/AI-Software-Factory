"""
factory.llm.router
==================
Serializes and routes LLM requests. Ensures resource safety.
"""

from __future__ import annotations

import logging
import threading

from factory.errors import ModelUnavailableError
from factory.llm.audit import LLMCallAuditor
from factory.llm.provider import LLMRequest, LLMResponse, LLMProvider
from factory.llm.ollama import OllamaProvider
from factory.llm.sanitizer import sanitize_request

logger = logging.getLogger(__name__)

# Global semaphore to enforce resource concurrency limits.
# In Phase 2: strictly 1 active LLM inference at a time.
_inference_semaphore = threading.Semaphore(1)


class LLMRouter:
    """
    Routes an LLMRequest to the appropriate provider and records the audit log.
    Ensures that only one model runs at a time (VRAM protection).

    Routing rules (by model name prefix):
    - "ollama:..."   → OllamaProvider
    - "opencode:..." → OpenCodeProvider (when enabled in settings)
    - anything else  → OllamaProvider (fallback)
    """

    def __init__(self) -> None:
        from factory.config import get_settings
        settings = get_settings()

        self.auditor = LLMCallAuditor()
        self._providers: dict[str, LLMProvider] = {
            "ollama": OllamaProvider(),
        }

        # Register OpenCodeProvider if enabled in settings
        if settings.models.opencode.enabled:
            try:
                from factory.llm.opencode_provider import OpenCodeProvider
                ort = settings.opencode_runtime
                self._providers["opencode"] = OpenCodeProvider(
                    host=ort.host,
                    port=ort.port,
                    startup_timeout=ort.startup_timeout_seconds,
                    manage_server=ort.manage_server,
                )
                logger.info(
                    "LLMRouter: OpenCodeProvider registered "
                    "(server=%s:%d).", ort.host, ort.port,
                )
            except Exception as exc:
                logger.warning(
                    "LLMRouter: failed to initialise OpenCodeProvider: %s. "
                    "Falling back to Ollama only.", exc,
                )


    def complete(self, request: LLMRequest, retry_count: int = 0) -> LLMResponse:
        """
        Execute a request with concurrency protection and auditing.
        """
        request = sanitize_request(request)
        
        provider_name = "ollama"
        if ":" in request.model:
            provider_name = request.model.split(":")[0]
            
        provider = self._providers.get(provider_name)
        if not provider:
            if provider_name == "opencode":
                import uuid
                from datetime import datetime, timezone

                # If opencode is disabled, do NOT silently fallback to Ollama, and DO NOT hang.
                # Just return an immediate failed response.
                start_time = datetime.now(timezone.utc)
                response = LLMResponse(
                    request_id=str(uuid.uuid4()),
                    content="",
                    start_time=start_time,
                    end_time=start_time,
                    duration_ms=0.0,
                    success=False,
                    error="OpenCode provider is disabled in this environment.",
                )
                logger.error("LLMRouter: %s", response.error)
                self.auditor.record_call(request, response, retry_count=retry_count)
                return response

            # Fallback to ollama
            provider = self._providers["ollama"]
            
        logger.debug("LLMRouter: waiting for inference semaphore (model=%s)", request.model)
        
        with _inference_semaphore:
            logger.info("LLMRouter: executing inference (model=%s)", request.model)
            response = provider.complete(request)
            
        self.auditor.record_call(request, response, retry_count=retry_count)
        
        if not response.success:
            logger.warning("LLMRouter: inference failed: %s", response.error)
            
        return response
