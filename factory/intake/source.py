"""
factory.intake.source
======================
Canonical source identity and revision pinning for the tool intake (Phase 2a).

Strict validation rules (fail-closed):
* Only public HTTPS GitHub repository URLs in canonical form:
  ``https://github.com/<owner>/<repo>`` — nothing else. No credentials,
  ports, query strings, fragments, sub-paths, or non-ASCII components.
* The caller must supply an exact 40-character commit SHA. Branches, tags,
  and moving refs are never accepted or silently resolved.
* The download URL is *derived* from validated components only — user input
  is never concatenated into an executable command.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from factory.errors import IntakeError

# GitHub owner: alphanumeric or hyphen, no leading/trailing hyphen, <= 39 chars.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
# GitHub repo: alphanumeric, dot, underscore, hyphen; <= 100 chars.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# Full 40-character lowercase hex commit SHA.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Restricted archive endpoint — never arbitrary URLs.
ARCHIVE_HOST = "codeload.github.com"


class InvalidSourceError(IntakeError):
    """Raised when a source URL or revision SHA is not canonical/valid."""
    code = "INTAKE_INVALID_SOURCE"


@dataclass(frozen=True)
class GitHubSource:
    """Validated, immutable identity of a pinned GitHub source."""
    owner: str
    repo: str
    canonical_url: str
    revision_sha: str

    @property
    def archive_url(self) -> str:
        """
        Derived download URL for the pinned revision tarball.

        Components were validated at construction time; this is a restricted
        GitHub archive endpoint, never a caller-supplied URL.
        """
        return f"https://{ARCHIVE_HOST}/{self.owner}/{self.repo}/tar.gz/{self.revision_sha}"


def validate_revision_sha(revision: str) -> str:
    """Return the normalized (lowercase) SHA, or raise InvalidSourceError."""
    if not isinstance(revision, str):
        raise InvalidSourceError("Revision must be a string commit SHA.")
    sha = revision.strip().lower()
    if not _SHA_RE.match(sha):
        raise InvalidSourceError(
            "Revision must be an exact 40-character commit SHA "
            "(hex). Branches, tags, and short SHAs are not accepted: "
            f"got {revision!r}."
        )
    return sha


def parse_github_source(url: str, revision: str) -> GitHubSource:
    """
    Validate a canonical public GitHub repository URL and pinned revision.

    Accepts exactly ``https://github.com/<owner>/<repo>`` (optional trailing
    slash; optional ``.git`` suffix on the repo name). Everything else —
    http, other hosts, embedded credentials, ports, queries, fragments,
    sub-paths, whitespace, non-ASCII — is rejected.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidSourceError("Source URL is required.")

    # Whitespace anywhere (including leading/trailing) is rejected before
    # any normalization — fail closed on sloppy input.
    if any(ch.isspace() for ch in url):
        raise InvalidSourceError("Source URL must not contain whitespace.")

    raw = url.strip()
    if "\\" in raw:
        raise InvalidSourceError("Source URL must use forward slashes only.")

    # Reject credentials, ports, queries, fragments before any parsing.
    if raw.count(":") != 1:
        raise InvalidSourceError(
            "Source URL must be plain https with no port or credentials."
        )
    for marker in ("@", "?", "#"):
        if marker in raw:
            raise InvalidSourceError(
                f"Source URL must not contain '{marker}' "
                "(no credentials, queries, or fragments)."
            )

    if not raw.lower().startswith("https://"):
        raise InvalidSourceError("Source URL must use the https scheme.")

    remainder = raw[len("https://"):].rstrip("/")
    parts = remainder.split("/")
    if len(parts) != 3:
        raise InvalidSourceError(
            "Source URL must be exactly https://github.com/<owner>/<repo> "
            "(no sub-paths)."
        )
    host, owner, repo = parts

    if host.lower() != "github.com":
        raise InvalidSourceError(
            f"Only github.com repositories are supported, got host {host!r}."
        )
    if not owner or not repo or not all(ord(ch) < 128 for ch in remainder):
        raise InvalidSourceError("Owner and repository must be non-empty ASCII.")

    if repo.lower().endswith(".git"):
        repo = repo[: -len(".git")]
    if not repo or ".." in repo or repo.startswith("."):
        raise InvalidSourceError(f"Invalid repository name: {repo!r}.")

    if not _OWNER_RE.match(owner):
        raise InvalidSourceError(f"Invalid GitHub owner: {owner!r}.")
    if not _REPO_RE.match(repo) or repo.endswith("."):
        raise InvalidSourceError(f"Invalid GitHub repository name: {repo!r}.")

    sha = validate_revision_sha(revision)
    canonical = f"https://github.com/{owner}/{repo}"
    return GitHubSource(owner=owner, repo=repo, canonical_url=canonical, revision_sha=sha)


def derive_tool_id(kind: str, canonical_url: str) -> str:
    """
    Stable logical identity for a tool: sha256(kind + canonical URL)[:16].

    The revision SHA is deliberately NOT part of the tool identity —
    revisions of the same source share one logical tool.

    Identity normalization: GitHub treats host/owner/repository casing as
    insignificant, so equivalent URLs differing only in case
    (``.../OpenAI/Skills`` vs ``.../openai/skills``) map to ONE identity
    instead of creating duplicate logical tools. Only the identity input is
    case-folded — the stored canonical URL and derived archive URL keep
    their original casing.
    """
    key = f"{kind}".lower() + ":" + canonical_url.lower()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return digest[:16]
