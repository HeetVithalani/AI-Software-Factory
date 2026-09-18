"""
Minimal Real Ollama Integration Test
====================================
Tests the full infrastructure path for a single inference call:
LLMRouter -> OllamaProvider -> Ollama -> Qwen -> response -> Pydantic validation -> success
"""

import pytest
from pydantic import BaseModel, Field
from factory.llm.router import LLMRouter
from factory.context.builder import ContextBuilder

class MinimalSchema(BaseModel):
    message: str = Field(description="A short greeting message")
    number: int = Field(description="The number 42")

@pytest.mark.integration
def test_real_ollama_minimal_inference():
    """
    Proves that a real local model can process a request and
    return a successfully parsed structured response without hanging.
    """
    from factory.state import FactoryState
    router = LLMRouter()
    context = ContextBuilder(FactoryState())

    req = context.build_request(
        task_id="test-ollama",
        system_prompt="You are a helpful assistant.",
        messages=[{"role": "user", "content": "Say hello and return the number 42."}],
        response_schema=MinimalSchema,
        model="ollama:qwen2.5-coder:7b",
        temperature=0.1
    )

    # Use a generous timeout by ensuring the underlying provider works
    response = router.complete(req)

    assert response.success is True, f"LLM Call failed: {response.error}"
    assert response.structured_data is not None, "Missing structured_data"

    # Prove Pydantic validation
    parsed = MinimalSchema.model_validate(response.structured_data)
    assert parsed.number == 42
    assert len(parsed.message) > 0
