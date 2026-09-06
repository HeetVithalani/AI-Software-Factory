"""
factory.orchestration.dag
=========================
Task Directed Acyclic Graph (DAG) for dependency-ordered execution.

The DAG holds TaskNode objects and provides:
- dependency validation (cycle detection via DFS)
- topological sort
- "ready tasks" query (all tasks whose dependencies are complete)
- status mutation helpers

Design note
-----------
This DAG is in-memory only. Persistence of task records happens through
the TaskRepository in factory.database. The DAG is reconstructed from
the DB on boot/recovery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator

from factory.errors import TaskCycleError
from factory.models import TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class TaskNode:
    """
    A single node in the task DAG.

    ``task_id`` must be globally unique within a project.
    ``depends_on`` lists task_ids that must be COMPLETED before this
    task can be scheduled.
    """
    task_id: str
    name: str
    agent_type: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    metadata: dict = field(default_factory=dict)

    def is_ready(self, completed_ids: set[str]) -> bool:
        """Return True if all dependencies are in the completed set."""
        return all(dep in completed_ids for dep in self.depends_on)

    def is_terminal(self) -> bool:
        """Return True if this node is in a terminal state."""
        return self.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED,
        }


class TaskDAG:
    """
    Directed Acyclic Graph of TaskNodes with topological execution ordering.

    Usage::

        dag = TaskDAG()
        dag.add(TaskNode("t1", "Plan"))
        dag.add(TaskNode("t2", "Build", depends_on=["t1"]))
        dag.add(TaskNode("t3", "Review", depends_on=["t2"]))
        dag.validate()                      # raises TaskCycleError if cyclic
        for task_id in dag.topological_order():
            print(task_id)
    """

    def __init__(self) -> None:
        self._nodes: dict[str, TaskNode] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, node: TaskNode) -> None:
        """Add a TaskNode to the DAG."""
        if node.task_id in self._nodes:
            raise ValueError(f"Duplicate task_id in DAG: {node.task_id!r}")
        self._nodes[node.task_id] = node
        logger.debug("DAG: added task %s (%s)", node.task_id, node.name)

    def remove(self, task_id: str) -> None:
        """Remove a task node (and any references to it in depends_on)."""
        self._nodes.pop(task_id, None)
        for node in self._nodes.values():
            if task_id in node.depends_on:
                node.depends_on.remove(task_id)

    def update_status(self, task_id: str, status: TaskStatus) -> None:
        """Update the status of a task node."""
        if task_id not in self._nodes:
            raise KeyError(f"Task {task_id!r} not found in DAG.")
        self._nodes[task_id].status = status
        logger.debug("DAG: task %s → %s", task_id, status.value)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> TaskNode | None:
        return self._nodes.get(task_id)

    def all_nodes(self) -> list[TaskNode]:
        return list(self._nodes.values())

    def node_count(self) -> int:
        return len(self._nodes)

    def completed_ids(self) -> set[str]:
        return {
            nid for nid, n in self._nodes.items()
            if n.status == TaskStatus.COMPLETED
        }

    def failed_ids(self) -> set[str]:
        return {
            nid for nid, n in self._nodes.items()
            if n.status == TaskStatus.FAILED
        }

    def pending_ids(self) -> set[str]:
        return {
            nid for nid, n in self._nodes.items()
            if n.status == TaskStatus.PENDING
        }

    def is_complete(self) -> bool:
        """True when all nodes are in a terminal state."""
        return all(n.is_terminal() for n in self._nodes.values())

    def has_unrecoverable_failure(self) -> bool:
        """
        True when a FAILED node blocks one or more PENDING nodes and there
        is no alternative path to completion.
        """
        failed = self.failed_ids()
        if not failed:
            return False
        # Any pending task that depends on a failed task is blocked
        for node in self._nodes.values():
            if node.status == TaskStatus.PENDING:
                if any(dep in failed for dep in node.depends_on):
                    return True
        return False

    def ready_tasks(self) -> list[TaskNode]:
        """
        Return all PENDING tasks whose dependencies are all COMPLETED.
        Excludes tasks blocked by FAILED dependencies.
        """
        done = self.completed_ids()
        return [
            node for node in self._nodes.values()
            if node.status == TaskStatus.PENDING and node.is_ready(done)
        ]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """
        Validate DAG integrity:
        1. All depends_on references point to existing nodes.
        2. Graph is acyclic (DFS cycle detection).

        Raises
        ------
        ValueError
            If a dependency references a non-existent node.
        TaskCycleError
            If the graph contains a cycle.
        """
        # Check all dependency references exist
        for node in self._nodes.values():
            for dep in node.depends_on:
                if dep not in self._nodes:
                    raise ValueError(
                        f"Task {node.task_id!r} depends on unknown task {dep!r}"
                    )

        # DFS cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {tid: WHITE for tid in self._nodes}

        def dfs(tid: str) -> None:
            color[tid] = GRAY
            for dep in self._nodes[tid].depends_on:
                if color[dep] == GRAY:
                    raise TaskCycleError(
                        f"Cycle detected in task DAG involving task {tid!r}"
                    )
                if color[dep] == WHITE:
                    dfs(dep)
            color[tid] = BLACK

        for tid in list(self._nodes):
            if color[tid] == WHITE:
                dfs(tid)

        logger.debug("DAG validation passed (%d nodes).", len(self._nodes))

    # ------------------------------------------------------------------
    # Topological ordering
    # ------------------------------------------------------------------

    def topological_order(self) -> list[str]:
        """
        Return task_ids in a valid topological execution order.

        Validates the DAG first; raises TaskCycleError if cyclic.
        """
        self.validate()

        # Kahn's algorithm
        in_degree: dict[str, int] = {tid: 0 for tid in self._nodes}
        for node in self._nodes.values():
            for dep in node.depends_on:
                in_degree[node.task_id] = in_degree.get(node.task_id, 0) + 1

        queue: list[str] = [tid for tid, deg in in_degree.items() if deg == 0]
        order: list[str] = []

        # Build reverse adjacency (which tasks depend ON this one)
        reverse: dict[str, list[str]] = {tid: [] for tid in self._nodes}
        for node in self._nodes.values():
            for dep in node.depends_on:
                reverse[dep].append(node.task_id)

        while queue:
            tid = queue.pop(0)
            order.append(tid)
            for dependent in reverse[tid]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(self._nodes):
            raise TaskCycleError("Topological sort failed — cycle in DAG.")

        return order

    def __repr__(self) -> str:
        return f"TaskDAG(nodes={len(self._nodes)})"
