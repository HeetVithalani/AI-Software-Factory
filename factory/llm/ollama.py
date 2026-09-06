"""
factory.llm.ollama
==================
Ollama HTTP provider implementation.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import httpx

from factory.config import get_settings
from factory.llm.provider import LLMProvider, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """
    Implements LLMProvider using Ollama's HTTP API.
    
    Uses httpx for synchronous requests. Expects `format: "json"` if an
    output_schema is provided.
    """

    def __init__(self, base_url: str | None = None) -> None:
        settings = get_settings()
        self.base_url = base_url or settings.models.ollama.base_url
        self.default_timeout = settings.models.ollama.request_timeout_seconds

    def complete(self, request: LLMRequest) -> LLMResponse:
        request_id = str(uuid.uuid4())
        
        # Ollama expects model name without the provider prefix
        model_name = request.model
        if model_name.startswith("ollama:"):
            model_name = model_name[7:]
            
        # Build prompt format for /api/chat
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        for msg in request.messages:
            messages.append(msg)
            
        payload = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            }
        }
        
        if request.output_schema:
            # We just tell Ollama to produce JSON. For more advanced Ollama versions, 
            # we can pass the actual JSON schema in `format`.
            # See Ollama docs: `format` can be a JSON schema object.
            payload["format"] = request.output_schema

        url = f"{self.base_url.rstrip('/')}/api/chat"
        timeout = httpx.Timeout(float(request.timeout or self.default_timeout))
        
        logger.debug("Ollama request [%s] to %s (model: %s)", request_id, url, model_name)
        
        start_time = datetime.now(timezone.utc)
        error_msg = ""
        success = False
        content = ""
        tokens_in = 0
        tokens_out = 0
        
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                
                content = data.get("message", {}).get("content", "")
                tokens_in = data.get("prompt_eval_count", 0)
                tokens_out = data.get("eval_count", 0)
                success = True
                
        except httpx.TimeoutException:
            error_msg = f"Ollama request timed out after {timeout.read}s"
            logger.error("Ollama %s", error_msg)
        except httpx.HTTPError as exc:
            error_msg = f"Ollama HTTP error: {exc}"
            logger.error("Ollama %s", error_msg)
        except Exception as exc:
            error_msg = f"Ollama unexpected error: {exc}"
            logger.error("Ollama %s", error_msg)
            
        end_time = datetime.now(timezone.utc)
        duration_ms = (end_time - start_time).total_seconds() * 1000.0

        structured_data = {}
        if success and request.output_schema:
            try:
                structured_data = json.loads(content)
            except json.JSONDecodeError:
                # We won't fail the request here, but schema validation will fail downstream
                pass

        return LLMResponse(
            request_id=request_id,
            content=content,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            success=success,
            error=error_msg,
            structured_data=structured_data,
        )
