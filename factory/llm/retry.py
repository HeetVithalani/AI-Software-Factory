"""
factory.llm.retry
=================
Bounded retry and recovery strategy for LLM calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
import time

logger = logging.getLogger(__name__)


class RecoveryAction(str, Enum):
    RETRY_SAME = "retry_same"
    RETRY_AUGMENTED = "retry_augmented"
    ESCALATE = "escalate"


@dataclass
class RetryPolicy:
    """Configures retry behavior for LLM interactions."""
    max_retries: int = 3
    base_backoff_sec: float = 2.0
    
    def determine_action(self, attempt: int, error: str) -> RecoveryAction:
        if attempt >= self.max_retries:
            return RecoveryAction.ESCALATE
            
        if "timeout" in error.lower() or "connection" in error.lower():
            # Transient infra failure -> same prompt
            return RecoveryAction.RETRY_SAME
            
        # Parse/validation/logic failure -> augment prompt with error
        return RecoveryAction.RETRY_AUGMENTED

    def wait(self, attempt: int) -> None:
        """Sleep with exponential backoff."""
        if attempt > 0:
            sleep_time = self.base_backoff_sec * (2 ** (attempt - 1))
            logger.info("Retrying LLM call in %.1fs (attempt %d)", sleep_time, attempt + 1)
            time.sleep(sleep_time)
