"""
Tests for LLM provider abstraction.
"""

from factory.llm.provider import LLMRequest, LLMResponse
from factory.llm.ollama import OllamaProvider

def test_llm_request_creation():
    req = LLMRequest(
        model="ollama:qwen2.5-coder:7b",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert req.model == "ollama:qwen2.5-coder:7b"
    assert req.max_tokens == 4096
    
def test_llm_response_creation():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    res = LLMResponse(
        request_id="123",
        content="hello",
        start_time=now,
        end_time=now,
        duration_ms=100.0,
    )
    assert res.success is True
    assert res.content == "hello"
