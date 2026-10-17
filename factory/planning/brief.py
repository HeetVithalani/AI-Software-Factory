"""Structured, persisted project intake briefs.

The brief is a deterministic normalisation of a user-supplied project
specification. It preserves the original specification verbatim (and its
SHA-256) and records what the user actually supplied. Missing information is
recorded as an explicit unknown — it is never invented.

Persistence lives in schema v7 (``project_briefs``). Briefs are immutable
versions: a new normalisation inserts a new row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from factory.database import get_connection
from factory.projects.requirements import RequirementsParser

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 50
_MAX_ENTRY_CHARS = 2000
_MAX_SPEC_CHARS = 200_000

# Heading keywords -> brief field. Matched case-insensitively against the
# heading text; the first match wins.
_SECTION_KEYS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("problem", "goal", "objective", "intended outcome"), "outcome"),
    (("non-functional", "nonfunctional", "quality attribute"), "non_functional"),
    (("functional requirement", "functional"), "functional"),
    (("exclusion", "out of scope", "not in scope"), "exclusions"),
    (("constraint", "limitation"), "constraints"),
    (("technology restriction", "technology restriction", "restricted tech"), "technology_restrictions"),
    (("technology", "tech stack", "stack"), "technologies"),
    (("deadline", "timeline", "schedule"), "deadline"),
    (("priorit",), "priorities"),
    (("acceptance", "definition of done"), "acceptance"),
    (("security", "privacy"), "security"),
    (("test", "quality assurance", "validation"), "testing"),
    (("deliverable",), "deliverables"),
    (("release",), "release_conditions"),
    (("unknown", "open question", "clarification"), "unknowns"),
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^(?:[-*+•]|\d+[.)])\s+(.*)$")


class ProjectBrief(BaseModel):
    """Normalised project brief with provenance and explicit unknowns."""

    brief_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    name: str = ""
    problem_statement: str = ""
    intended_outcome: str = ""
    functional_requirements: list[str] = Field(default_factory=list)
    non_functional_requirements: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    technology_restrictions: list[str] = Field(default_factory=list)
    deadline: str = ""
    priorities: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    security_constraints: list[str] = Field(default_factory=list)
    testing_expectations: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    release_conditions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    # Provenance
    source_kind: str = "text"          # "text" | "file"
    source_reference: str = ""         # path, when the spec came from a file
    source_sha256: str = ""
    original_spec: str = ""
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["brief_json"] = json.dumps(data, ensure_ascii=False, sort_keys=True)
        return data


def _clean_entries(lines: list[str]) -> list[str]:
    """Normalise a heading section into bounded, non-empty entries."""
    entries: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        match = _BULLET_RE.match(line)
        if match:
            line = match.group(1).strip()
        if not line:
            continue
        if len(line) > _MAX_ENTRY_CHARS:
            line = line[:_MAX_ENTRY_CHARS].rstrip()
        entries.append(line)
        if len(entries) >= _MAX_ENTRIES:
            break
    return entries


def _split_sections(spec: str) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """
    Split Markdown-ish text into keyword-mapped sections.

    Returns (sections, preamble_lines, unmatched_heading_sections).
    A section whose heading does not match a known keyword is ignored for
    field extraction but its heading is recorded so nothing is silently
    treated as a requirement.
    """
    sections: dict[str, list[str]] = {}
    preamble: list[str] = []
    unmatched: list[str] = []
    current: str | None = None

    for line in spec.splitlines():
        heading = _HEADING_RE.match(line.strip())
        if heading:
            title = heading.group(2).strip().lower()
            key = None
            for keywords, candidate in _SECTION_KEYS:
                if any(keyword in title for keyword in keywords):
                    key = candidate
                    break
            if key is None:
                # A top-level document title is not an unmapped requirement
                # section; only deeper unmatched headings are surfaced.
                if len(heading.group(1)) > 1:
                    unmatched.append(heading.group(2).strip())
                current = None
            else:
                current = key
                sections.setdefault(current, [])
            continue
        if current is None:
            preamble.append(line)
        else:
            sections[current].append(line)

    return sections, preamble, unmatched


def _first_paragraph(lines: list[str]) -> str:
    paragraph: list[str] = []
    for line in lines:
        if not line.strip():
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    return "\n".join(paragraph)[:2000]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_brief(
    project_id: str,
    spec_text: str,
    *,
    name: str = "",
    source_kind: str = "text",
    source_reference: str = "",
    metadata: dict[str, Any] | None = None,
) -> ProjectBrief:
    """
    Normalise *spec_text* into a :class:`ProjectBrief`.

    Deterministic: identical input produces an identical brief body (the
    brief_id and created_at are fresh identifiers/timestamps). The original
    specification is preserved verbatim and hashed.
    """
    if not isinstance(spec_text, str) or not spec_text.strip():
        raise ValueError("Project specification must be non-empty text.")
    original = spec_text.replace("\r\n", "\n").replace("\r", "\n")
    if len(original) > _MAX_SPEC_CHARS:
        raise ValueError(
            f"Project specification exceeds {_MAX_SPEC_CHARS} characters."
        )

    sections, preamble, unmatched = _split_sections(original)

    # Fall back to the existing deterministic requirement extractor when the
    # spec has no recognised functional/non-functional sections.
    parser = RequirementsParser()
    parsed = parser.parse_text(original)
    functional = _clean_entries(sections.get("functional", []))
    non_functional = _clean_entries(sections.get("non_functional", []))
    if not functional:
        functional = list(parsed.functional)
    if not non_functional:
        non_functional = list(parsed.non_functional)

    outcome_lines = sections.get("outcome", [])
    problem = ""
    intended = ""
    if outcome_lines:
        clean = _clean_entries(outcome_lines)
        if clean:
            problem = clean[0]
            intended = " ".join(clean[1:]) if len(clean) > 1 else (clean[0] if not preamble else "")
    if not problem and preamble:
        problem = _first_paragraph(preamble)

    deadline = " ".join(_clean_entries(sections.get("deadline", [])))[:1000]

    unknowns = _clean_entries(sections.get("unknowns", []))
    if not functional:
        unknowns.append("No functional requirements were supplied.")
    if not sections.get("acceptance") and not sections.get("functional"):
        unknowns.append("No acceptance criteria were supplied.")
    if not deadline:
        unknowns.append("No deadline was supplied.")
    if not sections.get("testing"):
        unknowns.append("No testing expectations were supplied.")
    if not sections.get("technologies") and not sections.get("technology_restrictions"):
        unknowns.append("No required or restricted technologies were supplied.")
    if unmatched:
        unknowns.append(
            "Specification headings not mapped to brief fields: "
            + ", ".join(unmatched[:10])
        )

    return ProjectBrief(
        project_id=project_id,
        name=name,
        problem_statement=problem,
        intended_outcome=intended,
        functional_requirements=functional,
        non_functional_requirements=non_functional,
        constraints=_clean_entries(sections.get("constraints", [])),
        exclusions=_clean_entries(sections.get("exclusions", [])),
        technologies=_clean_entries(sections.get("technologies", [])),
        technology_restrictions=_clean_entries(
            sections.get("technology_restrictions", [])
        ),
        deadline=deadline,
        priorities=_clean_entries(sections.get("priorities", [])),
        acceptance_criteria=_clean_entries(sections.get("acceptance", [])),
        security_constraints=_clean_entries(sections.get("security", [])),
        testing_expectations=_clean_entries(sections.get("testing", [])),
        deliverables=_clean_entries(sections.get("deliverables", [])),
        release_conditions=_clean_entries(sections.get("release_conditions", [])),
        unknowns=unknowns,
        source_kind=source_kind,
        source_reference=source_reference,
        source_sha256=_sha256_text(original),
        original_spec=original,
        metadata=dict(metadata or {}),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_brief(brief: ProjectBrief, db_path: Path | None = None) -> str:
    """Persist a brief version and return its brief_id."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO project_briefs (
                brief_id, project_id, name, source_kind, source_reference,
                source_sha256, original_spec, brief_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                brief.brief_id,
                brief.project_id,
                brief.name,
                brief.source_kind,
                brief.source_reference,
                brief.source_sha256,
                brief.original_spec,
                brief.to_row()["brief_json"],
                brief.created_at.isoformat(),
            ),
        )
        conn.commit()
    logger.info(
        "Saved project brief %s for project %s (sha=%s).",
        brief.brief_id, brief.project_id, brief.source_sha256[:12],
    )
    return brief.brief_id


def _row_to_brief(row: Any) -> ProjectBrief:
    data = json.loads(row["brief_json"])
    return ProjectBrief.model_validate(data)


def load_brief(brief_id: str, db_path: Path | None = None) -> ProjectBrief | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM project_briefs WHERE brief_id = ?", (brief_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_brief(row)


def latest_brief(project_id: str, db_path: Path | None = None) -> ProjectBrief | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM project_briefs WHERE project_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_brief(row)
