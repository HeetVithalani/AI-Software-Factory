"""
factory.planning.generator
==========================
Deterministic plan generation for release-eligible and smoke pipelines.

The generator uses only:
* the user-supplied, normalised brief,
* observed local research, and
* a deterministic validation strategy.

It does not invent requirements, success criteria, architecture decisions or
alternative designs. Factory invariants (security stage, final audit, evidence
requirements) are explicitly labelled as factory requirements.

Pipelines
---------
``standard``
    build -> review -> test -> security -> documentation -> audit
    (review/documentation omitted under the fast profile; hardened adds a
    pre-build security baseline). Security and audit are never omitted.
``smoke``
    A single builder task, explicitly marked ``release_eligible=false``.
    Intended only for bounded model/CLI smoke checks, never for delivery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from factory.models import QualityProfile
from factory.planning.brief import ProjectBrief
from factory.planning.plan_store import PlanValidationError, PIPELINES
from factory.planning.research import ResearchRecord
from factory.projects.profiles import profile_policy, resolve_profile
from factory.validation.strategy import build_test_strategy


def _dedupe(values: list[str], limit: int = 20) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _requirements(brief: ProjectBrief) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    counter = 0
    for kind, values in (
        ("functional", brief.functional_requirements),
        ("non_functional", brief.non_functional_requirements),
    ):
        for value in values:
            counter += 1
            entries.append(
                {
                    "requirement_id": f"R{counter}",
                    "kind": kind,
                    "text": str(value),
                    "source": "project_brief",
                }
            )
    return entries


def _task(
    task_id: str,
    name: str,
    agent_type: str,
    description: str,
    depends_on: list[str],
    *,
    requirement_ids: list[str],
    acceptance: list[str],
    tools: list[str],
    profile: QualityProfile,
    pipeline: str,
    brief_id: str,
    research_id: str,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "requirement_ids": list(requirement_ids),
        "acceptance_criteria": _dedupe(acceptance),
        "required_tools": list(tools),
        "profile": profile.value,
        "pipeline": pipeline,
        "brief_id": brief_id,
        "research_id": research_id,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "task_id": task_id,
        "name": name,
        "agent_type": agent_type,
        "description": description,
        "depends_on": list(depends_on),
        "requirement_ids": list(requirement_ids),
        "acceptance_criteria": list(metadata["acceptance_criteria"]),
        "metadata": metadata,
    }


def generate_plan(
    project_id: str,
    brief: ProjectBrief,
    research: ResearchRecord | None = None,
    *,
    profile: str | QualityProfile | None = None,
    pipeline: str = "standard",
) -> dict[str, Any]:
    """
    Generate a validated plan payload (without identity/hash).

    Raises
    ------
    PlanValidationError
        If the workspace stack has required checks the factory cannot run, or
        the brief contains no requirements.
    """
    if pipeline not in PIPELINES:
        raise PlanValidationError(
            f"Unknown pipeline {pipeline!r}; expected one of {PIPELINES}"
        )
    resolved = resolve_profile(profile)
    policy = profile_policy(resolved)
    requirements = _requirements(brief)
    if not requirements:
        raise PlanValidationError(
            "The project brief contains no requirements; refusing to invent "
            "work items."
        )

    brief_id = brief.brief_id
    research_id = research.research_id if research else ""
    all_req_ids = [r["requirement_id"] for r in requirements]
    functional = [
        r for r in requirements if r["kind"] == "functional"
    ]
    functional_ids = [r["requirement_id"] for r in functional] or all_req_ids
    functional_texts = [r["text"] for r in functional] or [
        r["text"] for r in requirements
    ]

    problem = (
        brief.problem_statement.strip()
        or brief.intended_outcome.strip()
        or "Implement the supplied project requirements."
    )

    tasks: list[dict[str, Any]] = []

    if pipeline == "smoke":
        tasks.append(
            _task(
                "task-implement",
                "Implement",
                "builder",
                problem,
                [],
                requirement_ids=all_req_ids,
                acceptance=functional_texts
                + (brief.acceptance_criteria or []),
                tools=["filesystem", "shell"],
                profile=resolved,
                pipeline=pipeline,
                brief_id=brief_id,
                research_id=research_id,
                extra_metadata={"smoke_only": True},
            )
        )
    else:
        strategy = build_test_strategy(
            Path(brief.metadata.get("workspace_root", ".")),
            profile=resolved,
        )
        if strategy.unsupported_required:
            raise PlanValidationError(
                "Cannot generate a release-eligible plan: required checks "
                "are not executable by the factory:\n- "
                + "\n- ".join(strategy.unsupported_required)
            )

        baseline: list[str] = []
        if policy.pre_build_security:
            tasks.append(
                _task(
                    "task-security-baseline",
                    "Security baseline",
                    "security",
                    "Scan the pre-existing workspace before implementation "
                    "starts and record findings.",
                    [],
                    requirement_ids=all_req_ids,
                    acceptance=[
                        "Security scan completes and records evidence.",
                        "Any pre-existing critical/high finding is reported "
                        "before implementation.",
                    ],
                    tools=["security"],
                    profile=resolved,
                    pipeline=pipeline,
                    brief_id=brief_id,
                    research_id=research_id,
                    extra_metadata={"security_baseline": True},
                )
            )
            baseline = ["task-security-baseline"]

        tasks.append(
            _task(
                "task-implement",
                "Implement",
                "builder",
                problem,
                baseline,
                requirement_ids=functional_ids,
                acceptance=functional_texts
                + (brief.acceptance_criteria or []),
                tools=["filesystem", "shell"],
                profile=resolved,
                pipeline=pipeline,
                brief_id=brief_id,
                research_id=research_id,
            )
        )

        dependents_for_audit = ["task-implement"]

        if policy.include_reviewer:
            tasks.append(
                _task(
                    "task-review",
                    "Review",
                    "reviewer",
                    "Review the implementation against the requirements and "
                    "report blocking defects.",
                    ["task-implement"],
                    requirement_ids=all_req_ids,
                    acceptance=[
                        "A review report is produced.",
                        "No blocking defect remains for the delivered scope.",
                    ],
                    tools=["filesystem"],
                    profile=resolved,
                    pipeline=pipeline,
                    brief_id=brief_id,
                    research_id=research_id,
                )
            )
            dependents_for_audit.append("task-review")

        tasks.append(
            _task(
                "task-test",
                "Test",
                "qa",
                "Run the deterministic validation strategy and record test "
                "evidence.",
                ["task-implement"],
                requirement_ids=all_req_ids,
                acceptance=[
                    "Every required, supported check completes.",
                    "pytest exits successfully with at least one test and zero "
                    "failures or errors.",
                    "A test_report.json artifact is written and hashed as "
                    "evidence.",
                ],
                tools=["testing"],
                profile=resolved,
                pipeline=pipeline,
                brief_id=brief_id,
                research_id=research_id,
                extra_metadata={
                    "required_checks": strategy.required_check_names()
                    or ["pytest"],
                    "test_path": "tests" if (Path(
                        brief.metadata.get("workspace_root", ".")
                    ) / "tests").is_dir() else "",
                    "validation_strategy": strategy.to_dict(),
                    "allow_repair": policy.allow_repair,
                    "max_repair_cycles": policy.max_repair_cycles,
                },
            )
        )
        dependents_for_audit.append("task-test")

        tasks.append(
            _task(
                "task-security",
                "Security scan",
                "security",
                "Run the deterministic workspace security scan and record "
                "findings.",
                ["task-implement"],
                requirement_ids=all_req_ids,
                acceptance=[
                    "The security scan completes and writes "
                    "security_report.json.",
                    "Zero critical or high findings remain.",
                ],
                tools=["security"],
                profile=resolved,
                pipeline=pipeline,
                brief_id=brief_id,
                research_id=research_id,
                extra_metadata={"required_checks": ["workspace_security_scan"]},
            )
        )
        dependents_for_audit.append("task-security")

        if policy.include_documentation:
            doc_deps = ["task-implement"]
            if policy.include_reviewer:
                doc_deps.append("task-review")
            doc_deps.extend(["task-test", "task-security"])
            tasks.append(
                _task(
                    "task-docs",
                    "Documentation",
                    "documentation",
                    "Produce documentation for the delivered implementation.",
                    doc_deps,
                    requirement_ids=all_req_ids,
                    acceptance=[
                        "A documentation artifact is produced for the "
                        "delivered scope.",
                    ],
                    tools=["filesystem"],
                    profile=resolved,
                    pipeline=pipeline,
                    brief_id=brief_id,
                    research_id=research_id,
                )
            )
            dependents_for_audit.append("task-docs")

        tasks.append(
            _task(
                "task-audit",
                "Final audit",
                "auditor",
                "Run the deterministic final audit over plan approval, task "
                "completion, required reports, and evidence.",
                dependents_for_audit,
                requirement_ids=all_req_ids,
                acceptance=[
                    "The plan's exact hash is approved by a human.",
                    "All required non-auditor tasks are completed.",
                    "Required test and security evidence is present and "
                    "passing.",
                    "Every requirement is covered by completed work.",
                ],
                tools=["filesystem"],
                profile=resolved,
                pipeline=pipeline,
                brief_id=brief_id,
                research_id=research_id,
            )
        )

    decisions: list[dict[str, Any]] = []
    return {
        "project_id": project_id,
        "name": brief.name,
        "description": problem,
        "brief_id": brief_id,
        "research_id": research_id,
        "profile": resolved.value,
        "pipeline": pipeline,
        "release_eligible": pipeline != "smoke",
        "requirements": requirements,
        "tasks": tasks,
        "decisions": decisions,
        "decision_note": (
            "No architecture decisions were made automatically. The plan "
            "uses only the supplied requirements, the observed local stack, "
            "and the factory's non-negotiable security/audit policy."
        ),
        "assumptions": [],
        "open_questions": (
            list(research.summary.open_questions) if research else []
        ),
        "research": (
            {
                "findings": list(research.summary.findings),
                "sources": list(research.summary.sources),
                "limitations": list(research.limitations),
                "evidence_confidence": research.summary.evidence_confidence,
            }
            if research
            else {}
        ),
        "factory_requirements": [
            "Non-negotiable security scan with zero critical/high findings.",
            "Deterministic test evidence; no model verdict can override it.",
            "Final audit over plan approval, task completion, and evidence.",
        ],
    }
