"""
factory.skills.bindings
========================
Project-scoped enablement of installed skills (Phase 2c).

A *binding* records that a human explicitly enabled one INSTALLED,
clean, approval-bound, hash-verified skill revision as untrusted
reference context for one resolved project workspace.

Invariants enforced here (fail-closed, no overrides):
* The tool must be INSTALLED — revoked / stale / rejected / uninstalled
  tools can never be enabled (the REVOKED kill-switch is terminal).
* The exact requested revision must exist, have been installed, have a
  completed CLEAN scan, and carry a full approval hash binding.
* The install directory must be the canonical
  ``<install_root>/<tool_id>/<revision_sha>`` path (no symlink, no
  traversal, strictly under the resolved install root) and its content
  tree must still match the recorded tree hash (tamper check).
* SKILL.md must exist inside the install directory (a skill without
  reference content is not enableable).
* A mandatory audit note and actor identity are recorded verbatim.

Project disable (``disable_skill``) removes ONLY the project binding —
it never revokes, deletes, or mutates the globally installed tool.

Nothing in this module executes, imports, or interprets skill content;
bindings are read exclusively by the skill-context provider.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from factory.database import get_connection
from factory.errors import (
    IntakeIntegrityError,
    IntakePolicyError,
    IntakeStateError,
)
from factory.intake import registry
from factory.intake.archive import hash_tree
from factory.models import (
    IntakeStatus,
    ScanState,
    SkillBinding,
    ToolKind,
    ToolRecord,
    ToolRevision,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sval(x: Any) -> str:
    """Model enum fields are plain str (use_enum_values); tolerate enums."""
    return x.value if hasattr(x, "value") else str(x)


def project_key_for(project_path: Path | str) -> str:
    """
    Canonical per-project identity used by BOTH the enable/disable CLI
    and the runtime provider: the same resolved workspace path always
    yields the same key, so enablement and retrieval can never disagree.
    """
    resolved = Path(project_path).expanduser().resolve()
    return os.path.normcase(str(resolved))


def _require_note(note: str) -> str:
    cleaned = (note or "").strip()
    if not cleaned:
        raise IntakePolicyError(
            "A non-empty audit note is mandatory for skill enablement/"
            "disable; it is recorded verbatim in the audit event."
        )
    return cleaned


def _require_project_dir(project_path: Path | str) -> Path:
    proj = Path(project_path).expanduser()
    if not proj.is_dir():
        raise IntakeStateError(
            f"Project path {proj} does not exist or is not a directory."
        )
    return proj


def resolve_installed_skill_path(
    tool_id: str,
    revision_sha: str,
    install_path: str | None,
    install_root: Path,
) -> Path:
    """
    Return the verified install directory for one installed revision, or
    raise. Enforces: install path recorded, present on disk, not a
    symlink, exactly the canonical ``<root>/<tool_id>/<revision_sha>``
    layout, and — after full resolution — strictly under the resolved
    install root (kills traversal and out-of-root placements).
    """
    if not install_path:
        raise IntakeStateError(
            f"Tool {tool_id!r} has no recorded install path."
        )
    root = install_root.resolve()
    actual = Path(install_path)
    if actual.is_symlink():
        raise IntakeIntegrityError(
            f"Install path for {tool_id!r} is a symlink; refusing."
        )
    expected = (root / tool_id / revision_sha).resolve()
    try:
        actual_resolved = actual.resolve()
    except OSError as exc:
        raise IntakeStateError(
            f"Cannot resolve install path for {tool_id!r}: {exc}"
        ) from exc
    if root not in actual_resolved.parents:
        raise IntakeIntegrityError(
            f"Install path {install_path!r} for {tool_id!r} resolves "
            f"outside the install root {root}; refusing."
        )
    if actual_resolved != expected:
        raise IntakeIntegrityError(
            f"Install path {install_path!r} for {tool_id!r} does not match "
            f"the canonical revision layout {expected}; refusing."
        )
    if not actual_resolved.is_dir():
        raise IntakeStateError(
            f"Install directory for {tool_id!r} is missing: {actual_resolved}"
        )
    return actual_resolved


def validate_enableable(
    tool_id: str,
    revision_sha: str,
    *,
    db_path: Path | None = None,
    install_root: Path | None = None,
) -> tuple[ToolRecord, ToolRevision]:
    """
    Fail-closed precondition check for enabling a skill binding.

    Returns ``(tool, revision)`` when every invariant holds; raises a
    typed intake error otherwise. Also used by the provider's runtime
    re-validation so enable-time and runtime checks can never diverge.
    """
    from factory.config import get_settings

    root = Path(install_root or get_settings().intake.install_root)

    tool = registry.get_tool(tool_id, db_path)
    if tool is None:
        raise IntakeStateError(f"Unknown tool id: {tool_id!r}.")
    status = _sval(tool.status)
    if status == IntakeStatus.REVOKED.value:
        raise IntakeStateError(
            f"Tool {tool_id!r} is REVOKED (terminal kill-switch); it can "
            "never be enabled for any project."
        )
    if status != IntakeStatus.INSTALLED.value:
        raise IntakeStateError(
            f"Skill enablement requires an INSTALLED tool; {tool_id!r} is "
            f"{status!r}. Run fetch/scan/check/approve/install first."
        )

    rev = registry.get_revision(tool_id, revision_sha, db_path)
    if rev is None:
        raise IntakeStateError(
            f"Revision {revision_sha!r} of {tool_id!r} is not in the "
            "registry (wrong or missing revision)."
        )
    if rev.installed_at is None:
        raise IntakeStateError(
            f"Revision {revision_sha!r} of {tool_id!r} has no recorded "
            "installation."
        )
    if _sval(rev.scan_state) != ScanState.CLEAN.value:
        raise IntakePolicyError(
            "Skill enablement requires a completed CLEAN scan; current "
            f"scan state is {_sval(rev.scan_state)!r}."
        )
    if not rev.approval_bound:
        raise IntakePolicyError(
            "Skill enablement requires a hash-bound human approval; none "
            f"is recorded for revision {revision_sha!r}."
        )

    install_dir = resolve_installed_skill_path(
        tool_id, revision_sha, tool.install_path, root
    )
    if rev.tree_sha256 and hash_tree(install_dir) != rev.tree_sha256:
        raise IntakeIntegrityError(
            f"Installed content of {tool_id!r} no longer matches the "
            "recorded tree hash (tampered or drifted); refusing to enable."
        )

    skill_md = install_dir / "SKILL.md"
    if skill_md.is_symlink():
        raise IntakeIntegrityError(
            f"SKILL.md for {tool_id!r} is a symlink; refusing."
        )
    if not skill_md.is_file():
        raise IntakeStateError(
            f"Installed skill {tool_id!r} has no SKILL.md at {skill_md}."
        )
    return tool, rev


def enable_skill(
    tool_id: str,
    revision_sha: str,
    project_path: Path | str,
    *,
    note: str,
    actor: str,
    db_path: Path | None = None,
    install_root: Path | None = None,
    audit_logger: Any = None,
) -> SkillBinding:
    """
    Explicitly enable an installed skill revision for ONE project.

    Idempotent per (project, tool): re-enabling updates the pinned
    revision and note. The audit payload contains project key,
    tool/revision, actor and note — never skill content.
    """
    cleaned_note = _require_note(note)
    proj = _require_project_dir(project_path)
    key = project_key_for(proj)
    tool, rev = validate_enableable(
        tool_id, revision_sha, db_path=db_path, install_root=install_root
    )
    cleaned_actor = (actor or "").strip() or "unknown"

    now = _now()
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT binding_id, created_at FROM skill_bindings "
            "WHERE project_key = ? AND tool_id = ?",
            (key, tool.tool_id),
        ).fetchone()
        if row is not None:
            binding_id = row["binding_id"]
            created_at = row["created_at"]
            conn.execute(
                """
                UPDATE skill_bindings
                SET revision_sha = ?, note = ?, actor = ?, enabled = 1,
                    updated_at = ?
                WHERE binding_id = ?
                """,
                (rev.revision_sha, cleaned_note, cleaned_actor,
                 now.isoformat(), binding_id),
            )
        else:
            binding_id = str(uuid.uuid4())
            created_at = now.isoformat()
            conn.execute(
                """
                INSERT INTO skill_bindings (
                    binding_id, project_key, tool_id, revision_sha, note,
                    actor, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (binding_id, key, tool.tool_id, rev.revision_sha,
                 cleaned_note, cleaned_actor, created_at, now.isoformat()),
            )
        conn.commit()

    _emit(audit_logger, "skill.binding.enabled", {
        "project_key": key,
        "tool_id": tool.tool_id,
        "revision_sha": rev.revision_sha,
        "actor": cleaned_actor,
        "note": cleaned_note,
    })
    logger.info(
        "Skill %s@%s enabled for project %s by %s",
        tool.tool_id, rev.revision_sha, key, cleaned_actor,
    )
    return SkillBinding(
        binding_id=binding_id,
        project_key=key,
        tool_id=tool.tool_id,
        revision_sha=rev.revision_sha,
        note=cleaned_note,
        actor=cleaned_actor,
        enabled=True,
        created_at=datetime.fromisoformat(created_at),
        updated_at=now,
    )


