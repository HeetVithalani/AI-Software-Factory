"""
tests/unit/test_llm_sanitizer.py
=================================
Deterministic unit tests for the request-content sanitizer (SEC-008).

Covers: every supported secret family (positive detection), redaction
correctness (placeholder in, secret out, context preserved), supported
message shapes, non-mutation of the caller's request, false-positive
resistance, and the logging/audit boundaries (raw secrets never logged
or persisted).

All databases live under pytest tmp_path. No network, no models.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factory.llm.provider import LLMRequest, LLMResponse
from factory.llm.sanitizer import (
    RedactionResult,
    redact_text,
    sanitize_request,
)


def _req(system: str = "", messages: list | None = None) -> LLMRequest:
    return LLMRequest(
        model="ollama:test",
        system_prompt=system,
        messages=messages if messages is not None else [],
        task_id="task-1",
    )


# ---------------------------------------------------------------------------
# Positive detections — one case per supported family
# ---------------------------------------------------------------------------

class TestPositiveDetections:
    @pytest.mark.parametrize("name,secret,placeholder", [
        (
            "private-key-block",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z\n"
            "-----END RSA PRIVATE KEY-----",
            "[REDACTED:private-key-block]",
        ),
        ("aws-access-key-id", "AKIAIOSFODNN7EXAMPLE", "[REDACTED:aws-access-key-id]"),
        (
            "aws-secret-key",
            "aws_secret_access_key = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'",
            "[REDACTED:aws-secret-key]",
        ),
        ("github-token", "ghp_" + "A" * 36, "[REDACTED:github-token]"),
        (
            "github-fine-grained-token",
            "github_pat_" + "a" * 12 + "_" + "B" * 59,
            "[REDACTED:github-fine-grained-token]",
        ),
        ("openai-api-key", "sk-" + "a1B2" * 8, "[REDACTED:openai-api-key]"),
        ("slack-token", "xoxb-" + "1234567890-abcdefghijklmn", "[REDACTED:slack-token]"),
        ("google-api-key", "AIza" + "Sy" * 17 + "A", "[REDACTED:google-api-key]"),
        ("stripe-key", "sk_live_" + "aB3dE5fG7hI9jK1lM2nO", "[REDACTED:stripe-key]"),
        (
            "jwt",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "[REDACTED:jwt]",
        ),
    ])
    def test_family_detected_and_redacted(self, name, secret, placeholder):
        text = f"intro {secret} outro"
        r = redact_text(text)
        assert secret not in r.text
        assert placeholder in r.text
        assert "intro" in r.text and "outro" in r.text
        assert name in r.counts

    def test_bearer_token_redacted_header_kept(self):
        text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
        r = redact_text(text)
        assert "abcdefghijklmnopqrstuvwxyz012345" not in r.text
        assert "[REDACTED:bearer-token]" in r.text
        assert "Bearer" in r.text  # header name preserved

    def test_url_basic_auth_password_redacted(self):
        text = "fetch https://admin:hunter2secret@internal.example.com/api"
        r = redact_text(text)
        assert "hunter2secret" not in r.text
        assert "admin" in r.text
        assert "internal.example.com" in r.text

    def test_quoted_credential_assignment_redacted(self):
        text = 'api_key = "abcdefghijklmnop123456"'
        r = redact_text(text)
        assert "abcdefghijklmnop123456" not in r.text
        assert "api_key" in r.text

    def test_multiple_secrets_all_redacted(self):
        text = (
            "key1: AKIAIOSFODNN7EXAMPLE token: ghp_"
            + "B" * 36
            + " end"
        )
        r = redact_text(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in r.text
        assert ("ghp_" + "B" * 36) not in r.text
        assert r.total >= 2


# ---------------------------------------------------------------------------
# False-positive resistance — ordinary text and code must survive
# ---------------------------------------------------------------------------

class TestFalsePositives:
    @pytest.mark.parametrize("text", [
        "The token of appreciation was well received.",
        "token = os.environ['TOKEN']  # read from environment",
        "password managers are recommended by security guides",
        "See docs/security.md for how we rotate credentials regularly.",
        "def get_secret(client): return client.secret_value",
        "The API rate limit is 100 requests per minute.",
        "curl https://api.example.com/v1/data?token=abc",  # short value
        "",
        "plain prose with no credentials at all",
    ])
    def test_ordinary_text_untouched(self, text):
        r = redact_text(text)
        assert r.text == text
        assert r.total == 0


# ---------------------------------------------------------------------------
# Request-level behavior and message shapes
# ---------------------------------------------------------------------------

class TestSanitizeRequest:
    def test_system_prompt_and_messages_redacted(self):
        secret = "ghp_" + "C" * 36
        req = _req(
            system=f"System says {secret} ok",
            messages=[
                {"role": "user", "content": f"user has {secret} here"},
                {"role": "assistant", "content": "clean message"},
            ],
        )
        out = sanitize_request(req)
        assert secret not in out.system_prompt
        assert secret not in out.messages[0]["content"]
        assert out.messages[1]["content"] == "clean message"
        assert "[REDACTED:github-token]" in out.system_prompt

    def test_original_request_not_mutated(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        req = _req(system=f"ctx {secret}", messages=[])
        _ = sanitize_request(req)
        assert secret in req.system_prompt  # caller object untouched

    def test_non_string_content_preserved(self):
        req = _req(messages=[{"role": "user", "content": 12345}])
        out = sanitize_request(req)
        assert out.messages[0]["content"] == 12345

    def test_missing_messages_field_tolerated(self):
        req = LLMRequest(model="ollama:t", system_prompt="s", messages=[])
        out = sanitize_request(req)
        assert out.system_prompt == "s"

    def test_non_dict_message_tolerated(self):
        req = _req(messages=["not-a-dict"])  # type: ignore[list-item]
        out = sanitize_request(req)
        assert out.messages == ["not-a-dict"]

    def test_clean_request_passes_through_unchanged(self):
        req = _req(system="You are helpful.", messages=[
            {"role": "user", "content": "Write a function to parse dates."}
        ])
        out = sanitize_request(req)
        assert out.system_prompt == req.system_prompt
        assert out.messages == req.messages


# ---------------------------------------------------------------------------
# Logging / audit boundaries — raw secrets never leak
# ---------------------------------------------------------------------------

class TestLoggingBoundary:
    def test_redaction_log_contains_no_secret(self, caplog):
        secret = "ghp_" + "D" * 36
        with caplog.at_level("WARNING", logger="factory.llm.sanitizer"):
            redact_text(f"leak {secret}")
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert secret not in joined
        assert "github-token" in joined  # pattern name IS logged

    def test_repr_is_content_free(self):
        r = redact_text("token = 'abcdefghijklmnop' and more")
        assert "abcdefghijklmnop" not in repr(r)
        assert isinstance(r, RedactionResult)

    def test_audit_persists_redacted_response(self, tmp_path):
        from factory.database import initialize_database
        from factory.llm.audit import LLMCallAuditor
        from factory.database import get_connection

        db = tmp_path / "factory.db"
        initialize_database(db)
        auditor = LLMCallAuditor(db_path=str(db))

        secret = "sk-" + "Z" * 40
        request = _req(system="s")
        response = LLMResponse(
            request_id="r-1",
            content=f"Here is the key you asked for: {secret}",
            start_time=datetime.now(timezone.utc),
            end_time=datetime.now(timezone.utc),
            duration_ms=1.0,
            success=False,
            error=f"upstream refused: {secret}",
        )
        auditor.record_call(request, response)

        with get_connection(db) as conn:
            row = conn.execute(
                "SELECT raw_response, error FROM llm_calls"
            ).fetchone()
        assert row is not None
        assert secret not in row["raw_response"]
        assert secret not in row["error"]
        assert "[REDACTED:openai-api-key]" in row["raw_response"]
