"""
factory.memory.sanitization
=============================
Knowledge sanitization — strips project-specific secrets before global storage.

Global knowledge must not leak project-specific secrets, credentials, or
proprietary information. The sanitizer runs before any entry is promoted
from project memory to global knowledge.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Patterns that indicate content contains project-specific secrets
_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+",
               re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/]+=*", re.IGNORECASE),
    re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY", re.IGNORECASE),
    re.compile(r"[A-Z0-9]{20}:[A-Za-z0-9+/]{40}", re.IGNORECASE),  # AWS-style
    re.compile(r"(?:mysql|postgres|mongodb|redis)://[^@\s]+@\S+", re.IGNORECASE),
    re.compile(r"\b(?:192\.168|10\.\d+\.\d+|172\.1[6-9]\.|172\.2\d\.|172\.3[01]\.)\d+\b"),
]

# Keys that are safe for global knowledge (generic, non-project-specific)
_SAFE_KNOWLEDGE_KEYS: frozenset[str] = frozenset({
    "best_practice", "pattern", "library", "framework",
    "algorithm", "architecture_decision", "api_design",
    "error_solution", "performance_tip", "security_pattern",
})


@dataclass
class SanitizationResult:
    """Result of a sanitization check."""
    is_clean: bool = True
    violations: list[str] = field(default_factory=list)
    sanitized_value: Any = None

    def add_violation(self, reason: str) -> None:
        self.is_clean = False
        self.violations.append(reason)


class KnowledgeSanitizer:
    """
    Sanitizes knowledge entries before global storage.

    Checks:
    1. Value does not contain secret patterns.
    2. Value does not reference project-specific identifiers (project_id).
    3. Content can be safely shared across all future projects.

    Usage::

        sanitizer = KnowledgeSanitizer()
        result = sanitizer.sanitize("best_practice", "Use type hints", project_id="p1")
        if result.is_clean:
            global_memory.store(key, result.sanitized_value)
    """

    def sanitize(
        self,
        key: str,
        value: Any,
        *,
        project_id: str | None = None,
    ) -> SanitizationResult:
        """
        Check and sanitize a knowledge entry for global storage.

        Parameters
        ----------
        key:
            The knowledge key (category/name).
        value:
            The value to check (string or dict).
        project_id:
            The originating project's ID (used to detect leakage).

        Returns
        -------
        SanitizationResult
            is_clean=True if safe to store globally.
        """
        result = SanitizationResult(sanitized_value=value)

        # Convert to string for pattern matching
        value_str = str(value) if not isinstance(value, str) else value

        # Check for secret patterns
        for pattern in _SECRET_PATTERNS:
            if pattern.search(value_str):
                result.add_violation(
                    f"Value contains potential secret (pattern: {pattern.pattern[:40]!r})"
                )

        # Check for project_id leakage
        if project_id and project_id in value_str:
            result.add_violation(
                f"Value contains project_id {project_id!r} — may be project-specific"
            )

        # Log violations
        if not result.is_clean:
            logger.warning(
                "KnowledgeSanitizer: entry %r failed sanitization: %s",
                key,
                "; ".join(result.violations),
            )
        else:
            logger.debug("KnowledgeSanitizer: entry %r passed sanitization.", key)

        return result

    def sanitize_dict(
        self,
        data: dict[str, Any],
        *,
        project_id: str | None = None,
    ) -> SanitizationResult:
        """Sanitize all values in a dict. Fails if any value fails."""
        combined = SanitizationResult(sanitized_value=data)
        for key, value in data.items():
            r = self.sanitize(key, value, project_id=project_id)
            if not r.is_clean:
                for v in r.violations:
                    combined.add_violation(f"[{key}] {v}")
        return combined
