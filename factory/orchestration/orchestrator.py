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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from factory.errors import (
    EmergencyStopError,
    SchedulerError,
    StateError,
    TaskError,
)
from factory.models import (
    AgentResult,
    ApprovalStatus,
    RecoveryStrategy,
    TaskRecord,
    TaskStatus,
    RiskLevel,
)
from factory.orchestration.dag import TaskDAG, TaskNode
from factory.orchestration.retry import (
    FailureKind,
    classify_failure,
    extract_approval_request_id,
)
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
    # Bounded retries (COR-004): how many RETRIES a transient failure
    # earns per task when the node declares no budget of its own.
    # Total attempts = default_max_retries + 1 (first attempt + retries).
    default_max_retries: int = 3
    # Deterministic backoff applied before requeueing a retried task.
    # 0.0 by default — pacing already exists via poll_interval_seconds.
    retry_backoff_seconds: float = 0.0


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
        project_id: str = "",
        plan_id: str = "",
        plan_hash: str = "",
        task_store: Any = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.dag = dag
        self.config = config or OrchestratorConfig()
        self._state_manager = state_manager
        self._recovery_manager = recovery_manager
        self._audit = audit_logger
        self._permission_broker = permission_broker
        # Runtime identity + persistence (Phase E implementation).
        self._project_id = project_id
        self._plan_id = plan_id
        self._plan_hash = plan_hash
        self._task_store = task_store
        self._workspace_root = (
            Path(workspace_root).resolve() if workspace_root else None
        )

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
    # Persistence helpers (no-ops when no project/task store is bound)
    # ------------------------------------------------------------------

    def _seed_tasks(self) -> None:
        if self._task_store is None or not self._project_id:
            return
        for node in self.dag.all_nodes():
            self._task_store.seed_node(
                self._project_id,
                node,
                plan_id=self._plan_id,
                plan_hash=self._plan_hash,
            )

    def _persist_node(self, node: TaskNode, result: AgentResult | None = None) -> None:
        if self._task_store is None or not self._project_id:
            return
        error: str | None = None
        summary: str | None = None
        if result is not None:
            summary = result.summary or None
            if not result.success:
                error = "\n".join(result.errors) or "task failed"
        self._task_store.upsert_node(
            self._project_id, node, error=error, result_summary=summary
        )

    def _resolve_evidence_path(self, raw_path: str) -> Path | None:
        if not raw_path:
            return None
        candidate = Path(raw_path)
        if not candidate.is_absolute() and self._workspace_root:
            candidate = self._workspace_root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        if self._workspace_root is not None:
            try:
                resolved.relative_to(self._workspace_root)
            except ValueError:
                logger.warning(
                    "Evidence path %r escapes the workspace; not recorded.",
                    raw_path,
                )
                return None
        return resolved

    def _persist_result_evidence(
        self, node: TaskNode, result: AgentResult
    ) -> None:
        """Hash and record every evidence artifact the agent declared."""
        if self._task_store is None or not self._project_id:
            return
        for item in result.evidence or []:
            if not isinstance(item, dict):
                continue
            resolved = self._resolve_evidence_path(str(item.get("path", "")))
            if resolved is None or not resolved.is_file():
                continue
            from factory.audit.evidence import attach_file_evidence
            try:
                attach_file_evidence(
                    resolved,
                    project_id=self._project_id,
                    task_id=node.task_id,
                    agent_id=node.agent_type,
                    metadata={"kind": str(item.get("kind", "file"))},
                    db_path=self._task_store.db_path,
                )
            except Exception:  # noqa: BLE001 — evidence loss is surfaced later
                logger.exception(
                    "Failed to attach evidence %s for task %s",
                    resolved, node.task_id,
                )

    def _checkpoint_state(self, node: TaskNode | None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": self._project_id,
            "plan_id": self._plan_id,
            "plan_hash": self._plan_hash,
            "task_statuses": {
                n.task_id: (
                    n.status.value
                    if isinstance(n.status, TaskStatus)
                    else str(n.status)
                )
                for n in self.dag.all_nodes()
            },
            "completed_task_ids": sorted(self.dag.completed_ids()),
            "last_task_id": node.task_id if node is not None else "",
        }

    def _save_checkpoint(
        self, node: TaskNode | None = None, result: AgentResult | None = None
    ) -> None:
        if self._recovery_manager is None or not self._project_id:
            return
        paths: list[Path] = []
        if result is not None:
            for item in result.evidence or []:
                if not isinstance(item, dict):
                    continue
                resolved = self._resolve_evidence_path(str(item.get("path", "")))
                if resolved is not None and resolved.is_file():
                    paths.append(resolved)
        try:
            self._recovery_manager.save_checkpoint(
                self._checkpoint_state(node),
                project_id=self._project_id,
                task_id=node.task_id if node is not None else None,
                workspace_paths=paths or None,
            )
        except Exception:  # noqa: BLE001 — checkpoint loss must be visible
            logger.exception("Failed to save recovery checkpoint.")

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
            project_id=self._project_id,
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

        # COR-008: requeue FAILED nodes that still hold declared retry budget
        # (e.g. marked FAILED by crash recovery or an external caller).
        self._requeue_recoverable_failures()

        # COR-003: reconcile AWAITING_APPROVAL nodes — requeue when the
        # approval is granted in the DB and fingerprint re-verified;
        # terminate when the human explicitly denied it.
        self._reconcile_awaiting_approvals()

        # Dispatch ready tasks up to concurrency limit
        ready = self.dag.ready_tasks()
        if not ready:
            awaiting = [
                n for n in self.dag.all_nodes()
                if n.status == TaskStatus.AWAITING_APPROVAL
            ]
            if awaiting:
                # Nothing can proceed until a human decides — exit the
                # loop honestly instead of spinning to max_steps.
                self._status = OrchestratorStatus.AWAITING_APPROVAL
                if self._audit:
                    self._audit.emit("orchestrator.awaiting_approval", {
                        "task_ids": [n.task_id for n in awaiting],
                        "approval_request_ids": [
                            n.metadata.get("approval_request_id", "")
                            for n in awaiting
                        ],
                    })
                logger.info(
                    "Orchestrator awaiting human approval for %d task(s).",
                    len(awaiting),
                )
                return False
            return True  # pre-existing stall cases keep old spin semantics

        for node in ready:
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

            # Durable state: task is RUNNING before the side effect starts.
            self._persist_node(node)

            # Execute (synchronous in phase 1)
            result = self._dispatch_task(node)

            # Apply the outcome: completed / awaiting-approval / bounded
            # retry / terminal failure (see _record_result).
            self._record_result(node, result)

            # Record outcome, deterministic evidence, and a checkpoint.
            self._persist_node(node, result)
            self._persist_result_evidence(node, result)
            self._save_checkpoint(node, result)

            self.scheduler.complete(node.task_id)

        return True

    # ------------------------------------------------------------------
    # Result handling — bounded retries (COR-004) + approval pause (COR-003)
    # ------------------------------------------------------------------

    def _record_result(self, node: TaskNode, result: AgentResult) -> None:
        """
        Apply one dispatch result to the DAG.

        * success → COMPLETED.
        * awaiting approval → AWAITING_APPROVAL (a pause, not a failure:
          no retry budget consumed; requeued only after grant +
          fingerprint re-verification).
        * transient failure with retry budget → requeued to PENDING with
          an incremented retry_count (bounded: retry_count < max_retries).
        * anything else → terminal failure (retry_count >= max_retries,
          non-retryable/unknown classification, or a non-retry recovery
          strategy), marked so the DAG classifies it as unrecoverable.
        """
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
            return

        kind = classify_failure(result)

        # ---- Approval pause (COR-003) --------------------------------
        if kind == FailureKind.AWAITING_APPROVAL:
            request_id = result.approval_request_id or extract_approval_request_id(
                "\n".join(result.errors)
            )
            node.metadata["approval_request_id"] = request_id
            node.metadata["awaiting_since"] = datetime.now(
                timezone.utc
            ).isoformat()
            self.dag.update_status(node.task_id, TaskStatus.AWAITING_APPROVAL)
            if self._audit:
                self._audit.emit("task.awaiting_approval", {
                    "task_id": node.task_id,
                    "name": node.name,
                    "approval_request_id": request_id,
                    "errors": result.errors,
                })
            logger.info(
                "Task %s paused awaiting approval (request_id=%s).",
                node.task_id, request_id or "<unbound>",
            )
            return

        # ---- Ordinary failure → bounded retry (COR-004) ---------------
        strategy = str(node.metadata.get("recovery_strategy", "") or "")
        try:
            max_retries = int(
                node.metadata.get(
                    "max_retries", self.config.default_max_retries
                )
            )
        except (TypeError, ValueError):
            max_retries = self.config.default_max_retries
        max_retries = max(0, max_retries)
        # Declare the budget on the node so DAG-level COR-008
        # classification sees the same numbers the orchestrator uses.
        node.metadata["max_retries"] = max_retries
        retries_used = int(node.metadata.get("retry_count", 0))

        non_retryable_strategy = strategy in {
            RecoveryStrategy.ESCALATE_HUMAN.value,
            RecoveryStrategy.SKIP.value,
        }
        will_retry = (
            kind == FailureKind.TRANSIENT
            and not non_retryable_strategy
            and retries_used < max_retries
        )

        if will_retry:
            attempt = retries_used + 1
            node.metadata["retry_count"] = attempt
            backoff = max(0.0, float(self.config.retry_backoff_seconds))
            if self._audit:
                self._audit.emit("task.retry", {
                    "task_id": node.task_id,
                    "name": node.name,
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "failure_kind": kind.value,
                    "backoff_seconds": backoff,
                    "errors": result.errors,
                })
            logger.warning(
                "Task %s failed (%s) — retry %d/%d scheduled.",
                node.task_id, kind.value, attempt, max_retries,
            )
            if backoff:
                time.sleep(backoff)
            self.dag.update_status(node.task_id, TaskStatus.PENDING)
            return

        # ---- Terminal outcomes ---------------------------------------
        if strategy == RecoveryStrategy.SKIP.value:
            node.metadata["terminal_failure"] = True
            self.dag.update_status(node.task_id, TaskStatus.SKIPPED)
            if self._audit:
                self._audit.emit("task.skipped", {
                    "task_id": node.task_id,
                    "recovery_strategy": strategy,
                    "errors": result.errors,
                })
            logger.warning(
                "Task %s skipped (recovery_strategy=skip).", node.task_id
            )
            return

        node.metadata["terminal_failure"] = True
        if strategy == RecoveryStrategy.ESCALATE_HUMAN.value:
            if self._audit:
                self._audit.emit("task.escalated", {
                    "task_id": node.task_id,
                    "recovery_strategy": strategy,
                    "errors": result.errors,
                })
            logger.warning(
                "Task %s escalated to a human "
                "(recovery_strategy=escalate_human).",
                node.task_id,
            )

        self.dag.update_status(node.task_id, TaskStatus.FAILED)
        if self._audit:
            self._audit.emit(
                "task.failed",
                {
                    "task_id": node.task_id,
                    "errors": result.errors,
                    "failure_kind": kind.value,
                    "retries_used": retries_used,
                    "max_retries": max_retries,
                    "retry_exhausted": retries_used >= max_retries,
                },
            )

    # ------------------------------------------------------------------
    # Requeue passes (COR-008 / COR-003)
    # ------------------------------------------------------------------

    def _requeue_recoverable_failures(self) -> None:
        """
        Requeue FAILED nodes whose declared retry budget remains (COR-008).

        Typically produced by crash recovery or an external caller: a
        failure mark with retries left is not final. Bounded — each
        re-dispatch either succeeds, consumes budget, or terminates with
        a terminal marker.
        """
        for node in self.dag.all_nodes():
            if (
                node.status == TaskStatus.FAILED
                and self.dag.is_failure_recoverable(node.task_id)
            ):
                self.dag.update_status(node.task_id, TaskStatus.PENDING)
                self._persist_node(node)
                if self._audit:
                    self._audit.emit("task.retry_requeued", {
                        "task_id": node.task_id,
                        "retry_count": node.metadata.get("retry_count", 0),
                        "max_retries": node.metadata.get("max_retries", 0),
                        "reason": "failed node retains declared retry budget",
                    })
                logger.info(
                    "Task %s requeued: retry budget remains (%s/%s used).",
                    node.task_id,
                    node.metadata.get("retry_count", 0),
                    node.metadata.get("max_retries", 0),
                )

    def _reconcile_awaiting_approvals(self) -> None:
        """
        Reconcile AWAITING_APPROVAL tasks against their DB decisions (COR-003).

        * ``approved`` AND fingerprint re-verified → requeue to PENDING.
        * ``denied`` → terminal failure: the human decided against the
          operation, so the parked task ends (audit records the denial).
        * pending / lookup problems / unverifiable rows → stay parked
          (fail-closed: nothing resumes without a verified grant).
        * no recorded request id at all → terminal failure: nothing can
          ever resolve this state, so parking it forever would be a lie.

        Requeue happens ONLY when, in the DB:

        1. the request exists and its status is ``approved``, and
        2. the stored operation fingerprint is present AND re-verifies
           against a fresh recomputation from the request's own material
           fields — legacy/unbound (NULL) fingerprints and tampered rows
           never resume (fail-closed).

        The permission broker then re-checks the EXACT operation again
        during re-execution (operation-bound approvals, SEC-006): if the
        task attempts a materially different operation it lands in a fresh
        approval request instead of riding this grant.
        """
        for node in self.dag.all_nodes():
            if node.status != TaskStatus.AWAITING_APPROVAL:
                continue
            request_id = str(node.metadata.get("approval_request_id", "") or "")
            if not request_id:
                # Unresolvable state — no id can ever be verified.
                node.metadata["terminal_failure"] = True
                self.dag.update_status(node.task_id, TaskStatus.FAILED)
                self._persist_node(node)
                if self._audit:
                    self._audit.emit("task.awaiting_unresolvable", {
                        "task_id": node.task_id,
                        "reason": "awaiting state has no approval_request_id",
                    })
                logger.error(
                    "Task %s awaited an approval with no request id — "
                    "marked failed (nothing could ever resume it).",
                    node.task_id,
                )
                continue

            state = self._approval_state(request_id)

            if state == "denied":
                node.metadata["terminal_failure"] = True
                self.dag.update_status(node.task_id, TaskStatus.FAILED)
                self._persist_node(node)
                if self._audit:
                    self._audit.emit("task.approval_denied", {
                        "task_id": node.task_id,
                        "approval_request_id": request_id,
                    })
                logger.warning(
                    "Task %s terminated: approval %s was DENIED.",
                    node.task_id, request_id,
                )
                continue

            if state != "approved":
                continue  # pending / missing / unverifiable → stay parked

            node.metadata["approval_requeued_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            self.dag.update_status(node.task_id, TaskStatus.PENDING)
            self._persist_node(node)
            if self._audit:
                self._audit.emit("task.approval_granted_requeued", {
                    "task_id": node.task_id,
                    "approval_request_id": request_id,
                })
            logger.info(
                "Task %s requeued after approval %s (fingerprint re-verified).",
                node.task_id, request_id,
            )

    def _approval_state(self, request_id: str) -> str:
        """
        Look up an approval decision in the DB with fail-closed semantics.

        Returns ``"approved"`` / ``"denied"`` / ``"pending"`` /
        ``"missing"`` (row absent) / ``"unverified"`` (lookup failed, a
        legacy row, or a fingerprint that does not re-verify).

        Only ``"approved"`` is ever sufficient to resume: the stored
        operation fingerprint must be present AND match a fresh
        recomputation from the request's own material fields — legacy
        NULL/unbound approvals and tampered/stale rows never clear.
        """
        broker = self._permission_broker
        if broker is None:
            return "unverified"
        mgr = getattr(broker, "approval_manager", None)
        if mgr is None:
            return "unverified"
        try:
            request = mgr.get_request(request_id)
        except Exception as exc:  # noqa: BLE001 — resume must fail closed
            logger.debug("Approval lookup failed for %s: %s", request_id, exc)
            return "unverified"
        if request is None:
            return "missing"

        status = (
            request.status.value
            if isinstance(request.status, ApprovalStatus)
            else str(request.status)
        )
        if status == ApprovalStatus.DENIED.value:
            return "denied"
        if status != ApprovalStatus.APPROVED.value:
            return "pending"

        if not request.operation_fingerprint:
            logger.warning(
                "Approval %s has no operation fingerprint (legacy/unbound) "
                "— not resuming.",
                request_id,
            )
            return "unverified"

        from factory.permissions.approvals import make_operation_fingerprint
        recomputed = make_operation_fingerprint(
            request.action_category or request.action,
            request.target,
            request.action,
        )
        if recomputed != request.operation_fingerprint:
            logger.warning(
                "Approval %s failed fingerprint re-verification — "
                "not resuming.",
                request_id,
            )
            return "unverified"
        return "approved"

    def run(self, max_steps: int = 10_000) -> OrchestratorStatus:
        """
        Run the orchestrator loop until completion, stop, or max_steps.

        Returns the final OrchestratorStatus.
        """
        self._seed_tasks()
        self._save_checkpoint()
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

        # Final durable snapshot so a restart reconstructs the true state.
        for node in self.dag.all_nodes():
            try:
                self._persist_node(node)
            except Exception:  # noqa: BLE001
                logger.exception("Final persist failed for %s", node.task_id)
        self._save_checkpoint()
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
                "awaiting_approval": len(self.dag.awaiting_approval_ids()),
            },
        }
