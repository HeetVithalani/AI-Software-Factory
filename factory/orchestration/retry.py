"""
factory.orchestration.retry
=============================
Deterministic failure classification for bounded task retries (COR-004).

Classification is a closed, explicit table — never an LLM verdict:

1. ``AgentResult.failure_kind`` (structured field), when set to a known
   value, wins. Producers that know the real cause set it: tools and
   agents emit ``awaiting_approval`` for paused-on-human work.
2. Otherwise the error text is matched against known signatures:

   * TRANSIENT — safe to retry inside the declared budget: timeouts,
     connection resets/aborts, rate limits (429), gateway errors
     (502/503/504), "service unavailable", "temporarily ...".
   * PERMANENT — deterministic defects where retrying cannot help
     (no executor registered, agent budget exhausted, policy denial).
   * AWAITING_APPROVAL — a pending human decision embedded in an error
     string (fallback for producers that did not set the structured
     field): pause, never retry, never fail.

3. Anything else is UNKNOWN and is treated as **non-retryable** —
   fail-closed by design: retrying failures we do not understand can
   mask deterministic defects behind flapping state. Only explicitly
   known-transient failures earn a retry.

"Connection refused" is deliberately NOT transient: in a local-first
Ollama setup it means the service is not running, which a retry cannot
fix and retrying only delays the human signal.
"""

from __future__ import annotations

import re
from enum import Enum

from factory.models import AgentResult

__all__ = [
    "FailureKind",
    "classify_failure",
    "extract_approval_request_id",
]


class FailureKind(str, Enum):
    """Deterministic failure classification values."""

    AWAITING_APPROVAL = "awaiting_approval"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"
    #: A deterministic verification failure (e.g. failing tests) where the
    #: implementation — not the infrastructure — is at fault. The
    #: orchestrator may schedule ONE bounded repair cycle if the approved
    #: plan allows it; otherwise the failure is terminal as usual.
    REPAIRABLE = "repairable"


#: Structured values accepted from ``AgentResult.failure_kind``.
_KNOWN_KINDS = frozenset(kind.value for kind in FailureKind)

#: Known-transient signatures (case-insensitive).
_TRANSIENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"timed?\s*out|\btimeout\b", re.IGNORECASE),
    re.compile(
        r"connection (?:reset|aborted|closed)|broken pipe|econnreset",
        re.IGNORECASE,
    ),
    re.compile(
        r"rate[ _-]?limit|too many requests|\b429\b", re.IGNORECASE
    ),
    re.compile(
        r"\b50[234]\b|bad gateway|service unavailable|gateway timeout",
        re.IGNORECASE,
    ),
    re.compile(
        r"temporar(?:ily|y) (?:unavailable|disabled|failed)", re.IGNORECASE
    ),
    re.compile(r"network (?:error|unreachable|trouble)", re.IGNORECASE),
)

#: Known-permanent signatures (retrying cannot change the outcome).
_PERMANENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"no executor registered", re.IGNORECASE),
    re.compile(r"exceeded max (?:turns|steps)", re.IGNORECASE),
    re.compile(r"denied by policy", re.IGNORECASE),
    re.compile(r"outside the workspace sandbox", re.IGNORECASE),
    re.compile(r"may not start with '-'", re.IGNORECASE),
    re.compile(r"repository root", re.IGNORECASE),
)

#: Pending-human-approval signature in free-text errors (fallback path).
_APPROVAL_PATTERN = re.compile(
    r"requires (?:human )?(?:approval|review)", re.IGNORECASE
)

#: Approval request id embedded in error strings: request_id=<uuid>.
_REQUEST_ID_PATTERN = re.compile(
    r"request_id=([0-9a-fA-F][0-9a-fA-F-]{7,63})"
)


def extract_approval_request_id(text: str) -> str:
    """Return the approval request id embedded in ``text`` ("" if none)."""
    match = _REQUEST_ID_PATTERN.search(text or "")
    return match.group(1) if match else ""


def classify_failure(result: AgentResult) -> FailureKind:
    """
    Classify a FAILED ``AgentResult`` deterministically.

    Returns a :class:`FailureKind`. Callers must only pass failed
    results; a successful result classifies as UNKNOWN (no retry
    semantics apply — the caller should not have asked).

    Pure function: same input → same classification, always.
    """
    kind = (result.failure_kind or "").strip().lower()
    if kind in _KNOWN_KINDS:
        return FailureKind(kind)

    text = "\n".join(result.errors) if result.errors else (result.summary or "")

    # Approval gate first: paused work must never be retried or failed,
    # even if the message happens to contain a transient signature.
    if _APPROVAL_PATTERN.search(text):
        return FailureKind.AWAITING_APPROVAL

    for pattern in _TRANSIENT_PATTERNS:
        if pattern.search(text):
            return FailureKind.TRANSIENT

    for pattern in _PERMANENT_PATTERNS:
        if pattern.search(text):
            return FailureKind.PERMANENT

    return FailureKind.UNKNOWN
