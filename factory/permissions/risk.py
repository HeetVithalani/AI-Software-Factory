"""
factory.permissions.risk
=========================
Deterministic risk engine — classifies actions before execution.

The risk engine uses explicit rule tables, NOT an LLM. Every action
category maps to a RiskLevel through deterministic rules.

Risk levels:
  SAFE     — safe development commands, read-only ops
  LOW      — normal file writes within project scope
  MEDIUM   — shell commands with side effects, unknown commands
  HIGH     — git mutations, network access, external service calls
  CRITICAL — sensitive data access, production-adjacent actions,
             destructive operations, force flags
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.models import RiskLevel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rule tables
# ---------------------------------------------------------------------------

# Git subcommands classified by risk
_GIT_READ_ONLY: frozenset[str] = frozenset({
    "status", "diff", "log", "show", "branch", "tag",
    "remote", "fetch", "--version", "rev-parse", "ls-files",
    "shortlog", "describe",
})

_GIT_HIGH_RISK: frozenset[str] = frozenset({
    "commit", "push", "pull", "merge", "rebase", "cherry-pick",
    "reset", "branch -d", "branch -D", "tag -d", "gc",
    "stash", "apply",
})

_GIT_CRITICAL: frozenset[str] = frozenset({
    "push --force", "push -f", "reset --hard",
    "clean -f", "clean -fd", "filter-branch",
})

# Shell command prefixes that are known dangerous
_SHELL_CRITICAL_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+-rf?\b"),
    re.compile(r"\bformat\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r">\s*/dev/"),
    re.compile(r">\s*(?:[A-Za-z]:\\|/|\\|\\.\\.)"),  # Redirect to absolute path or parent dir
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bfdisk\b"),
    re.compile(r"curl\s.*\|\s*(?:bash|sh|python)"),  # curl-pipe-execute
    re.compile(r"wget\s.*-O\s*-.*\|\s*(?:bash|sh|python)"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r"\bpasswd\b"),
]

_SHELL_HIGH_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bnpm\s+publish\b"),
    re.compile(r"\bpypi\b"),
    re.compile(r"\btwine\s+upload\b"),
    re.compile(r"\bheroku\b"),
    re.compile(r"\bgcloud\s+deploy\b"),
    re.compile(r"\baws\s+s3\s+cp\b"),
    re.compile(r"\bssh\b"),
    re.compile(r"\bscp\b"),
    re.compile(r"\bcurl\b"),
    re.compile(r"\bwget\b"),
]

# File operations that are safe within project scope
_SAFE_FILE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml",
    ".md", ".txt", ".toml", ".cfg", ".ini", ".rst",
})


@dataclass
class ActionContext:
    """
    Describes an action for risk classification.

    All fields are optional but more context → more accurate risk.
    """
    # Action category: "git", "shell", "file.read", "file.write",
    #                  "network", "process", "approval", "knowledge"
    category: str = ""
    # Specific command/operation string (e.g. "git commit -m …")
    command: str = ""
    # Target path (if applicable)
    target_path: Path | None = None
    # Whether the target is known to be a sensitive file
    target_is_sensitive: bool = False
    # Additional context
    metadata: dict[str, Any] = field(default_factory=dict)


class RiskEngine:
    """
    Classifies the risk level of an action deterministically.

    Usage::

        engine = RiskEngine()
        ctx = ActionContext(category="git", command="git commit -m 'fix'")
        level = engine.classify(ctx)
        # → RiskLevel.HIGH
    """

    def classify(self, ctx: ActionContext) -> RiskLevel:
        """Return the RiskLevel for the given ActionContext."""
        # Sensitive target → always at least HIGH, possibly CRITICAL
        if ctx.target_is_sensitive:
            return RiskLevel.CRITICAL

        category = ctx.category.lower()
        command = ctx.command.strip()

        if category == "git" or command.startswith("git "):
            return self._classify_git(command)

        if category == "shell" or category == "process":
            return self._classify_shell(command)

        if category in ("file.write", "file.create"):
            return self._classify_file_write(ctx.target_path)

        if category == "file.read":
            # Read-only — check for sensitive path
            if ctx.target_path and self._path_is_sensitive(ctx.target_path):
                return RiskLevel.CRITICAL
            return RiskLevel.SAFE

        if category == "file.delete":
            return RiskLevel.HIGH

        if category == "network":
            return RiskLevel.HIGH

        if category == "knowledge":
            impact = ctx.metadata.get("impact", "low")
            if impact == "high":
                return RiskLevel.HIGH
            return RiskLevel.LOW

        if category == "approval":
            return RiskLevel.SAFE

        # Unknown category — treat as MEDIUM
        logger.debug("RiskEngine: unknown category %r — defaulting to MEDIUM.", category)
        return RiskLevel.MEDIUM

    # ------------------------------------------------------------------
    # Private classifiers
    # ------------------------------------------------------------------

    def _classify_git(self, command: str) -> RiskLevel:
        """Classify a git command by its subcommand."""
        # Strip 'git ' prefix if present
        cmd = re.sub(r"^git\s+", "", command).strip()

        # Critical: force operations
        for pattern in _GIT_CRITICAL:
            if cmd.startswith(pattern) or pattern in cmd:
                logger.debug("Git command classified CRITICAL: %r", command)
                return RiskLevel.CRITICAL

        # High: write operations
        first_word = cmd.split()[0] if cmd.split() else ""
        if first_word in _GIT_HIGH_RISK:
            logger.debug("Git command classified HIGH: %r", command)
            return RiskLevel.HIGH

        # Safe: read-only
        if first_word in _GIT_READ_ONLY:
            return RiskLevel.SAFE

        # Unknown git subcommand — treat as HIGH (conservative)
        logger.debug(
            "Unknown git subcommand %r — defaulting to HIGH.", first_word
        )
        return RiskLevel.HIGH

    def _classify_shell(self, command: str) -> RiskLevel:
        """Classify a shell command by pattern matching."""
        # Critical patterns
        for pattern in _SHELL_CRITICAL_PATTERNS:
            if pattern.search(command):
                logger.debug(
                    "Shell command classified CRITICAL: %r (pattern: %s)",
                    command, pattern.pattern,
                )
                return RiskLevel.CRITICAL

        # High-risk patterns
        for pattern in _SHELL_HIGH_PATTERNS:
            if pattern.search(command):
                logger.debug(
                    "Shell command classified HIGH: %r (pattern: %s)",
                    command, pattern.pattern,
                )
                return RiskLevel.HIGH

        # Safe development commands
        safe_prefixes = (
            "python ", "python3 ", "pytest ", "pip ", "npm ", "npx ",
            "node ", "echo ", "cat ", "ls ", "dir ", "type ",
            "ruff ", "black ", "mypy ", "flake8 ",
        )
        if any(command.lstrip().startswith(p) for p in safe_prefixes):
            return RiskLevel.SAFE

        # Default unknown shell: MEDIUM
        return RiskLevel.MEDIUM

    def _classify_file_write(self, path: Path | None) -> RiskLevel:
        """Classify a file write operation."""
        if path is None:
            return RiskLevel.MEDIUM
        if self._path_is_sensitive(path):
            return RiskLevel.CRITICAL
        if path.suffix.lower() in _SAFE_FILE_EXTENSIONS:
            return RiskLevel.LOW
        return RiskLevel.MEDIUM

    def _path_is_sensitive(self, path: Path) -> bool:
        from factory.permissions.sensitive_data import SensitiveDataDetector
        return SensitiveDataDetector().is_sensitive_path(path)

    def requires_approval(
        self, level: RiskLevel, policies: Any = None
    ) -> bool:
        """
        Return True if this risk level requires human approval given policies.

        With default policies: HIGH and CRITICAL always require approval.
        """
        if policies is None:
            from factory.permissions.policies import PolicySet
            policies = PolicySet()

        if level == RiskLevel.CRITICAL:
            return True
        if level == RiskLevel.HIGH and policies.require_approval_high_risk:
            return True
        return False
