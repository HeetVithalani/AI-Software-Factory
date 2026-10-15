"""
Tests for various recovery scenarios (crash, network, validation).
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers — exercise the real BuilderAgent retry loop with a fully mocked LLM
# (no network, no external services).
# ---------------------------------------------------------------------------

class _RecordingContext:
    """Stands in for ContextBuilder; snapshots every build_request call."""

    def __init__(self):
        self.calls = []

    def build_request(self, **kwargs):
        # Snapshot the messages list — the builder mutates it in place.
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs.get("messages", []))
        self.calls.append(snapshot)
        return MagicMock(model="stub:noop", task_id=kwargs.get("task_id"))


class _ScriptedRouter:
    """Returns scripted LLMResponse objects; the last one repeats."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.retry_counts = []

    def complete(self, request, retry_count=0):
        self.retry_counts.append(retry_count)
        idx = min(len(self.retry_counts) - 1, len(self._responses) - 1)
        return self._responses[idx]


def _llm_response(content: str):
    from factory.llm.provider import LLMResponse
    now = datetime.now(timezone.utc)
    return LLMResponse(
        request_id="recovery-test",
        content=content,
        start_time=now,
        end_time=now,
        duration_ms=1.0,
        success=True,
        structured_data={},
    )


def _make_builder_task():
    from factory.models import TaskRecord
    return TaskRecord(
        project_id="proj-recovery",
        name="Recovery Task",
        agent_type="builder",
        description="Finish the recovery task.",
        metadata={},
    )


@pytest.mark.integration
def test_network_timeout_recovery(tmp_path):
    """
    Tests that a network timeout during LLM inference produces a failed
    response (the provider itself does not retry; the RetryPolicy loop
    lives in BuilderAgent, covered by the tests below).
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
def test_invalid_json_schema_retry(tmp_path, monkeypatch):
    """
    Invalid JSON/schema output must go through the real retry loop in
    BuilderAgent (RetryPolicy): bounded retries, an augmented prompt that
    carries the parse error, and successful recovery once the model
    returns valid JSON. Covers all three rejection paths: no JSON found,
    JSON parse error, and schema validation failure.
    """
    from factory.agents.builder import BuilderAgent
    from factory.llm.retry import RetryPolicy

    # Backoff timing is not under test — record the attempts instead of sleeping.
    waits = []
    monkeypatch.setattr(RetryPolicy, "wait", lambda self, attempt: waits.append(attempt))

    router = _ScriptedRouter([
        _llm_response("this is not JSON"),
        _llm_response("still not JSON {broken"),
        # Valid JSON, wrong shape: missing the required `completed` field →
        # pydantic schema-validation failure.
        _llm_response(json.dumps({"summary": "done"})),
        _llm_response(json.dumps({"summary": "done", "completed": True})),
    ])
    context = _RecordingContext()
    agent = BuilderAgent(
        workspace_root=tmp_path, llm_router=router, context_builder=context
    )

    result = agent.run(_make_builder_task())

    # Successful recovery: a valid BuilderProposal arrives on the fourth attempt.
    assert result.success is True, result.errors
    assert result.outputs["turns"] == 1

    # Retry count: attempts 0, 1, 2, 3 — exactly three retries before recovery.
    assert router.retry_counts == [0, 1, 2, 3]
    # Exponential backoff was requested between the failed attempts.
    assert waits == [1, 2, 3]

    # The augmented prompt carrying the parse errors was actually sent on the
    # retries, and the final (successful) request still carried them.
    assert len(context.calls) == 4
    assert not any(
        "Previous output was invalid" in m.get("content", "")
        for m in context.calls[0]["messages"]
    )
    # The no-JSON error from the first failure was carried into the third request…
    assert any(
        "No valid JSON" in m.get("content", "")
        for m in context.calls[2]["messages"]
    )
    # …and the schema-validation error from the third failure was carried into
    # the final request's augmented prompt.
    final_messages = context.calls[-1]["messages"]
    assert any(
        "Previous output was invalid" in m.get("content", "")
        for m in final_messages
    )
    assert any(
        "Schema validation failed" in m.get("content", "")
        for m in final_messages
    )


@pytest.mark.integration
def test_invalid_json_retry_exhaustion_fails_closed(tmp_path, monkeypatch):
    """
    Retry exhaustion must fail the task (fail closed), never pass silently.
    RetryPolicy(max_retries=3) → attempts 0..3 (4 total LLM calls),
    backoff waits after attempts 1..3, then ESCALATE.
    """
    from factory.agents.builder import BuilderAgent
    from factory.llm.retry import RetryPolicy

    waits = []
    monkeypatch.setattr(RetryPolicy, "wait", lambda self, attempt: waits.append(attempt))

    router = _ScriptedRouter([_llm_response("garbage output, never valid")])
    context = _RecordingContext()
    agent = BuilderAgent(
        workspace_root=tmp_path, llm_router=router, context_builder=context
    )

    result = agent.run(_make_builder_task())

    assert result.success is False
    assert any("after retries" in e for e in result.errors)
    # Exactly max_retries + 1 total LLM calls — no unbounded retry storm.
    assert router.retry_counts == [0, 1, 2, 3]
    # Backoff between attempts; ESCALATE at attempt 3 skips the trailing wait.
    assert waits == [1, 2, 3]
