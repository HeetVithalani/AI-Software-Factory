"""
factory.orchestration.scheduler
================================
Resource-aware task scheduler.

The Scheduler tracks which tasks are currently running, enforces
concurrency limits, and maintains a resource budget (RAM/VRAM) to prevent
overloading the host machine.

Design note
-----------
This machine has 16 GB RAM and 6 GB VRAM (RTX 3050).  Running multiple
large Ollama models simultaneously would exhaust VRAM.  The scheduler
holds a ResourceBudget that tracks committed resources and refuses to
schedule a task whose model exceeds available headroom.

The scheduler is synchronous and single-threaded by design — the
orchestrator loop drives it.  Async/parallel execution can be layered
on top in a future phase without changing the scheduler's contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from factory.errors import SchedulerError
from factory.models import TaskRecord, TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class ResourceBudget:
    """
    Tracks RAM and VRAM consumption of concurrently running tasks.

    All values are in gigabytes.
    """
    total_ram_gb: float = 16.0
    total_vram_gb: float = 6.0
    max_inference_ram_gb: float = 10.0
    max_inference_vram_gb: float = 5.5

    _committed_ram_gb: float = field(default=0.0, repr=False)
    _committed_vram_gb: float = field(default=0.0, repr=False)
    # task_id → (ram_gb, vram_gb)
    _allocations: dict[str, tuple[float, float]] = field(
        default_factory=dict, repr=False
    )

    @property
    def available_ram_gb(self) -> float:
        return max(0.0, self.max_inference_ram_gb - self._committed_ram_gb)

    @property
    def available_vram_gb(self) -> float:
        return max(0.0, self.max_inference_vram_gb - self._committed_vram_gb)

    def can_allocate(self, ram_gb: float, vram_gb: float) -> bool:
        """Return True if the requested resources fit within current headroom."""
        return (
            self._committed_ram_gb + ram_gb <= self.max_inference_ram_gb
            and self._committed_vram_gb + vram_gb <= self.max_inference_vram_gb
        )

    def allocate(self, task_id: str, ram_gb: float, vram_gb: float) -> None:
        """Reserve resources for a task. Raises SchedulerError if insufficient."""
        if not self.can_allocate(ram_gb, vram_gb):
            raise SchedulerError(
                f"Insufficient resources for task {task_id!r}: "
                f"need RAM={ram_gb:.1f}GB (have {self.available_ram_gb:.1f}GB), "
                f"VRAM={vram_gb:.1f}GB (have {self.available_vram_gb:.1f}GB)."
            )
        self._allocations[task_id] = (ram_gb, vram_gb)
        self._committed_ram_gb += ram_gb
        self._committed_vram_gb += vram_gb
        logger.debug(
            "Allocated RAM=%.1fGB VRAM=%.1fGB for task %s. "
            "Remaining: RAM=%.1fGB VRAM=%.1fGB.",
            ram_gb, vram_gb, task_id,
            self.available_ram_gb, self.available_vram_gb,
        )

    def release(self, task_id: str) -> None:
        """Release resources previously allocated to a task."""
        if task_id not in self._allocations:
            return
        ram, vram = self._allocations.pop(task_id)
        self._committed_ram_gb = max(0.0, self._committed_ram_gb - ram)
        self._committed_vram_gb = max(0.0, self._committed_vram_gb - vram)
        logger.debug(
            "Released RAM=%.1fGB VRAM=%.1fGB for task %s.",
            ram, vram, task_id,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "committed_ram_gb": round(self._committed_ram_gb, 2),
            "committed_vram_gb": round(self._committed_vram_gb, 2),
            "available_ram_gb": round(self.available_ram_gb, 2),
            "available_vram_gb": round(self.available_vram_gb, 2),
            "active_allocations": len(self._allocations),
        }


@dataclass
class ScheduledTask:
    """A task that has been accepted by the scheduler for execution."""
    task_record: TaskRecord
    scheduled_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # Resource allocation for this task
    ram_gb: float = 0.0
    vram_gb: float = 0.0


class Scheduler:
    """
    Resource-aware task scheduler.

    The scheduler sits between the orchestrator and task execution:
    - enforces max_concurrent_tasks limit
    - enforces ResourceBudget (RAM/VRAM)
    - tracks running tasks
    - signals when capacity is available

    It does NOT execute tasks itself — the orchestrator calls
    ``schedule(task)`` and is responsible for dispatching to agents.
    """

    def __init__(
        self,
        max_concurrent: int = 2,
        budget: ResourceBudget | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.budget = budget or ResourceBudget()
        self._running: dict[str, ScheduledTask] = {}

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def is_at_capacity(self) -> bool:
        return self.running_count >= self.max_concurrent

    def can_schedule(
        self,
        task: TaskRecord,
        *,
        required_ram_gb: float = 0.5,
        required_vram_gb: float = 0.0,
    ) -> bool:
        """
        Return True if this task can be scheduled right now.

        Checks:
        1. Concurrency limit not exceeded
        2. Resource budget has headroom
        """
        if self.is_at_capacity:
            logger.debug(
                "Cannot schedule task %s: at concurrency limit (%d/%d).",
                task.task_id, self.running_count, self.max_concurrent,
            )
            return False
        if not self.budget.can_allocate(required_ram_gb, required_vram_gb):
            logger.debug(
                "Cannot schedule task %s: insufficient resources.",
                task.task_id,
            )
            return False
        return True

    def schedule(
        self,
        task: TaskRecord,
        *,
        required_ram_gb: float = 0.5,
        required_vram_gb: float = 0.0,
    ) -> ScheduledTask:
        """
        Accept a task into the running set.

        Raises SchedulerError if the task cannot be scheduled.
        """
        if not self.can_schedule(task, required_ram_gb=required_ram_gb,
                                  required_vram_gb=required_vram_gb):
            raise SchedulerError(
                f"Cannot schedule task {task.task_id!r} ({task.name!r}): "
                "concurrency limit or resource budget exceeded."
            )
        self.budget.allocate(task.task_id, required_ram_gb, required_vram_gb)
        scheduled = ScheduledTask(
            task_record=task,
            ram_gb=required_ram_gb,
            vram_gb=required_vram_gb,
        )
        self._running[task.task_id] = scheduled
        logger.info(
            "Scheduled task %s (%s) | RAM=%.1fGB VRAM=%.1fGB | "
            "running=%d/%d",
            task.task_id, task.name,
            required_ram_gb, required_vram_gb,
            self.running_count, self.max_concurrent,
        )
        return scheduled

    def complete(self, task_id: str) -> None:
        """Mark a task as done and release its resources."""
        if task_id in self._running:
            self._running.pop(task_id)
            self.budget.release(task_id)
            logger.debug("Scheduler: task %s completed, resources released.", task_id)

    def running_tasks(self) -> list[ScheduledTask]:
        return list(self._running.values())

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_concurrent": self.max_concurrent,
            "running_count": self.running_count,
            "is_at_capacity": self.is_at_capacity,
            "budget": self.budget.snapshot(),
            "running_task_ids": list(self._running.keys()),
        }
