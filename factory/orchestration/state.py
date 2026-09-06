"""
factory.orchestration.state
===========================
OrchestratorStatus enum — current state of the orchestrator loop.
"""

from __future__ import annotations

from enum import Enum


class OrchestratorStatus(str, Enum):
    """High-level status of the orchestrator control loop."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    EMERGENCY_STOP = "emergency_stop"
    RECOVERING = "recovering"
    AWAITING_APPROVAL = "awaiting_approval"
