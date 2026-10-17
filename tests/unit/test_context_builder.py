"""
Unit tests for ContextBuilder.
"""

from factory.context.builder import ContextBuilder
from factory.state import FactoryState

class _NullPersister:
    """Keeps model-selection tests from writing manifests to any real DB."""

    def save(self, manifest):  # noqa: D102 - test stub
        pass

def test_context_builder_estimate():
    builder = ContextBuilder(FactoryState())
    assert builder._estimate_tokens("hello world") == len("hello world") // 4

def test_context_builder_per_run_default_model_override():
    builder = ContextBuilder(
        FactoryState(), default_model="ollama:saluki-27b-iq2:latest"
    )
    builder.persister = _NullPersister()
    req = builder.build_request(task_id="t1", system_prompt="sys", messages=[])
    assert req.model == "ollama:saluki-27b-iq2:latest"

def test_context_builder_explicit_model_beats_per_run_default():
    builder = ContextBuilder(FactoryState(), default_model="per-run")
    builder.persister = _NullPersister()
    req = builder.build_request(
        task_id="t1", system_prompt="sys", messages=[], model="explicit",
    )
    assert req.model == "explicit"

def test_context_builder_falls_back_to_configured_default():
    from factory.config import get_settings
    builder = ContextBuilder(FactoryState())
    builder.persister = _NullPersister()
    req = builder.build_request(task_id="t1", system_prompt="sys", messages=[])
    assert req.model == get_settings().models.default_model

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
