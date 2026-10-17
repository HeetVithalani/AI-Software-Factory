"""
factory.validation.strategy
============================
Deterministic, task-specific validation strategy.

The strategy is derived only from observed workspace files and the project
profile. It never claims a check exists that the factory cannot execute:

* ``supported=True`` checks are run by a concrete tool (currently pytest via
  :class:`factory.tools.testing.TestingTool`, and the workspace security scan
  via :class:`factory.tools.security.SecurityTool`).
* A required check with ``supported=False`` blocks release-eligible plan
  creation instead of being silently skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from factory.models import QualityProfile
from factory.projects.profiles import profile_policy

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", "dist", "build",
}


@dataclass(frozen=True)
class CheckSpec:
    """One validation check and whether the factory can actually run it."""

    name: str
    kind: str                     # "test" | "security" | "lint"
    command: str
    reason: str
    required: bool = True
    supported: bool = True


@dataclass(frozen=True)
class TestStrategy:
    """The deterministic validation plan for a project."""

    stack: str
    checks: list[CheckSpec] = field(default_factory=list)
    unsupported_required: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def required_check_names(self) -> list[str]:
        return [
            c.name for c in self.checks if c.required and c.supported
        ]

    def to_dict(self) -> dict:
        return {
            "stack": self.stack,
            "checks": [
                {
                    "name": c.name,
                    "kind": c.kind,
                    "command": c.command,
                    "reason": c.reason,
                    "required": c.required,
                    "supported": c.supported,
                }
                for c in self.checks
            ],
            "unsupported_required": list(self.unsupported_required),
            "notes": list(self.notes),
        }


def _walk_files(root: Path, limit: int = 500) -> list[Path]:
    result: list[Path] = []

    def _walk(directory: Path) -> None:
        if len(result) >= limit:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if len(result) >= limit:
                return
            try:
                if entry.is_dir():
                    if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                        continue
                    _walk(entry)
                elif entry.is_file():
                    result.append(entry)
            except OSError:
                continue

    if root.is_dir():
        _walk(root)
    return result


def detect_project_stack(root: Path) -> str:
    files = _walk_files(root)
    names = {f.name.lower() for f in files}
    suffixes = {f.suffix.lower() for f in files}
    if ".py" in suffixes or {
        "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"
    } & names:
        return "python"
    if ".ts" in suffixes or ".tsx" in suffixes:
        return "node"
    if ".js" in suffixes or "package.json" in names:
        return "node"
    if ".rs" in suffixes or "cargo.toml" in names:
        return "rust"
    if ".go" in suffixes or "go.mod" in names:
        return "go"
    if ".java" in suffixes or "pom.xml" in names:
        return "java"
    if not files:
        return "empty"
    return "unknown"


def build_test_strategy(
    root: Path,
    *,
    profile: str | QualityProfile | None = None,
) -> TestStrategy:
    """
    Build the deterministic validation strategy for *root*.

    The profile never changes which checks are *known*; it only affects
    whether the full suite is required and how much repair is allowed.
    """
    policy = profile_policy(profile)
    stack = detect_project_stack(root)
    checks: list[CheckSpec] = []
    unsupported: list[str] = []
    notes: list[str] = []

    if stack in ("python", "empty"):
        test_files = [
            f for f in _walk_files(root)
            if f.name.startswith("test_") or f.name.endswith("_test.py")
        ]
        tests_dir = (root / "tests").is_dir()
        if test_files or tests_dir:
            reason = "Discovered a Python test suite; QA runs pytest."
        else:
            reason = (
                "No Python test suite was discovered. QA still runs pytest; "
                "pytest's 'no tests collected' result must fail closed."
            )
        checks.append(
            CheckSpec(
                name="pytest",
                kind="test",
                command="python -m pytest",
                reason=reason,
                required=True,
                supported=True,
            )
        )
        checks.append(
            CheckSpec(
                name="workspace_security_scan",
                kind="security",
                command="",
                reason=(
                    "Workspace security scan must complete and report zero "
                    "critical/high findings."
                ),
                required=True,
                supported=True,
            )
        )
        if stack == "empty":
            notes.append(
                "Greenfield workspace: no source or test files exist yet. "
                "The builder must create tests for QA to pass."
            )
    elif stack == "node":
        package_json = root / "package.json"
        has_script = False
        try:
            import json
            data = json.loads(package_json.read_text(encoding="utf-8"))
            has_script = bool(
                isinstance(data, dict)
                and isinstance(data.get("scripts"), dict)
                and data["scripts"].get("test")
            )
        except (OSError, ValueError):
            has_script = False
        if has_script:
            checks.append(
                CheckSpec(
                    name="npm_test",
                    kind="test",
                    command="npm test",
                    reason="package.json declares a test script.",
                    required=True,
                    supported=False,
                )
            )
            unsupported.append(
                "npm_test: the factory has no Node test executor; install "
                "one through the approved tool-intake workflow first."
            )
        else:
            unsupported.append(
                "node test runner: no package.json test script was found "
                "and the factory has no built-in Node executor."
            )
        checks.append(
            CheckSpec(
                name="workspace_security_scan",
                kind="security",
                command="",
                reason=(
                    "Workspace security scan must complete and report zero "
                    "critical/high findings."
                ),
                required=True,
                supported=True,
            )
        )
    else:
        if stack == "unknown":
            unsupported.append(
                "unknown stack: no recognised project manifest or source "
                "files were found. Add pyproject.toml/setup.py/"
                "requirements.txt (Python) or use the approved tool-intake "
                "workflow for another stack before planning."
            )
        else:
            unsupported.append(
                f"{stack}: no supported test executor exists for this stack; "
                "the factory will not claim a test was run."
            )

    if policy.require_full_test_suite:
        notes.append("Profile requires the full discovered suite, not a subset.")
    else:
        notes.append(
            "Fast profile permits a narrowed test path when the plan "
            "declares one; the full suite is not guaranteed."
        )

    return TestStrategy(
        stack=stack,
        checks=checks,
        unsupported_required=unsupported,
        notes=notes,
    )
