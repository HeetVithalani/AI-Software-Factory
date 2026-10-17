"""
factory.planning.research
==========================
Deterministic local research pass for the RPI workflow.

This service inspects the project workspace and the supplied brief, records
what it actually observed (with file paths and content hashes), and writes a
persisted :class:`~factory.research.researcher.ResearchSummary`.

Honesty rules
-------------
* No network access. External research is *not* performed and this is
  recorded as an explicit limitation on every summary.
* Findings are observations about local files (existence, stack markers,
  test layout) — never guesses about the wider ecosystem.
* ``evidence_confidence`` is derived from the trust levels of the local
  sources that were actually read, not from model output.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from factory.database import get_connection
from factory.planning.brief import ProjectBrief
from factory.research.researcher import ResearchSummary
from factory.research.sources import ResearchSource

logger = logging.getLogger(__name__)

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".tox", ".idea", ".vscode", "site-packages",
}
_SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".pyd", ".dll", ".so", ".dylib", ".exe", ".bin",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".tar",
    ".gz", ".7z", ".rar", ".db", ".sqlite", ".sqlite3", ".gguf",
}
_MAX_SCAN_FILES = 400
_MAX_READ_BYTES = 128 * 1024
_SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
    "credentials", "credentials.json", "secrets.json", "secrets.yaml",
    "secrets.yml", ".netrc", ".npmrc", ".pypirc",
}
_READ_CANDIDATES = (
    "README.md", "README.rst", "README.txt", "readme.md",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Pipfile", "poetry.lock", "package.json", "tsconfig.json",
    "Cargo.toml", "go.mod", "Makefile", "Dockerfile",
    "requirements.yaml", "REQUIREMENTS.md",
)


class ResearchRecord(BaseModel):
    """A persisted research run."""

    research_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    brief_id: str = ""
    topic: str = ""
    summary: ResearchSummary
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name in _SENSITIVE_NAMES:
        return True
    if name.startswith(".env"):
        return True
    return False


def _iter_files(root: Path) -> list[Path]:
    """Depth-first, deterministic file walk with bounded size and skips."""
    files: list[Path] = []

    def _walk(directory: Path) -> None:
        if len(files) >= _MAX_SCAN_FILES:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if len(files) >= _MAX_SCAN_FILES:
                return
            try:
                if entry.is_dir():
                    if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                        continue
                    _walk(entry)
                elif entry.is_file():
                    if entry.suffix.lower() in _SKIP_SUFFIXES:
                        continue
                    if _is_sensitive(entry):
                        continue
                    files.append(entry)
            except OSError:
                continue

    if root.is_dir():
        _walk(root)
    return files


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()[:_MAX_READ_BYTES]
    except OSError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _detect_stack(files: list[Path]) -> tuple[str, list[str]]:
    """Return (primary_stack, marker_names) from observed files."""
    names = {f.name.lower() for f in files}
    suffixes = {f.suffix.lower() for f in files}
    markers: list[str] = []

    def _mark(name: str, label: str) -> None:
        if name.lower() in names:
            markers.append(label)

    _mark("pyproject.toml", "pyproject.toml")
    _mark("setup.py", "setup.py")
    _mark("requirements.txt", "requirements.txt")
    _mark("package.json", "package.json")
    _mark("cargo.toml", "Cargo.toml")
    _mark("go.mod", "go.mod")
    _mark("pom.xml", "pom.xml")

    if ".py" in suffixes or (
        {"pyproject.toml", "setup.py", "requirements.txt"} & set(markers)
    ):
        return "python", markers
    if ".ts" in suffixes or "package.json" in markers:
        return "node", markers
    if ".rs" in suffixes or "Cargo.toml" in markers:
        return "rust", markers
    if ".go" in suffixes or "go.mod" in markers:
        return "go", markers
    if ".java" in suffixes or "pom.xml" in markers:
        return "java", markers
    if not suffixes:
        return "empty", markers
    return "unknown", markers


def _find_test_layout(root: Path, files: list[Path]) -> list[str]:
    findings: list[str] = []
    test_files = [
        f for f in files
        if f.name.startswith("test_") or f.name.endswith("_test.py")
        or f.name.endswith(".test.ts") or f.name.endswith(".test.js")
    ]
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        findings.append("A 'tests/' directory exists.")
    if test_files:
        findings.append(
            f"{len(test_files)} test file(s) were found (e.g. "
            f"{', '.join(str(f.relative_to(root)) for f in test_files[:3])})."
        )
    if not test_files and not tests_dir.is_dir():
        findings.append(
            "No test suite was discovered in the workspace; QA must fail "
            "closed until tests exist."
        )
    return findings


def run_local_research(
    project_id: str,
    brief: ProjectBrief,
    workspace_root: Path,
    *,
    db_path: Path | None = None,
) -> ResearchRecord:
    """
    Perform the deterministic local research pass and persist it.

    *workspace_root* is resolved first; the workspace is never modified.
    """
    root = Path(workspace_root).resolve()
    files = _iter_files(root)
    limitations: list[str] = [
        "No external/network research was performed: the factory has no "
        "approved outbound research tool configured for this run.",
        "Findings describe only local workspace contents at the recorded "
        "time; they are observations, not architectural recommendations.",
    ]

    stack, markers = _detect_stack(files)
    summary = ResearchSummary(
        project_id=project_id,
        task_id=None,
        topic=f"Local workspace research for {brief.name or project_id}",
        confidence_basis=(
            "Based on direct reading of local workspace files "
            f"({len(files)} file(s) inventoried)."
        ),
        metadata={
            "method": "local_workspace_inspection",
            "external_research": False,
            "files_inventoried": len(files),
            "workspace_root": str(root),
        },
    )

    summary.add_finding(
        f"Workspace inventory: {len(files)} file(s) outside ignored "
        "directories (VCS, virtualenvs, caches, binaries)."
    )
    if stack == "empty":
        summary.add_finding(
            "The workspace currently contains no source files; this is a "
            "greenfield project."
        )
    elif stack == "unknown":
        summary.add_finding(
            "No recognised language/stack markers were observed; the "
            "primary stack could not be determined from local files."
        )
    else:
        summary.add_finding(
            f"Primary stack appears to be '{stack}' (markers: "
            f"{', '.join(markers) if markers else 'source files only'})."
        )
    for finding in _find_test_layout(root, files):
        summary.add_finding(finding)

    # The human-supplied specification is the highest-trust local source.
    summary.add_source(
        ResearchSource(
            source_type="human_input",
            name="Project specification",
            reference=brief.source_reference or "inline specification",
            trust_level=5,
            metadata={"sha256": brief.source_sha256, "brief_id": brief.brief_id},
        )
    )

    # Read known documentation/config candidates for provenance.
    for candidate in _READ_CANDIDATES:
        path = root / candidate
        if not path.is_file() or _is_sensitive(path):
            continue
        text = _read_text(path)
        if text is None:
            continue
        summary.add_source(
            ResearchSource(
                source_type="documentation",
                name=candidate,
                reference=str(path.relative_to(root)),
                trust_level=4,
                metadata={
                    "sha256": _sha256_bytes(text.encode("utf-8")),
                    "bytes": len(text.encode("utf-8")),
                },
            )
        )

    # External source references supplied by the user are recorded but not
    # retrieved — provenance without false claims.
    refs_file = root / "research_sources.json"
    if refs_file.is_file():
        try:
            refs = json.loads(refs_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            refs = []
            limitations.append(
                "research_sources.json exists but could not be parsed; no "
                "references were recorded."
            )
        if isinstance(refs, list):
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                name = str(ref.get("name", "external reference"))
                url = str(ref.get("url", ""))
                summary.add_source(
                    ResearchSource(
                        source_type="external_url",
                        name=name,
                        reference=url,
                        trust_level=1,
                        metadata={"retrieved": False},
                    )
                )
            if refs:
                limitations.append(
                    "User-supplied external references were recorded but not "
                    "retrieved; their claims are unverified."
                )

    for question in brief.unknowns:
        summary.add_open_question(question)

    source_count = len(summary.sources)
    high_trust = sum(
        1 for s in summary.sources if int(s.get("trust_level", 0)) >= 4
    )
    summary.evidence_confidence = round(
        (high_trust / source_count) if source_count else 0.0, 3
    )

    record = ResearchRecord(
        project_id=project_id,
        brief_id=brief.brief_id,
        topic=summary.topic,
        summary=summary,
        limitations=limitations,
    )
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO research_records (
                research_id, project_id, brief_id, topic,
                summary_json, limitations_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.research_id,
                record.project_id,
                record.brief_id,
                record.topic,
                record.summary.model_dump_json(),
                json.dumps(record.limitations, ensure_ascii=False),
                record.created_at.isoformat(),
            ),
        )
        conn.commit()

    logger.info(
        "Local research %s for project %s: %d finding(s), %d source(s).",
        record.research_id, project_id, len(summary.findings),
        len(summary.sources),
    )
    return record


def load_research(
    research_id: str, db_path: Path | None = None
) -> ResearchRecord | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM research_records WHERE research_id = ?",
            (research_id,),
        ).fetchone()
    if row is None:
        return None
    return ResearchRecord(
        research_id=row["research_id"],
        project_id=row["project_id"],
        brief_id=row["brief_id"],
        topic=row["topic"],
        summary=ResearchSummary.model_validate_json(row["summary_json"]),
        limitations=json.loads(row["limitations_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
