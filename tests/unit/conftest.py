"""
tests/unit/conftest.py
=====================
Per-test fixtures for factory agent unit tests.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from factory.llm.provider import LLMRequest, LLMResponse


class _MockLLMRouter:
    """Mock LLM router that returns controlled LLMResponse objects."""
    def __init__(self, response: LLMResponse | None = None):
        self._response = response or LLMResponse(
            request_id="test",
            content="",
            start_time=None,
            end_time=None,
            duration_ms=0.0,
            success=True,
            error="",
            structured_data={},
        )

    def complete(self, request: LLMRequest, retry_count: int = 0) -> LLMResponse:
        return self._response


@pytest.fixture
def mock_llm_router():
    """Provide a mock LLM router for agents that need it."""
    return _MockLLMRouter(
        LLMResponse(
            request_id="test",
            content="",
            start_time=None,
            end_time=None,
            duration_ms=0.0,
            success=True,
            error="",
            structured_data={"_dummy": "data"},
        )
    )


@pytest.fixture
def mock_llm_router_with_planner():
    """Provide a mock LLM router with PlannerProposal-structured data."""
    from factory.llm.schemas.planner import PlannerProposal, PlannerTask
    return _MockLLMRouter(
        LLMResponse(
            request_id="test",
            content="",
            start_time=None,
            end_time=None,
            duration_ms=0.0,
            success=True,
            error="",
            structured_data=PlannerProposal(
                summary="Test project plan",
                decisions=[],
                assumptions=[],
                tasks=[
                    PlannerTask(
                        task_id="task-build",
                        name="Build",
                        agent_type="builder",
                        depends_on=[],
                        description="Implement the required functionality",
                        metadata={},
                    ),
                    PlannerTask(
                        task_id="task-api",
                        name="API Integration Test",
                        agent_type="qa",
                        depends_on=[],
                        description="Run API-specific integration tests",
                        metadata={},
                    ),
                ],
            ).model_dump(),
        )
    )


@pytest.fixture
def mock_llm_router_with_reviewer():
    """Provide a mock LLM router with ReviewerProposal-structured data."""
    from factory.llm.schemas.reviewer import ReviewerProposal
    return _MockLLMRouter(
        LLMResponse(
            request_id="test",
            content="",
            start_time=None,
            end_time=None,
            duration_ms=0.0,
            success=True,
            error="",
            structured_data=ReviewerProposal(
                summary="Review summary",
                approved=True,
                defects=[
                    {"file_path": "test.py", "description": "Empty file", "severity": "low", "suggested_fix": "Add content"},
                ],
                risks=[],
            ).model_dump(),
        )
    )


@pytest.fixture
def mock_llm_router_with_qa():
    """Provide a mock LLM router with QAProposal-structured data."""
    from factory.llm.schemas.qa import QAProposal
    # Check the test name to determine if tests pass or fail
    test_name = str(pytest.__dict__.get("_nodeid", "")).lower()
    has_failing = "failing" in test_name or "fail" in test_name
    
    all_passed = not has_failing
    
    return _MockLLMRouter(
        LLMResponse(
            request_id="test",
            content="",
            start_time=None,
            end_time=None,
            duration_ms=0.0,
            success=True,
            error="",
            structured_data=QAProposal(
                summary="All tests passing" if all_passed else "Some tests failing",
                all_passed=all_passed,
                failed_tests=["test_bad"] if not all_passed else [],
                suggestions=[],
            ).model_dump(),
        )
    )


@pytest.fixture
def mock_llm_router_with_security():
    """Provide a mock LLM router with SecurityProposal-structured data."""
    from factory.llm.schemas.security import SecurityProposal
    # Return 1 vulnerability to test detection of security issues
    vulnerabilities = [{"file_path": ".env", "description": "Secret found", "severity": "high", "suggested_fix": "Remove or move secret"}]
    approved = False
    
    return _MockLLMRouter(
        LLMResponse(
            request_id="test",
            content="",
            start_time=None,
            end_time=None,
            duration_ms=0.0,
            success=True,
            error="",
            structured_data=SecurityProposal(
                summary="Security scan complete",
                approved=approved,
                vulnerabilities=vulnerabilities,
                risks=[],
            ).model_dump(),
        )
    )


@pytest.fixture
def mock_llm_router_with_documentation():
    """Provide a mock LLM router with DocumentationProposal-structured data."""
    from factory.llm.schemas.documentation import DocumentationProposal
    return _MockLLMRouter(
        LLMResponse(
            request_id="test",
            content="",
            start_time=None,
            end_time=None,
            duration_ms=0.0,
            success=True,
            error="",
            structured_data=DocumentationProposal(
                summary="Documentation generated",
                readme_content="# Project README",
            ).model_dump(),
        )
    )


@pytest.fixture
def mock_context_builder():
    """Provide a mock context builder for agents that need it."""
    return MagicMock()