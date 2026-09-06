"""
factory.memory.validation
==========================
Validation helpers for knowledge entries and memory operations.

Ensures entries meet quality and integrity thresholds before being
accepted into project or global memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationResult:
    """Result of a memory entry validation check."""
    valid: bool = True
    errors: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.valid = False
        self.errors.append(reason)


def validate_knowledge_entry(
    category: str,
    key: str,
    value: Any,
    *,
    min_confidence: float = 0.0,
    confidence: float = 0.0,
) -> ValidationResult:
    """
    Validate a knowledge entry before global storage.

    Checks:
    - category and key are non-empty strings
    - key does not contain path-traversal or injection characters
    - value is not empty
    - confidence meets the minimum threshold (if specified)
    """
    result = ValidationResult()

    if not isinstance(category, str) or not category.strip():
        result.fail("category must be a non-empty string.")

    if not isinstance(key, str) or not key.strip():
        result.fail("key must be a non-empty string.")
    elif re.search(r"[/\\<>\"'`]", key):
        result.fail(f"key {key!r} contains disallowed characters.")

    if value is None or (isinstance(value, str) and not value.strip()):
        result.fail("value must not be empty.")

    if min_confidence > 0 and confidence < min_confidence:
        result.fail(
            f"confidence {confidence:.2f} is below minimum {min_confidence:.2f}."
        )

    return result
