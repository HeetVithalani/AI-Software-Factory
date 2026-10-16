"""
Unit tests for factory.cli.plan_loader
"""

import json
import pytest
from pathlib import Path
import tempfile
import os

from factory.cli.plan_loader import load_dag_from_plan, find_plan_path
from factory.errors import ConfigurationError
from factory.orchestration.dag import TaskDAG


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_PLAN = {
    "project_id": "test",
    "tasks": [
        {
            "task_id": "task-1",
            "name": "Step One",
            "agent_type": "planner",
            "depends_on": [],
            "description": "First task",
        },
        {
            "task_id": "task-2",
            "name": "Step Two",
            "agent_type": "builder",
            "depends_on": ["task-1"],
            "description": "Second task",
        },
    ],
}

FULL_PLAN_PATH = (
    Path(__file__).parent.parent / "fixtures" / "hello_project" / "plan.json"
)


def _write_plan(tmp_path: Path, data: dict) -> Path:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(data), encoding="utf-8")
    return plan_file


# ---------------------------------------------------------------------------
# Tests: load_dag_from_plan
# ---------------------------------------------------------------------------

class TestLoadDagFromPlan:
    def test_valid_plan_returns_dag_and_meta(self, tmp_path):
        plan_file = _write_plan(tmp_path, MINIMAL_PLAN)
        dag, plan = load_dag_from_plan(plan_file)
        assert isinstance(dag, TaskDAG)
        assert dag.node_count() == 2
        assert plan["project_id"] == "test"

    def test_correct_dependency_wired(self, tmp_path):
        plan_file = _write_plan(tmp_path, MINIMAL_PLAN)
        dag, _ = load_dag_from_plan(plan_file)
        node2 = dag.get("task-2")
        assert node2 is not None
        assert "task-1" in node2.depends_on

    def test_agent_type_preserved(self, tmp_path):
        plan_file = _write_plan(tmp_path, MINIMAL_PLAN)
        dag, _ = load_dag_from_plan(plan_file)
        assert dag.get("task-1").agent_type == "planner"
        assert dag.get("task-2").agent_type == "builder"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigurationError, match="not found"):
            load_dag_from_plan(tmp_path / "nonexistent.json")

    def test_invalid_json_raises(self, tmp_path):
        plan_file = tmp_path / "plan.json"
        plan_file.write_text("{bad json", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="not valid JSON"):
            load_dag_from_plan(plan_file)

    def test_empty_tasks_raises(self, tmp_path):
        plan_file = _write_plan(tmp_path, {"project_id": "x", "tasks": []})
        with pytest.raises(ConfigurationError, match="no 'tasks' list"):
            load_dag_from_plan(plan_file)

    def test_missing_task_id_raises(self, tmp_path):
        plan_file = _write_plan(tmp_path, {
            "tasks": [{"name": "oops", "agent_type": "builder"}]
        })
        with pytest.raises(ConfigurationError, match="task_id"):
            load_dag_from_plan(plan_file)

    def test_unknown_dependency_raises(self, tmp_path):
        plan_file = _write_plan(tmp_path, {
            "tasks": [
                {"task_id": "t1", "name": "T1", "depends_on": ["ghost-task"]}
            ]
        })
        with pytest.raises(ConfigurationError):
            load_dag_from_plan(plan_file)

    def test_cycle_raises(self, tmp_path):
        plan_file = _write_plan(tmp_path, {
            "tasks": [
                {"task_id": "a", "name": "A", "depends_on": ["b"]},
                {"task_id": "b", "name": "B", "depends_on": ["a"]},
            ]
        })
        with pytest.raises(ConfigurationError):
            load_dag_from_plan(plan_file)

    def test_description_in_metadata(self, tmp_path):
        plan_file = _write_plan(tmp_path, {
            "tasks": [
                {
                    "task_id": "t1",
                    "name": "T1",
                    "description": "Do stuff",
                    "depends_on": [],
                }
            ]
        })
        dag, _ = load_dag_from_plan(plan_file)
        assert dag.get("t1").metadata["description"] == "Do stuff"

    def test_full_hello_project_fixture(self):
        """Smoke test the real fixture plan."""
        assert FULL_PLAN_PATH.exists(), (
            f"Fixture plan not found at {FULL_PLAN_PATH}"
        )
        dag, plan = load_dag_from_plan(FULL_PLAN_PATH)
        assert dag.node_count() == 8
        assert plan["project_id"] == "hello-project"
        # All 7 agent types present
        types = {n.agent_type for n in dag.all_nodes()}
        assert "planner" in types
        assert "builder" in types
        assert "reviewer" in types
        assert "qa" in types
        assert "security" in types
        assert "documentation" in types
        assert "auditor" in types


