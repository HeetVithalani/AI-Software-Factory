"""
factory.orchestration.orchestrator
===================================
Main execution controller — the factory's kernel loop.

The Orchestrator:
1. Holds references to: DAG, Scheduler, StateManager, RecoveryManager,
   PermissionBroker, AuditLogger
2. Drives the task execution loop (synchronous, step-by-step)
3. Responds to emergency stop signals (file sentinel + state flag)
4. Pauses/resumes the loop on demand
5. Delegates task dispatch to registered AgentExecutors
6. Records all transitions to the audit log

Design constraints from architecture principles:
- No infinite loops — bounded by max retries and DAG completion
- Emergency stop halts dangerous operations immediately,
  safely pauses normal work
- Permission decisions are NOT delegated to an LLM
- Crash recovery inspects workspace before continuing
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from factory.errors import (
    EmergencyStopError,
    SchedulerError,
    StateError,
    TaskError,
)
from factory.models import AgentResult, TaskRecord, TaskStatus, RiskLevel
from factory.orchestration.dag import TaskDAG, TaskNode
from factory.orchestration.scheduler import Scheduler, ResourceBudget
from factory.orchestration.state import OrchestratorStatus

logger = logging.getLogger(__name__)


# A callable that takes a TaskRecord and returns an AgentResult
AgentExecutor = Callable[[TaskRecord], AgentResult]


@dataclass
class OrchestratorConfig:
    """Runtime configuration for the Orchestrator."""
    max_concurrent: int = 2
    poll_interval_seconds: float = 0.1
    checkpoint_every_n_tasks: int = 5
    # Resource budget defaults (GB)
    default_task_ram_gb: float = 0.5
    default_task_vram_gb: float = 0.0


class Orchestrator:
    """
    Central execution controller.

    Usage (conceptual)::

        orch = Orchestrator(dag, config=OrchestratorConfig())
        orch.register_executor("planner", my_planner_fn)
        orch.run()
    """

    def __init__(
        self,
        dag: TaskDAG,
        *,
        config: OrchestratorConfig | None = None,
        state_manager: Any = None,
        recovery_manager: Any = None,
        audit_logger: Any = None,
        permission_broker: Any = None,
        budget: ResourceBudget | None = None,
    ) -> None:
        self.dag = dag
        self.config = config or OrchestratorConfig()
        self._state_manager = state_manager
        self._recovery_manager = recovery_manager
        self._audit = audit_logger
        self._permission_broker = permission_broker

        self.scheduler = Scheduler(
            max_concurrent=self.config.max_concurrent,
            budget=budget or ResourceBudget(),
        )

        self._status: OrchestratorStatus = OrchestratorStatus.IDLE
        self._emergency_stop: bool = False
        self._paused: bool = False
        self._executors: dict[str, AgentExecutor] = {}
        self._tasks_completed: int = 0

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_executor(self, agent_type: str, fn: AgentExecutor) -> None:
        """Register a callable to execute tasks of a given agent_type."""
        self._executors[agent_type] = fn
        logger.debug("Orchestrator: registered executor for %r.", agent_type)

    # ------------------------------------------------------------------
    # Control signals
    # ------------------------------------------------------------------

    @property
    def status(self) -> OrchestratorStatus:
        return self._status

    def emergency_stop(self, reason: str = "operator request") -> None:
        """
        Trigger an emergency stop.

        Immediately blocks new task dispatch.  In-flight tasks are
        allowed to reach their next safe checkpoint but no new work starts.
        """
        self._emergency_stop = True
        self._paused = True
        self._status = OrchestratorStatus.EMERGENCY_STOP
        logger.critical("ORCHESTRATOR EMERGENCY STOP: %s", reason)
        if self._audit:
            self._audit.emit(
                "orchestrator.emergency_stop", {"reason": reason}
            )

    def pause(self) -> None:
        if self._emergency_stop:
            raise StateError("Cannot pause while emergency stop is active.")
        self._paused = True
        self._status = OrchestratorStatus.PAUSED
        logger.info("Orchestrator paused.")

    def resume(self) -> None:
        if self._emergency_stop:
            raise StateError(
                "Cannot resume while emergency stop is active. "
                "Clear the emergency stop first."
            )
        self._paused = False
        self._status = OrchestratorStatus.RUNNING
        logger.info("Orchestrator resumed.")

    def clear_emergency_stop(self) -> None:
        """Allow resumption after manual review. Requires explicit call."""
        self._emergency_stop = False
        self._paused = False
        self._status = OrchestratorStatus.IDLE
        logger.warning("Emergency stop cleared by operator.")

    # ------------------------------------------------------------------
    # Execution loop
    # ------------------------------------------------------------------

    def _check_stop_signals(self) -> None:
        """Check file sentinel and internal flag for emergency stop."""
        if self._emergency_stop:
            raise EmergencyStopError("Emergency stop active.")
        # Check filesystem sentinel
        if self._state_manager:
            if self._state_manager.check_emergency_stop_file():
                self.emergency_stop("Sentinel file detected.")
                raise EmergencyStopError("Emergency stop sentinel file found.")

    def _dispatch_task(self, node: TaskNode) -> AgentResult:
        """
        Dispatch a single task to its registered executor.

        Returns an AgentResult regardless of success/failure.
        """
        executor = self._executors.get(node.agent_type)
        if executor is None:
            # No executor registered — use a stub that fails gracefully
            logger.error(
                "No executor registered for agent_type=%r (task=%s).",
                node.agent_type, node.task_id,
            )
            return AgentResult(
                agent_id=node.agent_type or "unknown",
                task_id=node.task_id,
                success=False,
                errors=[f"No executor registered for agent_type={node.agent_type!r}"],
            )

        # Build a TaskRecord for the executor
        record = TaskRecord(
            task_id=node.task_id,
            project_id="",  # orchestrator doesn't track project_id here
            name=node.name,
            agent_type=node.agent_type,
            description=node.metadata.get("description", ""),
            status=TaskStatus.RUNNING,
            metadata=node.metadata,
        )

        try:
            result = executor(record)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Executor raised exception for task %s: %s", node.task_id, exc
            )
            result = AgentResult(
                agent_id=node.agent_type or "unknown",
                task_id=node.task_id,
                success=False,
                errors=[str(exc)],
            )

        return result

    def step(self) -> bool:
        """
        Execute one scheduling step.

        Returns True if there is still work to do, False when the DAG is
        complete or the orchestrator should stop.
        """
        self._check_stop_signals()

        if self._paused:
            return True  # still work pending but paused

        if self.dag.is_complete():
            self._status = OrchestratorStatus.IDLE
            return False

        if self.dag.has_unrecoverable_failure():
            logger.error(
                "DAG has unrecoverable failures — halting orchestrator."
            )
            self._status = OrchestratorStatus.STOPPED
            return False

        # Dispatch ready tasks up to concurrency limit
        for node in self.dag.ready_tasks():
            ram = node.metadata.get(
                "required_ram_gb", self.config.default_task_ram_gb
            )
            vram = node.metadata.get(
                "required_vram_gb", self.config.default_task_vram_gb
            )

            if not self.scheduler.can_schedule(
                TaskRecord(
                    task_id=node.task_id,
                    project_id="",
                    name=node.name,
                ),
                required_ram_gb=ram,
                required_vram_gb=vram,
            ):
                continue

            # Mark running
            self.dag.update_status(node.task_id, TaskStatus.RUNNING)
            self.scheduler.schedule(
                TaskRecord(
                    task_id=node.task_id,
                    project_id="",
                    name=node.name,
                ),
                required_ram_gb=ram,
                required_vram_gb=vram,
            )

            if self._audit:
                self._audit.emit(
                    "task.started",
                    {"task_id": node.task_id, "name": node.name},
                )

            # Execute (synchronous in phase 1)
            result = self._dispatch_task(node)

            # Update DAG
            if result.success:
                self.dag.update_status(node.task_id, TaskStatus.COMPLETED)
                self._tasks_completed += 1
                if self._audit:
                    self._audit.emit(
                        "task.completed",
                        {
                            "task_id": node.task_id,
                            "summary": result.summary,
                        },
                    )
            else:
                self.dag.update_status(node.task_id, TaskStatus.FAILED)
                if self._audit:
                    self._audit.emit(
                        "task.failed",
                        {
                            "task_id": node.task_id,
                            "errors": result.errors,
                        },
                    )

            self.scheduler.complete(node.task_id)

        return True

    def run(self, max_steps: int = 10_000) -> OrchestratorStatus:
        """
        Run the orchestrator loop until completion, stop, or max_steps.

        Returns the final OrchestratorStatus.
        """
        self._status = OrchestratorStatus.RUNNING
        if self._audit:
            self._audit.emit("orchestrator.started", {})

        try:
            for _ in range(max_steps):
                if not self.step():
                    break
                time.sleep(self.config.poll_interval_seconds)
        except EmergencyStopError:
            self._status = OrchestratorStatus.EMERGENCY_STOP
        except Exception as exc:  # noqa: BLE001
            logger.exception("Orchestrator loop error: %s", exc)
            self._status = OrchestratorStatus.STOPPED

        if self._audit:
            self._audit.emit(
                "orchestrator.stopped",
                {
                    "final_status": self._status,
                    "tasks_completed": self._tasks_completed,
                },
            )
        logger.info(
            "Orchestrator stopped. Status=%s, completed=%d tasks.",
            self._status, self._tasks_completed,
        )
        return self._status

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of orchestrator state."""
        return {
            "status": self._status,
            "emergency_stop": self._emergency_stop,
            "paused": self._paused,
            "tasks_completed": self._tasks_completed,
            "scheduler": self.scheduler.snapshot(),
            "dag": {
                "total": self.dag.node_count(),
                "completed": len(self.dag.completed_ids()),
                "failed": len(self.dag.failed_ids()),
                "pending": len(self.dag.pending_ids()),
                "ready": len(self.dag.ready_tasks()),
            },
        }