def disable_skill(
    tool_id: str,
    project_path: Path | str,
    *,
    note: str,
    actor: str,
    db_path: Path | None = None,
    audit_logger: Any = None,
) -> SkillBinding:
    """
    Remove the skill binding for ONE project only.

    Never touches ``tool_registry`` / ``tool_revisions`` — the global
    installation stays INSTALLED and intact. The audit payload contains
    project key, tool/revision, actor and reason — never skill content.
    """
    cleaned_note = _require_note(note)
    proj = _require_project_dir(project_path)
    key = project_key_for(proj)
    cleaned_actor = (actor or "").strip() or "unknown"

    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT binding_id, revision_sha, created_at FROM skill_bindings "
            "WHERE project_key = ? AND tool_id = ? AND enabled = 1",
            (key, tool_id),
        ).fetchone()
        if row is None:
            raise IntakeStateError(
                f"No enabled skill binding for {tool_id!r} in project "
                f"{key!r}."
            )
        conn.execute(
            "DELETE FROM skill_bindings WHERE binding_id = ?",
            (row["binding_id"],),
        )
        conn.commit()

    _emit(audit_logger, "skill.binding.disabled", {
        "project_key": key,
        "tool_id": tool_id,
        "revision_sha": row["revision_sha"],
        "actor": cleaned_actor,
        "reason": cleaned_note,
    })
    logger.info(
        "Skill %s disabled for project %s by %s", tool_id, key, cleaned_actor
    )
    return SkillBinding(
        binding_id=row["binding_id"],
        project_key=key,
        tool_id=tool_id,
        revision_sha=row["revision_sha"],
        note=cleaned_note,
        actor=cleaned_actor,
        enabled=False,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=_now(),
    )


