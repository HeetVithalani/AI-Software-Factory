"""
Unit tests for ContextBuilder.
"""

from factory.context.builder import ContextBuilder
from factory.state import FactoryState

def test_context_builder_estimate():
    builder = ContextBuilder(FactoryState())
    assert builder._estimate_tokens("hello world") == len("hello world") // 4

def test_context_builder_manifest_creation():
    builder = ContextBuilder(FactoryState())
    req = builder.build_request(
        task_id="t1",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        project_id="p1",
    )
    assert req.task_id == "t1"
    assert req.project_id == "p1"
    assert req.manifest_id is not None
