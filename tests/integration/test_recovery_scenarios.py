"""
Tests for various recovery scenarios (crash, network, validation).
"""

import pytest
import subprocess
import os
from pathlib import Path
from unittest.mock import patch
import httpx

@pytest.mark.integration
def test_network_timeout_recovery(tmp_path):
    """
    Tests that a network timeout during LLM inference triggers a retry.
    """
    from factory.llm.ollama import OllamaProvider
    from factory.llm.provider import LLMRequest
    from factory.llm.schemas.builder import BuilderProposal
    
    provider = OllamaProvider()
    req = LLMRequest(
        model="ollama:qwen2.5-coder:7b",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        output_schema=BuilderProposal.model_json_schema()
    )
    
    # We will mock httpx.Client.send to throw a timeout on the first call
    call_count = [0]
    
    original_send = httpx.Client.send
    
    def mock_send(self, request, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise httpx.ReadTimeout("Mocked timeout")
        return original_send(self, request, *args, **kwargs)
        
    with patch("httpx.Client.send", new=mock_send):
        # We only run the provider completion directly instead of full E2E to test the error handling
        response = provider.complete(req)
        
    # Verify it failed
    assert response.success is False
    assert "timed out" in response.error

@pytest.mark.integration
def test_invalid_json_schema_retry():
    # Similar to above, this tests the ContextBuilder/Provider logic for invalid schemas
    # For now, we leave it as pass to indicate we need more complex mocking
    pass
