"""
Unit tests for the deterministic task-context collector and its wiring.

Verifies:
- deterministic selection (same workspace + task -> identical context)
- sensitive/binary files are never included, even when named by the task
- keyword and fixed-priority scoring select the right files first
- file/character budgets are enforced and truncation is flagged
- ContextBuilder records injected files in the manifest
- BuilderAgent injects the block and passes included_files through
"""

from __future__ import annotations

from factory.context.builder import ContextBuilder
from factory.context.collector import collect_for_task, collect_task_context
from factory.models import TaskRecord
from factory.state import FactoryState


def _task(description: str, acceptance: list[str] | None = None) -> TaskRecord:
    return TaskRecord(
        project_id="p1",
        name="Build",
        agent_type="builder",
        description=description,
        metadata={"acceptance_criteria": acceptance or []},
    )


class _RecordingPersister:
    def __init__(self):
        self.manifests = []

    def save(self, manifest):
        self.manifests.append(manifest)


class TestCollector:
    def test_deterministic_selection(self, tmp_path):
        (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
        (tmp_path / "routes.py").write_text("def route():\n    pass\n", encoding="utf-8")
        (tmp_path / "models.py").write_text("class Model: pass\n", encoding="utf-8")

        first = collect_task_context(
            tmp_path, task_description="extend routes.py with a new route"
        )
        second = collect_task_context(
            tmp_path, task_description="extend routes.py with a new route"
        )
        assert first.included_paths == second.included_paths
        assert first.render() == second.render()
        assert "routes.py" in first.included_paths

    def test_keyword_match_outranks_priority_names(self, tmp_path):
        (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
        (tmp_path / "payments.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "other.py").write_text("y = 2\n", encoding="utf-8")

        context = collect_task_context(
            tmp_path, task_description="fix the payments.py rounding bug"
        )
        assert context.included_paths[0] == "payments.py"

    def test_sensitive_files_never_included(self, tmp_path):
        (tmp_path / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
        (tmp_path / "server.key").write_text("PRIVATE\n", encoding="utf-8")
        (tmp_path / "id_rsa").write_text("ssh-private\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")

        context = collect_task_context(
            tmp_path, task_description="read .env server.key and id_rsa"
        )
        assert context.included_paths == ["app.py"]
        assert "SECRET" not in context.render()
        assert "ssh-private" not in context.render()
        reasons = dict(context.skipped)
        assert reasons.get(".env") == "sensitive file excluded"
        assert reasons.get("server.key") == "sensitive file excluded"
        assert reasons.get("id_rsa") == "sensitive file excluded"

    def test_binary_files_excluded(self, tmp_path):
        (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        context = collect_task_context(tmp_path, task_description="look at image.png")
        assert context.included_paths == ["notes.md"]

    def test_skip_dirs_and_hidden_dirs(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "lib.js").write_text("x", encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main\n", encoding="utf-8")
        context = collect_task_context(tmp_path)
        assert context.included_paths == ["src/main.py"]

    def test_budgets_enforced_and_truncation_flagged(self, tmp_path):
        for index in range(5):
            (tmp_path / f"f{index}.txt").write_text("a" * 4000, encoding="utf-8")
        context = collect_task_context(
            tmp_path, max_files=2, max_file_chars=100, max_total_chars=150,
        )
        assert len(context.files) == 2
        assert all(f.truncated for f in context.files)
        assert any(
            "budget" in reason for _path, reason in context.skipped
        )
        total = sum(len(content) for _path, content in context.blocks)
        assert total <= 150

    def test_empty_workspace_renders_nothing(self, tmp_path):
        context = collect_task_context(tmp_path)
        assert context.included_paths == []
        assert context.render() == ""

    def test_missing_workspace_is_empty_not_an_error(self, tmp_path):
        context = collect_task_context(tmp_path / "does-not-exist")
        assert context.included_paths == []

    def test_collect_for_task_uses_description(self, tmp_path):
        (tmp_path / "api.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
        context = collect_for_task(tmp_path, _task("update api.py"))
        assert "api.py" in context.included_paths


class TestContextBuilderInjection:
    def test_manifest_records_included_files_and_prompt_gets_block(self):
        persister = _RecordingPersister()
        builder = ContextBuilder(FactoryState())
        builder.persister = persister

        request = builder.build_request(
            task_id="t1",
            system_prompt="SYSTEM POLICY",
            messages=[{"role": "user", "content": "hi"}],
            context_block="--- README.md ---\n# Demo",
            included_files=["README.md"],
        )
        assert request.system_prompt.startswith("SYSTEM POLICY")
        assert "README.md" in request.system_prompt
        assert persister.manifests[0].included_files == ["README.md"]

    def test_manifest_defaults_to_empty_list(self):
        persister = _RecordingPersister()
        builder = ContextBuilder(FactoryState())
        builder.persister = persister
        builder.build_request(task_id="t1", system_prompt="s", messages=[])
        assert persister.manifests[0].included_files == []


class _RecordingRouter:
    def __init__(self, response):
        self._response = response
        self.requests = []

    def complete(self, request, retry_count: int = 0):
        self.requests.append(request)
        return self._response


class TestBuilderAgentInjection:
    def test_builder_injects_workspace_context(self, tmp_path):
        from factory.agents.builder import BuilderAgent
        from factory.llm.provider import LLMResponse
        from factory.llm.schemas.builder import BuilderProposal

        (tmp_path / "README.md").write_text("# Existing project\n", encoding="utf-8")

        response = LLMResponse(
            request_id="t1",
            content="",
            start_time=None,
            end_time=None,
            duration_ms=0.0,
            success=True,
            error="",
            structured_data=BuilderProposal(
                summary="finished", completed=True,
            ).model_dump(),
        )
        router = _RecordingRouter(response)
        persister = _RecordingPersister()
        context_builder = ContextBuilder(FactoryState())
        context_builder.persister = persister

        agent = BuilderAgent(
            workspace_root=tmp_path,
            llm_router=router,
            context_builder=context_builder,
        )
        result = agent.run(_task("implement the feature"))
        assert result.success is True

        assert router.requests, "builder must issue at least one request"
        prompt = router.requests[0].system_prompt
        assert "WORKSPACE CONTEXT" in prompt
        assert "# Existing project" in prompt
        assert persister.manifests[0].included_files == ["README.md"]

    def test_builder_without_workspace_still_works(self, tmp_path):
        from factory.agents.builder import BuilderAgent
        from factory.llm.provider import LLMResponse
        from factory.llm.schemas.builder import BuilderProposal

        response = LLMResponse(
            request_id="t1",
            content="",
            start_time=None,
            end_time=None,
            duration_ms=0.0,
            success=True,
            error="",
            structured_data=BuilderProposal(
                summary="finished", completed=True,
            ).model_dump(),
        )
        persister = _RecordingPersister()
        context_builder = ContextBuilder(FactoryState())
        context_builder.persister = persister

        agent = BuilderAgent(
            workspace_root=None,
            llm_router=_RecordingRouter(response),
            context_builder=context_builder,
        )
        result = agent.run(_task("do something"))
        assert result.success is True
        assert persister.manifests[0].included_files == []
