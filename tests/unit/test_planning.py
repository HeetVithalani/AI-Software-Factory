"""
Unit tests for factory.planning.* and supporting profile/strategy modules.

Covers the RPI persistence layer end to end at unit level:
- brief normalisation (sections, provenance, explicit unknowns)
- local research (findings, source hashes, honest limitations)
- deterministic plan generation (pipelines, profiles, fail-closed stacks)
- plan validation (traceability, acceptance, tools, smoke flag)
- plan store (versions, hash binding, tamper detection, decisions)

All databases live in pytest temp dirs; the repository factory.db is never
touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.database import get_connection, initialize_database
from factory.planning.brief import ProjectBrief, build_brief, latest_brief, load_brief, save_brief
from factory.planning.generator import generate_plan
from factory.planning.plan_store import (
    PlanApprovalError,
    PlanNotApprovedError,
    PlanNotFoundError,
    PlanStore,
    PlanValidationError,
    canonical_plan_hash,
    validate_plan,
)
from factory.planning.research import load_research, run_local_research
from factory.projects.profiles import profile_policy, resolve_profile
from factory.validation.strategy import build_test_strategy, detect_project_stack


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SPEC = """# Demo Project

## Goal
A tiny Python greeting module used by tests.

## Functional Requirements
- greet(name) returns "Hello, <name>!"
- greet("") raises ValueError

## Non-functional Requirements
- Python 3.11+ compatibility

## Acceptance Criteria
- pytest passes with at least one greeting test.

## Testing
- Run pytest.

