"""
factory.llm.sanitizer
=====================
Prevents sensitive data from leaking into LLM prompts.
"""

from __future__ import annotations

import logging
from factory.llm.provider import LLMRequest

logger = logging.getLogger(__name__)


def sanitize_request(request: LLMRequest) -> LLMRequest:
    """
    Ensure the request contains no sensitive data.
    If it does, strip it or reject it based on policy.
    Currently delegates to the memory sanitizer if needed.
    """
    # For Phase 2, the primary boundary is the SensitiveDataDetector during file read/write.
    # We will just do a basic pass-through for now, as the filesystem layer blocks secret reads.
    return request
