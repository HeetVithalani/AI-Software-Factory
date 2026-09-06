"""
Integration tests for LLM recovery.
"""

import pytest
from factory.llm.retry import RetryPolicy, RecoveryAction

def test_retry_policy_transient():
    policy = RetryPolicy(max_retries=3)
    action = policy.determine_action(1, "connection timeout")
    assert action == RecoveryAction.RETRY_SAME

def test_retry_policy_logic():
    policy = RetryPolicy(max_retries=3)
    action = policy.determine_action(1, "invalid JSON")
    assert action == RecoveryAction.RETRY_AUGMENTED

def test_retry_policy_escalate():
    policy = RetryPolicy(max_retries=3)
    action = policy.determine_action(3, "still failing")
    assert action == RecoveryAction.ESCALATE
