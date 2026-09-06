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
    """

    def __init__(self) -> None:
        self.auditor = LLMCallAuditor()
        self._providers: dict[str, LLMProvider] = {
            "ollama": OllamaProvider(),
        }

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