class TestFindPlanPath:
    def test_explicit_overrides_all(self, tmp_path):
        explicit = tmp_path / "custom.json"
        explicit.touch()
        result = find_plan_path(tmp_path, explicit_plan=explicit)
        assert result == explicit.resolve()

    def test_project_dir_used_when_no_explicit(self, tmp_path):
        (tmp_path / "plan.json").touch()
        result = find_plan_path(tmp_path)
        assert result == (tmp_path / "plan.json")

    def test_fallback_to_cwd_plan(self, tmp_path):
        # no plan.json in tmp_path, no explicit
        result = find_plan_path(tmp_path)
        # Should return some path (may not exist — caller handles that)
        assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# Fail-closed agent_type validation (Phase 2c)
# ---------------------------------------------------------------------------

class TestAgentTypeValidation:
    def test_all_builtin_agent_types_pass(self, tmp_path):
        from factory.cli.plan_loader import BUILTIN_AGENT_TYPES
        plan = {
            "project_id": "p",
            "tasks": [
                {
                    "task_id": f"t-{agent_type}",
                    "name": agent_type.title(),
                    "agent_type": agent_type,
                    "depends_on": [],
                    "description": "step",
                }
                for agent_type in BUILTIN_AGENT_TYPES
            ],
        }
        dag, _ = load_dag_from_plan(_write_plan(tmp_path, plan))
        assert dag.node_count() == len(BUILTIN_AGENT_TYPES)

    def test_unknown_agent_type_rejected_before_execution(self, tmp_path):
        plan = {
            "tasks": [
                {
                    "task_id": "t1", "name": "T1",
                    "agent_type": "hacker-agent",
                    "depends_on": [], "description": "x",
                }
            ]
        }
        with pytest.raises(ConfigurationError, match="unknown agent_type"):
            load_dag_from_plan(_write_plan(tmp_path, plan))

    def test_agent_type_from_skill_content_rejected(self, tmp_path):
        # Nothing outside the built-in set — e.g. content a skill asked
        # to register — can pass load-time validation.
        plan = {
            "tasks": [
                {
                    "task_id": "t1", "name": "T1",
                    "agent_type": "skill-defined-executor",
                    "depends_on": [], "description": "x",
                }
            ]
        }
        with pytest.raises(ConfigurationError, match="unknown agent_type"):
            load_dag_from_plan(_write_plan(tmp_path, plan))

    def test_agent_type_is_case_sensitive(self, tmp_path):
        plan = {
            "tasks": [
                {
                    "task_id": "t1", "name": "T1",
                    "agent_type": "Builder",
                    "depends_on": [], "description": "x",
                }
            ]
        }
        with pytest.raises(ConfigurationError, match="unknown agent_type"):
            load_dag_from_plan(_write_plan(tmp_path, plan))

    def test_empty_agent_type_still_loads(self, tmp_path):
        # Backward compatibility: an empty agent_type loads as before and
        # fails later at dispatch (existing behavior, not this guard).
        plan = {
            "tasks": [
                {
                    "task_id": "t1", "name": "T1",
                    "depends_on": [], "description": "x",
                }
            ]
        }
        dag, _ = load_dag_from_plan(_write_plan(tmp_path, plan))
        assert dag.get("t1").agent_type == ""
