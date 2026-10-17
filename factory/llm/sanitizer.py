"""
factory.llm.sanitizer
=======================
Deterministic request/response content sanitizer (SEC-008).

Prevents high-confidence credentials from leaving the process inside
LLM requests, and from being persisted verbatim in the audit trail.

Policy (explicit and deterministic)
-----------------------------------
* **What is scanned**: the request ``system_prompt`` and every message
  ``content`` string; and (defense-in-depth) the persisted
  ``raw_response``/``error`` audit fields via :func:`redact_text`.
* **What is detected**: high-confidence, structured secret formats only
  — private-key blocks, cloud access key IDs, provider API tokens
  (GitHub, OpenAI, Slack, Google, Stripe), JWTs, bearer tokens,
  basic-auth URL credentials, and quoted ``password``/``secret``/``token``
  style assignments with a high-entropy-looking value.
* **What happens**: detected secrets are **replaced in place** with a
  deterministic placeholder ``[REDACTED:<pattern-name>]``. Requests are
  never rejected for a detection — redaction preserves the legitimate
  task context while guaranteeing the raw secret is not transmitted or
  logged. This is a deliberate choice: rejection would let any leaked
  secret DoS the pipeline, while redaction neutralises it.
* **Logging**: only pattern names and counts are ever logged — the
  matched text is never written to logs, errors, or audit records.
* **Non-mutation**: :func:`sanitize_request` returns a NEW ``LLMRequest``;
  the caller's object is untouched.

Known limitations (never claim perfect detection)
-------------------------------------------------
* Detection is pattern-based and deterministic: secrets that do not
  match a known structured format (e.g. an unquoted 12-word passphrase,
  a novel vendor token shape, or a secret split across messages) are
  NOT detected. The filesystem-layer ``SensitiveDataDetector`` remains
  the primary boundary for keeping secrets out of context in the first
  place; this sanitizer is a last-line transmission safeguard.
* Generic assignment detection requires a QUOTED value of at least 16
  characters to keep false positives near zero; unquoted assignments
  like ``token=$HOME/tok`` are intentionally not matched.
* Encoded/encrypted secrets (base64-of-base64, rot13, etc.) are not
  detected.
"""

from __future__ import annotations

import dataclasses
import logging
import re

from factory.llm.provider import LLMRequest

logger = logging.getLogger(__name__)

__all__ = [
    "RedactionResult",
    "redact_text",
    "sanitize_request",
    "SECRET_PATTERNS",
]


# ---------------------------------------------------------------------------
# Detection patterns — (name, compiled regex, replacement template)
# Group 1 (if present) is the secret; otherwise the whole match redacts.
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    # PEM/OPENSSH private key blocks (multi-line, bounded).
    (
        "private-key-block",
        re.compile(
            r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----"
            r".*?-----END (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED:private-key-block]",
    ),
    # AWS access key IDs (AKIA/ASIA + 16 uppercase alnum).
    (
        "aws-access-key-id",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "[REDACTED:aws-access-key-id]",
    ),
    # AWS secret assignment (40-char base64ish value).
    (
        "aws-secret-key",
        re.compile(
            r"(?i)\baws_secret_access_key\s*[:=]\s*['\"]?"
            r"([A-Za-z0-9/+=]{40})['\"]?"
        ),
        "[REDACTED:aws-secret-key]",
    ),
    # GitHub tokens (classic + fine-grained + app/installation).
    (
        "github-token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
        "[REDACTED:github-token]",
    ),
    (
        "github-fine-grained-token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
        "[REDACTED:github-fine-grained-token]",
    ),
    # OpenAI/Anthropic-style secret keys.
    (
        "openai-api-key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "[REDACTED:openai-api-key]",
    ),
    # Slack tokens.
    (
        "slack-token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        "[REDACTED:slack-token]",
    ),
    # Google API keys.
    (
        "google-api-key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "[REDACTED:google-api-key]",
    ),
    # Stripe live/test keys.
    (
        "stripe-key",
        re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"),
        "[REDACTED:stripe-key]",
    ),
    # JWTs (three base64url segments).
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "[REDACTED:jwt]",
    ),
    # HTTP bearer tokens — redact only the token, keep the header name.
    (
        "bearer-token",
        re.compile(r"(\b[Bb]earer\s+)([A-Za-z0-9_.=-]{20,})"),
        r"\1[REDACTED:bearer-token]",
    ),
    # basic-auth URL credentials — redact only the password segment.
    (
        "url-basic-auth",
        re.compile(r"(://[^/\s:@]+:)([^/\s@]{4,})(@)"),
        r"\1[REDACTED:url-basic-auth]\3",
    ),
    # Quoted secret-ish assignments with a substantial value.
    (
        "quoted-credential-assignment",
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|apikey"
            r"|access[_-]?token|client[_-]?secret)\b"
            r"(\s*[:=]\s*)(['\"])([^\s'\"]{16,})\3"
        ),
        r"\1\2\3[REDACTED:quoted-credential-assignment]\3",
    ),
]

# Public tuple for introspection/tests.
SECRET_PATTERNS: tuple[str, ...] = tuple(name for name, _, _ in _SECRET_PATTERNS)


class RedactionResult:
    """Outcome of a redaction pass."""

    __slots__ = ("text", "counts")

    def __init__(self, text: str, counts: dict[str, int]) -> None:
        self.text = text
        self.counts = counts

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def __repr__(self) -> str:  # never includes text — safe to log
        return f"RedactionResult(total={self.total}, counts={dict(self.counts)})"


def redact_text(text: str) -> RedactionResult:
    """
    Replace every detected secret in ``text`` with a placeholder.

    Deterministic and pure. Only pattern NAMES and counts are logged —
    never the matched text.
    """
    if not text:
        return RedactionResult(text, {})

    counts: dict[str, int] = {}
    result = text
    for name, pattern, replacement in _SECRET_PATTERNS:
        result, n = pattern.subn(replacement, result)
        if n:
            counts[name] = counts.get(name, 0) + n

    if counts:
        logger.warning(
            "Sanitizer redacted %d secret(s) from outbound content: %s",
            sum(counts.values()),
            ", ".join(f"{k}x{v}" for k, v in sorted(counts.items())),
        )
    return RedactionResult(result, counts)


def sanitize_request(request: LLMRequest) -> LLMRequest:
    """
    Return a COPY of ``request`` with secrets redacted.

    Scans ``system_prompt`` and every message ``content`` (string
    values only). The original request object is never mutated.
    """
    sys_result = redact_text(request.system_prompt or "")
    new_system = sys_result.text

    new_messages: list[dict[str, str]] = []
    message_counts: dict[str, int] = {}
    for msg in request.messages or []:
        if not isinstance(msg, dict):
            new_messages.append(msg)  # type: ignore[arg-type]
            continue
        content = msg.get("content")
        if isinstance(content, str):
            r = redact_text(content)
            if r.counts:
                for k, v in r.counts.items():
                    message_counts[k] = message_counts.get(k, 0) + v
            new_messages.append({**msg, "content": r.text})
        else:
            new_messages.append(dict(msg))

    total = {**sys_result.counts}
    for k, v in message_counts.items():
        total[k] = total.get(k, 0) + v

    if total:
        logger.warning(
            "Sanitizer modified LLM request (task_id=%s): %s",
            request.task_id,
            ", ".join(f"{k}x{v}" for k, v in sorted(total.items())),
        )

    return dataclasses.replace(
        request,
        system_prompt=new_system,
        messages=new_messages,
    )