def list_bindings(
    project_key: str | None = None,
    *,
    enabled_only: bool = True,
    db_path: Path | None = None,
) -> list[SkillBinding]:
    """List skill bindings (optionally scoped to one project)."""
    sql = "SELECT * FROM skill_bindings"
    clauses: list[str] = []
    params: list[Any] = []
    if project_key is not None:
        clauses.append("project_key = ?")
        params.append(project_key)
    if enabled_only:
        clauses.append("enabled = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY project_key, tool_id"
    with get_connection(db_path) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        SkillBinding(
            binding_id=r["binding_id"],
            project_key=r["project_key"],
            tool_id=r["tool_id"],
            revision_sha=r["revision_sha"],
            note=r["note"],
            actor=r["actor"],
            enabled=bool(r["enabled"]),
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
        )
        for r in rows
    ]


def list_installed_skills(
    db_path: Path | None = None,
) -> list[ToolRecord]:
    """All globally installed agent-skill tools (inspection helper)."""
    tools = registry.list_tools(db_path)
    return [
        t for t in tools
        if _sval(t.kind) == ToolKind.AGENT_SKILL.value
        and _sval(t.status) == IntakeStatus.INSTALLED.value
    ]


def _emit(audit_logger: Any, event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort audit emit; never breaks the operation."""
    if audit_logger is None:
        return
    try:
        audit_logger.emit(event_type, payload)
    except Exception as exc:  # pragma: no cover — audit must not block
        logger.warning("Skill binding audit emit failed: %s", exc)