## Security
- No user input is written to disk.
"""


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    (workspace / "README.md").write_text("# Demo\n", encoding="utf-8")
    return workspace


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "factory.db"
    initialize_database(db)
    return db


def _brief(tmp_path: Path) -> ProjectBrief:
    workspace = _make_workspace(tmp_path)
    return build_brief(
        "proj-1",
        SPEC,
        name="demo",
        source_kind="text",
        metadata={"workspace_root": str(workspace)},
    )


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

class TestProjectBrief:
    def test_sections_are_parsed(self, tmp_path):
        brief = _brief(tmp_path)
        assert len(brief.functional_requirements) == 2
        assert brief.functional_requirements[0].startswith("greet(name)")
        assert brief.non_functional_requirements
        assert brief.acceptance_criteria == [
            "pytest passes with at least one greeting test."
        ]
        assert brief.testing_expectations
        assert brief.security_constraints
        assert "No deadline was supplied." in brief.unknowns

    def test_original_spec_preserved_and_hashed(self, tmp_path):
        brief = _brief(tmp_path)
        assert brief.original_spec == SPEC
        assert len(brief.source_sha256) == 64
        assert brief.source_sha256 == build_brief(
            "proj-1", SPEC, name="demo"
        ).source_sha256

    def test_empty_spec_rejected(self):
        with pytest.raises(ValueError):
            build_brief("p", "   \n  ")

    def test_no_invented_requirements(self):
        """A spec with nothing requirement-like yields the whole text once."""
        brief = build_brief("p", "Just a free-form thought.")
        assert brief.functional_requirements == ["Just a free-form thought."]
        assert brief.non_functional_requirements == []
        assert "No acceptance criteria were supplied." in brief.unknowns

    def test_persistence_round_trip(self, tmp_path):
        db = _db(tmp_path)
        brief = _brief(tmp_path)
        save_brief(brief, db)
        loaded = load_brief(brief.brief_id, db)
        assert loaded is not None
        assert loaded.brief_id == brief.brief_id
        assert loaded.functional_requirements == brief.functional_requirements
        assert loaded.original_spec == SPEC
        assert latest_brief("proj-1", db).brief_id == brief.brief_id
        assert latest_brief("other", db) is None


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

class TestLocalResearch:
    def test_findings_and_sources(self, tmp_path):
        db = _db(tmp_path)
        brief = _brief(tmp_path)
        workspace = Path(brief.metadata["workspace_root"])
        record = run_local_research("proj-1", brief, workspace, db_path=db)

        findings = " ".join(record.summary.findings).lower()
        assert "python" in findings
        assert record.summary.sources
        spec_source = record.summary.sources[0]
        assert spec_source["source_type"] == "human_input"
        assert spec_source["metadata"]["sha256"] == brief.source_sha256
        readme = [
            s for s in record.summary.sources
            if s["name"] == "README.md"
        ]
        assert readme and len(readme[0]["metadata"]["sha256"]) == 64

    def test_external_research_declared_limitation(self, tmp_path):
        db = _db(tmp_path)
        brief = _brief(tmp_path)
        record = run_local_research(
            "proj-1", brief, Path(brief.metadata["workspace_root"]), db_path=db
        )
        assert any(
            "no external" in lim.lower() for lim in record.limitations
        )
        assert record.summary.metadata["external_research"] is False

    def test_persistence_round_trip(self, tmp_path):
        db = _db(tmp_path)
        brief = _brief(tmp_path)
        record = run_local_research(
            "proj-1", brief, Path(brief.metadata["workspace_root"]), db_path=db
        )
        loaded = load_research(record.research_id, db)
        assert loaded is not None
        assert loaded.summary.findings == record.summary.findings
        assert loaded.limitations == record.limitations

    def test_sensitive_files_never_become_sources(self, tmp_path):
        db = _db(tmp_path)
        brief = _brief(tmp_path)
        workspace = Path(brief.metadata["workspace_root"])
        (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
        record = run_local_research("proj-1", brief, workspace, db_path=db)
        references = [s["reference"] for s in record.summary.sources]
        assert ".env" not in references
        assert "SECRET" not in json.dumps(record.summary.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Profiles + strategy
# ---------------------------------------------------------------------------

class TestProfiles:
    def test_unknown_profile_fails_closed(self):
        with pytest.raises(Exception):
            resolve_profile("reckless")

    def test_policies_are_deterministic(self):
        fast = profile_policy("fast")
        hardened = profile_policy("hardened")
        assert fast.include_reviewer is False
        assert fast.allow_repair is False
        assert hardened.pre_build_security is True
        assert hardened.allow_repair is True


class TestStrategy:
    def test_python_stack_detected(self, tmp_path):
        workspace = _make_workspace(tmp_path)
        assert detect_project_stack(workspace) == "python"
        strategy = build_test_strategy(workspace, profile="balanced")
        assert "pytest" in strategy.required_check_names()
        assert "workspace_security_scan" in strategy.required_check_names()
        assert not strategy.unsupported_required

    def test_node_stack_is_explicitly_unsupported(self, tmp_path):
        workspace = tmp_path / "nodeapp"
        workspace.mkdir()
        (workspace / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8"
        )
        strategy = build_test_strategy(workspace)
        assert strategy.unsupported_required
        assert all(not c.supported for c in strategy.checks if c.required and c.kind == "test")

    def test_empty_workspace_uses_python_and_notes_greenfield(self, tmp_path):
        workspace = tmp_path / "empty"
        workspace.mkdir()
        strategy = build_test_strategy(workspace)
        assert strategy.stack == "empty"
        assert "pytest" in strategy.required_check_names()
        assert any("greenfield" in n.lower() for n in strategy.notes)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class TestGenerator:
    def _generate(self, tmp_path, profile="balanced", pipeline="standard"):
        brief = _brief(tmp_path)
        db = _db(tmp_path)
        research = run_local_research(
            brief.project_id, brief,
            Path(brief.metadata["workspace_root"]), db_path=db,
        )
        plan = generate_plan(
            brief.project_id, brief, research,
            profile=profile, pipeline=pipeline,
        )
        return brief, plan

    def test_standard_pipeline_has_required_stages(self, tmp_path):
        _, plan = self._generate(tmp_path)
        types = {t["agent_type"] for t in plan["tasks"]}
        assert {"builder", "qa", "security", "auditor"} <= types
        assert plan["release_eligible"] is True
        assert validate_plan(plan) == []

    def test_dependencies_are_ordered(self, tmp_path):
        _, plan = self._generate(tmp_path)
        by_id = {t["task_id"]: t for t in plan["tasks"]}
        assert by_id["task-review"]["depends_on"] == ["task-implement"]
        assert by_id["task-test"]["depends_on"] == ["task-implement"]
        assert "task-test" in by_id["task-audit"]["depends_on"]
        assert "task-security" in by_id["task-audit"]["depends_on"]

    def test_fast_profile_omits_review_and_docs_not_security(self, tmp_path):
        _, plan = self._generate(tmp_path, profile="fast")
        types = {t["agent_type"] for t in plan["tasks"]}
        assert "reviewer" not in types
        assert "documentation" not in types
        assert {"builder", "qa", "security", "auditor"} <= types
        assert plan["profile"] == "fast"

    def test_hardened_profile_adds_pre_build_baseline(self, tmp_path):
        _, plan = self._generate(tmp_path, profile="hardened")
        baseline = [
            t for t in plan["tasks"] if t["task_id"] == "task-security-baseline"
        ]
        assert baseline and baseline[0]["depends_on"] == []
        implement = [
            t for t in plan["tasks"] if t["task_id"] == "task-implement"
        ][0]
        assert implement["depends_on"] == ["task-security-baseline"]

    def test_smoke_pipeline_is_not_release_eligible(self, tmp_path):
        _, plan = self._generate(tmp_path, pipeline="smoke")
        assert len(plan["tasks"]) == 1
        assert plan["tasks"][0]["agent_type"] == "builder"
        assert plan["release_eligible"] is False
        assert validate_plan(plan) == []

    def test_unsupported_stack_fails_closed(self, tmp_path):
        workspace = tmp_path / "nodeapp"
        workspace.mkdir()
        (workspace / "package.json").write_text("{}", encoding="utf-8")
        brief = build_brief(
            "proj-node", "## Functional Requirements\n- serve a page\n",
            name="nodeapp", metadata={"workspace_root": str(workspace)},
        )
        with pytest.raises(PlanValidationError):
            generate_plan("proj-node", brief, None)

    def test_no_requirements_fails_closed(self, tmp_path):
        brief = ProjectBrief(
            project_id="p", name="x",
            metadata={"workspace_root": str(tmp_path)},
        )
        with pytest.raises(PlanValidationError):
            generate_plan("p", brief, None)


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------

def _minimal_plan() -> dict:
    return {
        "project_id": "p1",
        "pipeline": "standard",
        "profile": "balanced",
        "release_eligible": True,
        "requirements": [
            {"requirement_id": "R1", "kind": "functional", "text": "do it"}
        ],
        "tasks": [
            {
                "task_id": "t1",
                "name": "Build",
                "agent_type": "builder",
                "description": "build",
                "depends_on": [],
                "requirement_ids": ["R1"],
                "acceptance_criteria": ["works"],
                "metadata": {"required_tools": ["filesystem"]},
            },
            {
                "task_id": "t2",
                "name": "Test",
                "agent_type": "qa",
                "description": "test",
                "depends_on": ["t1"],
                "requirement_ids": ["R1"],
                "acceptance_criteria": ["passes"],
                "metadata": {
                    "required_tools": ["testing"],
                    "required_checks": ["pytest"],
                },
            },
            {
                "task_id": "t3",
                "name": "Security",
                "agent_type": "security",
                "description": "scan",
                "depends_on": ["t1"],
                "requirement_ids": ["R1"],
                "acceptance_criteria": ["clean"],
                "metadata": {"required_tools": ["security"]},
            },
            {
                "task_id": "t4",
                "name": "Audit",
                "agent_type": "auditor",
                "description": "audit",
                "depends_on": ["t2", "t3"],
                "requirement_ids": ["R1"],
                "acceptance_criteria": ["passes"],
                "metadata": {"required_tools": ["filesystem"]},
            },
        ],
    }


class TestValidatePlan:
    def test_minimal_plan_valid(self):
        assert validate_plan(_minimal_plan()) == []

    def test_missing_acceptance_criteria(self):
        plan = _minimal_plan()
        del plan["tasks"][0]["acceptance_criteria"]
        plan["tasks"][0]["metadata"].pop("acceptance_criteria", None)
        errors = validate_plan(plan)
        assert any("acceptance_criteria" in e for e in errors)

    def test_unknown_tool_capability(self):
        plan = _minimal_plan()
        plan["tasks"][0]["metadata"]["required_tools"] = ["kubernetes"]
        errors = validate_plan(plan)
        assert any("unknown tool capability" in e for e in errors)

    def test_uncovered_requirement(self):
        plan = _minimal_plan()
        plan["requirements"].append(
            {"requirement_id": "R2", "kind": "functional", "text": "second"}
        )
        errors = validate_plan(plan)
        assert any("R2" in e and "not covered" in e for e in errors)

    def test_smoke_plan_must_not_be_release_eligible(self):
        plan = _minimal_plan()
        plan["pipeline"] = "smoke"
        plan["release_eligible"] = True
        errors = validate_plan(plan)
        assert any("release_eligible" in e and "smoke" in e for e in errors)

    def test_standard_plan_requires_audit_stage(self):
        plan = _minimal_plan()
        plan["tasks"] = [t for t in plan["tasks"] if t["agent_type"] != "auditor"]
        errors = validate_plan(plan)
        assert any("auditor" in e for e in errors)

    def test_unknown_agent_type(self):
        plan = _minimal_plan()
        plan["tasks"][0]["agent_type"] = "wizard"
        errors = validate_plan(plan)
        assert any("unknown agent_type" in e for e in errors)

    def test_cycle_detected(self):
        plan = _minimal_plan()
        plan["tasks"][0]["depends_on"] = ["t2"]
        plan["tasks"][1]["depends_on"] = ["t1"]
        errors = validate_plan(plan)
        assert any("DAG validation failed" in e for e in errors)


# ---------------------------------------------------------------------------
# Plan store
# ---------------------------------------------------------------------------

class TestPlanStore:
    def _create(self, db: Path, plan: dict | None = None, project="p1"):
        store = PlanStore(db)
        version = store.create(
            project, plan or _minimal_plan(), created_by="test"
        )
        return store, version

    def test_create_and_load(self, tmp_path):
        db = _db(tmp_path)
        store, version = self._create(db)
        assert version.version == 1
        assert version.status == "pending"
        loaded = store.load(version.plan_id)
        assert loaded is not None
        assert loaded.plan_hash == version.plan_hash
        assert loaded.plan["plan_id"] == version.plan_id

    def test_canonical_hash_is_order_independent(self):
        plan_a = {"b": 1, "a": [1, 2]}
        plan_b = {"a": [1, 2], "b": 1}
        assert canonical_plan_hash(plan_a) == canonical_plan_hash(plan_b)

    def test_pending_plan_cannot_execute(self, tmp_path):
        db = _db(tmp_path)
        store, version = self._create(db)
        with pytest.raises(PlanNotApprovedError):
            store.require_approved(version.plan_id)

    def test_unknown_plan(self, tmp_path):
        store = PlanStore(_db(tmp_path))
        with pytest.raises(PlanNotFoundError):
            store.require_approved("missing")

    def test_approve_binds_exact_hash(self, tmp_path):
        db = _db(tmp_path)
        store, version = self._create(db)
        decision = store.decide(
            version.plan_id, "approved",
            actor="human", expected_hash=version.plan_hash,
        )
        assert decision.decision == "approved"
        approved = store.require_approved(version.plan_id)
        assert approved.plan_id == version.plan_id
        assert store.latest_approved("p1").plan_id == version.plan_id

    def test_wrong_hash_rejected(self, tmp_path):
        db = _db(tmp_path)
        store, version = self._create(db)
        with pytest.raises(PlanApprovalError):
            store.decide(
                version.plan_id, "approved",
                actor="human", expected_hash="0" * 64,
            )

    def test_empty_actor_rejected(self, tmp_path):
        db = _db(tmp_path)
        store, version = self._create(db)
        with pytest.raises(PlanApprovalError):
            store.decide(
                version.plan_id, "approved",
                actor="   ", expected_hash=version.plan_hash,
            )

    def test_rejection_blocks_execution(self, tmp_path):
        db = _db(tmp_path)
        store, version = self._create(db)
        store.decide(
            version.plan_id, "rejected",
            actor="human", expected_hash=version.plan_hash,
        )
        with pytest.raises(PlanNotApprovedError):
            store.require_approved(version.plan_id)

    def test_new_version_requires_new_approval(self, tmp_path):
        db = _db(tmp_path)
        store, v1 = self._create(db)
        store.decide(
            v1.plan_id, "approved",
            actor="human", expected_hash=v1.plan_hash,
        )
        v2 = store.create("p1", _minimal_plan())
        assert v2.version == 2
        assert store.load(v1.plan_id).status == "approved"
        with pytest.raises(PlanNotApprovedError):
            store.require_approved(v2.plan_id)
        # The old approval cannot be replayed onto the new version.
        with pytest.raises(PlanApprovalError):
            store.decide(
                v2.plan_id, "approved",
                actor="human", expected_hash=v1.plan_hash,
            )

    def test_tampered_plan_content_is_detected(self, tmp_path):
        db = _db(tmp_path)
        store, version = self._create(db)
        store.decide(
            version.plan_id, "approved",
            actor="human", expected_hash=version.plan_hash,
        )
        with get_connection(db) as conn:
            conn.execute(
                "UPDATE plans SET plan_json = ? WHERE plan_id = ?",
                (json.dumps({"project_id": "p1", "tasks": []}), version.plan_id),
            )
            conn.commit()
        with pytest.raises(PlanNotApprovedError):
            store.require_approved(version.plan_id)

    def test_latest_decision_wins(self, tmp_path):
        db = _db(tmp_path)
        store, version = self._create(db)
        store.decide(
            version.plan_id, "approved",
            actor="human", expected_hash=version.plan_hash,
        )
        store.decide(
            version.plan_id, "rejected",
            actor="human", expected_hash=version.plan_hash,
        )
        with pytest.raises(PlanNotApprovedError):
            store.require_approved(version.plan_id)

    def test_create_rejects_invalid_plan(self, tmp_path):
        db = _db(tmp_path)
        plan = _minimal_plan()
        plan["tasks"][0]["agent_type"] = "wizard"
        with pytest.raises(PlanValidationError):
            PlanStore(db).create("p1", plan)
