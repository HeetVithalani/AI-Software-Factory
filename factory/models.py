"""
factory.models
==============
Core data models for the AI Software Factory.

These Pydantic models are the canonical representations of projects, tasks,
approvals, checkpoints, and model records that cross component boundaries.
Persistence is handled by factory.database; these are pure data containers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    """Lifecycle states for a factory task."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ProjectStatus(str, Enum):
    """Lifecycle states for a project."""
    INTAKE = "intake"
    PLANNING = "planning"
    BUILDING = "building"
    REVIEWING = "reviewing"
    TESTING = "testing"
    AUDITING = "auditing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class RiskLevel(str, Enum):
    """Risk classification for actions and tasks."""
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalStatus(str, Enum):
    """Current state of an approval request."""
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    INHERITED = "inherited"   # approved by scope inheritance
    EXPIRED = "expired"


class QualityProfile(str, Enum):
    """Project quality execution profiles."""
    FAST = "fast"
    BALANCED = "balanced"
    HARDENED = "hardened"


class ModelProvider(str, Enum):
    """AI model provider types."""
    OLLAMA = "ollama"
    OPENCODE = "opencode"
    STUB = "stub"  # for testing without a real model


class RecoveryStrategy(str, Enum):
    """Recovery strategies for failed tasks."""
    RETRY_SAME = "retry_same"
    RETRY_DIFFERENT_MODEL = "retry_different_model"
    DECOMPOSE = "decompose"
    SKIP = "skip"
    ESCALATE_HUMAN = "escalate_human"


# ---------------------------------------------------------------------------
# Project record
# ---------------------------------------------------------------------------

class ProjectRecord(BaseModel):
    """Persistent project record stored in the database."""
    model_config = {"use_enum_values": True}

    project_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.INTAKE
    quality_profile: QualityProfile = QualityProfile.BALANCED
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    workspace_path: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    def touch(self) -> None:
        """Update the ``updated_at`` timestamp to now."""
        self.updated_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Task record
# ---------------------------------------------------------------------------

class TaskRecord(BaseModel):
    """Persistent task record stored in the database."""
    model_config = {"use_enum_values": True}

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    name: str
    description: str = ""
    agent_type: str = ""          # e.g. "planner", "builder"
    status: TaskStatus = TaskStatus.PENDING
    risk_level: RiskLevel = RiskLevel.SAFE
    depends_on: list[str] = Field(default_factory=list)  # task_ids
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    recovery_strategy: RecoveryStrategy = RecoveryStrategy.RETRY_SAME
    result_summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING
        self.started_at = datetime.now(timezone.utc)

    def mark_completed(self, summary: str = "") -> None:
        self.status = TaskStatus.COMPLETED
        self.completed_at = datetime.now(timezone.utc)
        self.result_summary = summary

    def mark_failed(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.completed_at = datetime.now(timezone.utc)
        self.error = error
        self.retry_count += 1


# ---------------------------------------------------------------------------
# Approval models
# ---------------------------------------------------------------------------

class ApprovalRequest(BaseModel):
    """A human-approval request raised by the permission broker."""
    model_config = {"use_enum_values": True}

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None

    # What exactly is being requested
    action: str
    target: str
    reason: str
    expected_effect: str
    risk_level: RiskLevel
    consequence_if_denied: str

    # Scope boundary — used to match future inheritance
    action_category: str = ""     # e.g. "git.commit", "shell.run"
    scope_key: str = ""           # deterministic key for scope matching

    requested_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: datetime | None = None
    decided_by: str = "human"
    decision_note: str = ""


class ApprovalDecision(BaseModel):
    """A recorded decision on an ApprovalRequest."""
    model_config = {"use_enum_values": True}

    request_id: str
    status: ApprovalStatus          # APPROVED / DENIED
    decided_by: str = "human"
    decision_note: str = ""
    decided_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # If approved, does this grant scope inheritance for similar future actions?
    grants_scope_inheritance: bool = False
    scope_key: str = ""


# ---------------------------------------------------------------------------
# Checkpoint record
# ---------------------------------------------------------------------------

class CheckpointRecord(BaseModel):
    """A snapshot of orchestrator state written before and after risky steps."""
    model_config = {"use_enum_values": True}

    checkpoint_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str | None = None
    task_id: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # JSON-serialisable state snapshot
    orchestrator_state: dict[str, Any] = Field(default_factory=dict)
    # Hash of critical workspace files for integrity verification
    workspace_hashes: dict[str, str] = Field(default_factory=dict)
    is_valid: bool = True
    invalidated_reason: str = ""


# ---------------------------------------------------------------------------
# Model registry records
# ---------------------------------------------------------------------------

class ModelCapability(BaseModel):
    """Capability profile for a single AI model."""
    # Task types this model is good at
    task_types: list[str] = Field(default_factory=list)
    # Max context window in tokens (approximate)
    context_window: int = 4096
    # Relative speed rating 1–10 (10 = fastest)
    speed_rating: int = 5
    # Relative quality rating 1–10
    quality_rating: int = 5
    # Approximate VRAM usage in GB (0 = CPU-only)
    vram_gb: float = 0.0
    # Approximate RAM usage in GB
    ram_gb: float = 1.0
    # Whether this model can handle sensitive/private data safely
    privacy_safe: bool = True


class ModelRecord(BaseModel):
    """A model registered in the factory model registry."""
    model_config = {"use_enum_values": True}

    model_id: str              # e.g. "ollama:qwen2.5-coder:7b"
    display_name: str
    provider: ModelProvider
    # Provider-specific identifier (e.g. Ollama model tag)
    provider_model_name: str
    capability: ModelCapability = Field(default_factory=ModelCapability)
    available: bool = True
    last_checked_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent result
# ---------------------------------------------------------------------------

class AgentResult(BaseModel):
    """Structured output from any agent invocation."""
    model_config = {"use_enum_values": True}

    agent_id: str
    task_id: str | None = None
    project_id: str | None = None
    success: bool
    summary: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)
    # Deterministic evidence produced by the agent (file paths, hashes, etc.)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    produced_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
