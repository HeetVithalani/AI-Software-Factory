"""
factory.llm.structured_output
==============================
Deterministic JSON extraction and Pydantic schema validation.

Design principles
-----------------
* The factory NEVER trusts a model to produce valid JSON unprompted.
* This module extracts JSON from any text (handles markdown fences, prose).
* Validation is always performed against the known Pydantic schema.
* Invalid output is REJECTED — schemas are never weakened to accept bad output.
* Parse failures are recorded as structured audit data so they appear in
  the evidence trail.

Usage (in agents)::

    from factory.llm.structured_output import (
        extract_json_from_text,
        parse_structured_response,
        StructuredOutputError,
    )

    try:
        proposal = parse_structured_response(response, BuilderProposal)
    except StructuredOutputError as exc:
        # Log, audit, retry via existing retry mechanism
        ...
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar, Type

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    """Raised when structured output cannot be extracted or validated."""

    def __init__(self, message: str, raw_content: str = "") -> None:
        super().__init__(message)
        self.raw_content = raw_content


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json_from_text(text: str) -> str | None:
    """
    Find and return the first valid JSON object or array from ``text``.

    Handles:
    - Raw JSON response (entire text is JSON)
    - Markdown code fences (```json ... ``` or ``` ... ```)
    - JSON embedded in prose
    - Trailing comma before closing brace (common model error — fixed)

    Returns the raw JSON string if found, or None if no valid JSON exists.
    Does NOT raise. Invalid JSON candidates are tried in order.
    """
    if not text:
        return None

    text = text.strip()

    # 1. Try the whole string first (cheapest — works if model output is clean)
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # 2. Extract from markdown code fences
    fence_patterns = [
        r"```(?:json)?\s*\n([\s\S]*?)\n```",   # ```json ... ```
        r"```(?:json)?\s*([\s\S]*?)```",         # inline ```json...```
        r"`([\s\S]*?)`",                          # single backtick
    ]
    for pattern in fence_patterns:
        for match in re.finditer(pattern, text):
            candidate = match.group(1).strip()
            result = _try_parse_json(candidate)
            if result is not None:
                return result

    # 3. Find the outermost {} or [] and extract
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        result = _extract_balanced(text, start_char, end_char)
        if result is not None:
            return result

    logger.debug(
        "extract_json_from_text: no valid JSON found in text of length %d",
        len(text),
    )
    return None


def _try_parse_json(text: str) -> str | None:
    """Try to parse text as JSON; return the text if valid, None otherwise."""
    text = text.strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    # Try removing trailing commas before } or ]
    fixed = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        json.loads(fixed)
        return fixed
    except json.JSONDecodeError:
        return None


def _extract_balanced(text: str, open_char: str, close_char: str) -> str | None:
    """
    Find the outermost balanced open_char/close_char block in text.
    Returns the first valid JSON block found, or None.
    """
    start_idx = text.find(open_char)
    if start_idx == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text[start_idx:], start=start_idx):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                candidate = text[start_idx:i + 1]
                result = _try_parse_json(candidate)
                if result is not None:
                    return result
                # Try again from next occurrence
                rest = text[i + 1:]
                return _extract_balanced(rest, open_char, close_char)
    return None


# ---------------------------------------------------------------------------
# Pydantic validation
# ---------------------------------------------------------------------------

def parse_structured_response(
    content: str,
    model_class: Type[T],
    *,
    context: str = "",
) -> T:
    """
    Extract JSON from ``content`` and validate it against ``model_class``.

    Parameters
    ----------
    content:
        Raw text from LLM (may contain prose, fences, etc.).
    model_class:
        Pydantic model class to validate against.
    context:
        Optional description for error messages (e.g. "BuilderAgent turn 3").

    Returns
    -------
    Validated Pydantic model instance.

    Raises
    ------
    StructuredOutputError
        If no JSON is found or Pydantic validation fails.
        Callers must catch this and route through the existing retry mechanism.
        This exception is NEVER silently swallowed.
    """
    json_str = extract_json_from_text(content)
    if json_str is None:
        raise StructuredOutputError(
            f"No valid JSON found in LLM output"
            + (f" ({context})" if context else ""),
            raw_content=content,
        )

    try:
        data: Any = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            f"JSON parse error: {exc}"
            + (f" ({context})" if context else ""),
            raw_content=content,
        ) from exc

    try:
        return model_class.model_validate(data)
    except ValidationError as exc:
        # Report first few errors for debuggability
        errors = exc.errors()[:5]
        raise StructuredOutputError(
            f"Schema validation failed for {model_class.__name__}: "
            f"{errors}"
            + (f" ({context})" if context else ""),
            raw_content=content,
        ) from exc


def parse_structured_data(
    structured_data: dict[str, Any],
    model_class: Type[T],
    *,
    context: str = "",
) -> T:
    """
    Validate a pre-parsed dict (from response.structured_data) against
    ``model_class``.

    Use this when the provider has already done JSON parsing.
    Falls through to ``parse_structured_response`` if dict is empty.
    """
    if not structured_data:
        raise StructuredOutputError(
            f"structured_data is empty — cannot validate {model_class.__name__}"
            + (f" ({context})" if context else ""),
        )
    try:
        return model_class.model_validate(structured_data)
    except ValidationError as exc:
        errors = exc.errors()[:5]
        raise StructuredOutputError(
            f"Schema validation failed for {model_class.__name__}: "
            f"{errors}"
            + (f" ({context})" if context else ""),
        ) from exc
