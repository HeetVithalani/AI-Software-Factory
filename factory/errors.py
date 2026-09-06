"""
factory.errors
==============
Structured exception hierarchy for the AI Software Factory.

All factory errors inherit from FactoryError so callers can catch broadly
or specifically. Every exception carries a machine-readable ``code`` string
so audit logs can filter without string-matching message text.
"""

from __future__ import annotations


class FactoryError(Exception):
    """Base class for all factory-internal errors."""

    code: str = "FACTORY_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.__class__.__name__}(code={self.code!r}, message={self.message!r})"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ConfigurationError(FactoryError):
    """Raised when factory configuration is invalid or cannot be loaded."""
    code = "CONFIGURATION_ERROR"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class StateError(FactoryError):
    """Raised when the factory state machine receives an invalid transition."""
    code = "STATE_ERROR"


class EmergencyStopError(FactoryError):
    """Raised when an emergency stop is triggered during execution."""
    code = "EMERGENCY_STOP"


# ---------------------------------------------------------------------------
# Permissions & Security
# ---------------------------------------------------------------------------

class PermissionDeniedError(FactoryError):
    """Raised when an action is denied by the permission broker."""
    code = "PERMISSION_DENIED"


class ApprovalRequiredError(FactoryError):
    """
    Raised when an action requires human approval before it can proceed.
    The ``request_id`` field links to the pending ApprovalRequest in the DB.
    """
    code = "APPROVAL_REQUIRED"

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


class SecurityViolationError(FactoryError):
    """Raised when a security policy is violated (e.g. sensitive data access)."""
    code = "SECURITY_VIOLATION"


class SensitiveDataError(FactoryError):
    """Raised when sensitive data is detected in an unsafe context."""
    code = "SENSITIVE_DATA"


# ---------------------------------------------------------------------------
# Tasks & Orchestration
# ---------------------------------------------------------------------------

class TaskError(FactoryError):
    """Generic error for task-level failures."""
    code = "TASK_ERROR"

    def __init__(self, message: str, *, task_id: str | None = None) -> None:
        super().__init__(message)
        self.task_id = task_id


class TaskCycleError(FactoryError):
    """Raised when a DAG contains a cycle, making execution impossible."""
    code = "TASK_CYCLE"


class SchedulerError(FactoryError):
    """Raised for scheduling failures (e.g. resource exhaustion)."""
    code = "SCHEDULER_ERROR"


class RecoveryError(FactoryError):
    """Raised when crash recovery cannot determine a safe continuation point."""
    code = "RECOVERY_ERROR"


class RecoveryExhaustedError(FactoryError):
    """Raised when all bounded recovery attempts have been exhausted."""
    code = "RECOVERY_EXHAUSTED"


# ---------------------------------------------------------------------------
# Models / AI
# ---------------------------------------------------------------------------

class ModelUnavailableError(FactoryError):
    """Raised when the required AI model is not available or unreachable."""
    code = "MODEL_UNAVAILABLE"

    def __init__(self, message: str, *, model_id: str | None = None) -> None:
        super().__init__(message)
        self.model_id = model_id


class ModelRoutingError(FactoryError):
    """Raised when no suitable model can be found for a given task context."""
    code = "MODEL_ROUTING_ERROR"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class ProjectError(FactoryError):
    """Generic error for project-level failures."""
    code = "PROJECT_ERROR"

    def __init__(self, message: str, *, project_id: str | None = None) -> None:
        super().__init__(message)
        self.project_id = project_id


class ProjectConflictError(FactoryError):
    """
    Raised when attempting to start a project while another is active.
    The factory supports only one active project at a time.
    """
    code = "PROJECT_CONFLICT"


# ---------------------------------------------------------------------------
# Memory / Knowledge
# ---------------------------------------------------------------------------

class MemoryError(FactoryError):
    """Raised for failures in project or global memory operations."""
    code = "MEMORY_ERROR"


class KnowledgeQuarantineError(FactoryError):
    """Raised when a knowledge entry is quarantined and cannot be used."""
    code = "KNOWLEDGE_QUARANTINE"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class ToolError(FactoryError):
    """Generic error for controlled-tool failures."""
    code = "TOOL_ERROR"

    def __init__(self, message: str, *, tool: str | None = None) -> None:
        super().__init__(message)
        self.tool = tool


class ToolTimeoutError(ToolError):
    """Raised when a tool invocation exceeds its configured timeout."""
    code = "TOOL_TIMEOUT"


class PathSandboxError(ToolError):
    """Raised when a tool attempts to access a path outside the allowed sandbox."""
    code = "PATH_SANDBOX"
