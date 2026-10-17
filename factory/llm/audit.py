"""
factory.llm.audit
=================
Persistence for LLM call records.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from factory.database import get_connection, to_json
from factory.llm.provider import LLMRequest, LLMResponse

logger = logging.getLogger(__name__)


class LLMCallAuditor:
    """Writes LLM request/response pairs to the llm_calls table."""

    def __init__(self, db_path: str | None = None) -> None:
        from factory.config import DB_PATH
        self._db_path = db_path or DB_PATH

    def record_call(self, request: LLMRequest, response: LLMResponse, retry_count: int = 0) -> int:
        """Persist a completed LLM call (success or failure)."""
        provider_name = request.model.split(":")[0] if ":" in request.model else "unknown"
        
        # Defense-in-depth (SEC-008): the model response can echo
        # secrets that were present in context; redact them before they
        # reach the database. Only placeholders are stored — the raw
        # values are never persisted or logged.
        from factory.llm.sanitizer import redact_text
        raw_response = redact_text(response.content).text
        error_text = redact_text(response.error or "").text
        structured = to_json(response.structured_data) if response.structured_data else "{}"

        try:
            with get_connection(self._db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO llm_calls (
                        provider, model, request_id, task_id,
                        start_time, end_time, duration_ms,
                        success, tokens_in, tokens_out,
                        retry_count, raw_response, error,
                        structured_output
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provider_name,
                        request.model,
                        response.request_id,
                        request.task_id,
                        response.start_time.isoformat(),
                        response.end_time.isoformat(),
                        response.duration_ms,
                        1 if response.success else 0,
                        response.tokens_in,
                        response.tokens_out,
                        retry_count,
                        raw_response,
                        error_text,
                        structured,
                    )
                )
                conn.commit()
                row_id = int(cursor.lastrowid or 0)
                return row_id
        except Exception as exc:
            logger.error("Failed to write LLM audit record: %s", exc)
            return 0
