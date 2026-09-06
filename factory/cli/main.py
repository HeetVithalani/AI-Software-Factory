"""
factory.cli.main
================
Main CLI for the AI Software Factory.
Commands:
- status: show factory status
- run <project_path> [--task-desc "description"]: start/resume an end-to-end run
"""

import argparse
import sys
import logging
from pathlib import Path
from uuid import uuid4

from factory.config import get_settings
from factory.database import initialize_database
from factory.state import StateManager
from factory.orchestration.orchestrator import Orchestrator, OrchestratorConfig
from factory.orchestration.dag import TaskDAG
from factory.permissions.broker import PermissionBroker
from factory.permissions.risk import RiskLevel
from factory.permissions.policies import load_policies
from factory.audit.logger import AuditLogger
from factory.llm.router import LLMRouter
from factory.context.builder import ContextBuilder

from factory.agents.builder import BuilderAgent

logger = logging.getLogger(__name__)


def run_status() -> int:
    sm = StateManager()
    state = sm.boot_state()
    print(f"Status: {'PAUSED' if state.paused else 'IDLE'}")
    return 0


def run_project(project_path: str, task_desc: str) -> int:
    path = Path(project_path).resolve()
    print(f"Running factory on project at {path}")
    if task_desc:
        print(f"Task description: {task_desc}")
    else:
        print("Using default build task.")
        task_desc = "Implement the main logic based on the project requirements."

    # 1. Init
    initialize_database()
    sm = StateManager()
    state = sm.boot_state()
    audit_logger = AuditLogger()
    broker = PermissionBroker(policies=load_policies())

    llm_router = LLMRouter()
    
    # Needs a FactoryState object but currently ContextBuilder uses state_manager or just global memory...
    # Oh, ContextBuilder expects `FactoryState` but `state` is a FactoryState instance!
    context_builder = ContextBuilder(state=state)

    # 2. DAG
    dag = TaskDAG()
    task_id = f"build-{uuid4().hex[:8]}"
    from factory.orchestration.dag import TaskNode
    dag.add(TaskNode(
        task_id=task_id,
        name="Build Project",
        agent_type="builder",
        metadata={"description": task_desc},
    ))

    # 3. Orchestrator
    orch = Orchestrator(
        dag=dag,
        state_manager=sm,
        audit_logger=audit_logger,
        permission_broker=broker,
    )

    # 4. Register Agents
    builder_agent = BuilderAgent(
        workspace_root=path,
        broker=broker,
        audit_logger=audit_logger,
        llm_router=llm_router,
        context_builder=context_builder,
    )
    orch.register_executor("builder", builder_agent.run)
    
    # 5. Run
    status = orch.run()
    print(f"Run completed with status: {status.value}")
    
    return 0 if status.value in ("idle", "running") else 1


def _setup_logging(level: str = "WARNING") -> None:
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s",
        level=getattr(logging, level.upper(), logging.WARNING),
        stream=sys.stderr,
    )

def main() -> int:
    parser = argparse.ArgumentParser(prog="factory")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    subparsers.add_parser("status")
    
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("project_path", type=str)
    run_parser.add_argument("--task-desc", type=str, default="")
    
    args = parser.parse_args()
    
    settings = get_settings()
    _setup_logging("DEBUG")  # Force debug for testing
    
    if args.command == "status":
        return run_status()
    elif args.command == "run":
        return run_project(args.project_path, args.task_desc)
    
    return 1

if __name__ == "__main__":
    sys.exit(main())
