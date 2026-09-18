"""
Unit tests for factory.llm.opencode_provider
Tests use mocking — no real opencode process is started.
"""

import json
import subprocess
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone

from factory.llm.opencode_provider import (
    OpenCodeProvider,
    _extract_assistant_text,
    _build_prompt_text,
    _port_open,
)
from factory.llm.structured_output import extract_json_from_text
from factory.llm.provider import LLMRequest, LLMResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(prompt="Hello", model="opencode:qwen2.5-coder:7b", timeout=30):
    return LLMRequest(
        model=model,
        system_prompt="You are a helpful assistant.",
        messages=[{"role": "user", "content": prompt}],
        task_id="test-task",
        timeout=timeout,
    )


def _json_event(content: str) -> str:
    return json.dumps({"type": "assistant", "content": content})


# ---------------------------------------------------------------------------
# _port_open
# ---------------------------------------------------------------------------

class TestPortOpen:
    def test_returns_false_on_closed_port(self):
        # Port 1 should never be open in tests
        assert _port_open("127.0.0.1", 1, timeout=0.2) is False

    def test_returns_true_when_open(self):
        # Use socket to create a listening server briefly
        import socket, threading
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            assert _port_open("127.0.0.1", port, timeout=1.0) is True


# ---------------------------------------------------------------------------
# _build_prompt_text
# ---------------------------------------------------------------------------

class TestBuildPromptText:
    def test_includes_system_prompt(self):
        req = _make_request()
        text = _build_prompt_text(req)
        assert "[SYSTEM]" in text
        assert "helpful assistant" in text

    def test_includes_user_message(self):
        req = _make_request(prompt="Tell me a joke.")
        text = _build_prompt_text(req)
        assert "Tell me a joke" in text

    def test_schema_appended_when_output_schema_set(self):
        req = _make_request()
        req.output_schema = {"type": "object", "properties": {"foo": {}}}
        text = _build_prompt_text(req)
        assert "JSON Schema" in text
        assert "[INSTRUCTION]" in text


# ---------------------------------------------------------------------------
# _extract_assistant_text
# ---------------------------------------------------------------------------

class TestExtractAssistantText:
    def test_assistant_event_type(self):
        events = [{"type": "assistant", "content": "Hello World"}]
        assert _extract_assistant_text(events) == "Hello World"

    def test_text_event_type(self):
        events = [{"type": "text", "text": "Foo"}]
        assert _extract_assistant_text(events) == "Foo"

    def test_content_block_delta(self):
        events = [{"type": "content_block_delta", "delta": {"text": "Bar"}}]
        assert _extract_assistant_text(events) == "Bar"

    def test_multiple_events_concatenated(self):
        events = [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": " World"},
        ]
        assert _extract_assistant_text(events) == "Hello World"

    def test_empty_events(self):
        assert _extract_assistant_text([]) == ""

    def test_non_dict_events_ignored(self):
        assert _extract_assistant_text(["not a dict", 42]) == ""


# ---------------------------------------------------------------------------
# OpenCodeProvider.complete()  — fully mocked
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    """Return a provider with manage_server=False (no server started)."""
    with patch("factory.llm.opencode_provider._find_opencode", return_value="opencode"):
        with patch("factory.llm.opencode_provider._port_open", return_value=False):
            p = OpenCodeProvider(manage_server=False)
    return p


class TestOpenCodeProviderComplete:
    def test_success_response(self, provider):
        stdout = _json_event('{"summary": "done", "completed": true}') + "\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = stdout
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            with patch.object(type(provider), "server_available",
                              new_callable=PropertyMock, return_value=False):
                response = provider.complete(_make_request())

        assert response.success is True
        assert "done" in response.content

    def test_timeout_returns_failure(self, provider):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("opencode", 30)):
            with patch.object(type(provider), "server_available",
                              new_callable=PropertyMock, return_value=False):
                response = provider.complete(_make_request(timeout=30))

        assert response.success is False
        assert "timed out" in response.error

    def test_oserror_returns_failure(self, provider):
        with patch("subprocess.run", side_effect=OSError("not found")):
            with patch.object(type(provider), "server_available",
                              new_callable=PropertyMock, return_value=False):
                response = provider.complete(_make_request())

        assert response.success is False

    def test_non_zero_exit_code_failure(self, provider):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error output"

        with patch("subprocess.run", return_value=mock_result):
            with patch.object(type(provider), "server_available",
                              new_callable=PropertyMock, return_value=False):
                response = provider.complete(_make_request())

        assert response.success is False

    def test_attach_used_when_server_available(self, provider):
        stdout = _json_event("hi") + "\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = stdout
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            with patch.object(type(provider), "server_available",
                              new_callable=PropertyMock, return_value=True):
                provider.complete(_make_request())

        call_args = mock_run.call_args[0][0]
        assert "--attach" in call_args

    def test_no_attach_when_server_unavailable(self, provider):
        stdout = _json_event("hi") + "\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = stdout
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            with patch.object(type(provider), "server_available",
                              new_callable=PropertyMock, return_value=False):
                provider.complete(_make_request())

        call_args = mock_run.call_args[0][0]
        assert "--attach" not in call_args

    def test_model_prefix_stripped(self, provider):
        stdout = _json_event("ok") + "\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = stdout
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            with patch.object(type(provider), "server_available",
                              new_callable=PropertyMock, return_value=False):
                req = _make_request(model="opencode:qwen2.5-coder:7b")
                provider.complete(req)

        call_args = mock_run.call_args[0][0]
        model_idx = call_args.index("-m") + 1
        assert call_args[model_idx] == "qwen2.5-coder:7b"

    def test_returns_lmresponse_type(self, provider):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _json_event("hello") + "\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            with patch.object(type(provider), "server_available",
                              new_callable=PropertyMock, return_value=False):
                response = provider.complete(_make_request())

        assert isinstance(response, LLMResponse)

    def test_no_executable_returns_failure(self):
        with patch("factory.llm.opencode_provider._find_opencode", return_value=None):
            p = OpenCodeProvider(manage_server=False)
            response = p.complete(_make_request())
        assert response.success is False
        assert "executable not found" in response.error
