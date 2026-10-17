"""
factory.agents.builder
=======================
Builder agent — uses LLM to write source files to the workspace.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from factory.models import AgentResult, TaskRecord
from factory.agents.base import BaseAgent
from factory.llm.schemas.builder import BuilderProposal
from factory.llm.retry import RetryPolicy, RecoveryAction
from factory.llm.structured_output import (
    parse_structured_response,
    parse_structured_data,
    StructuredOutputError,
)

logger = logging.getLogger(__name__)


class BuilderAgent(BaseAgent):
    """
    Autonomous Builder.
    Runs an LLM loop proposing tool calls (shell, filesystem) to complete a task.
    """

    agent_type = "builder"

    def run(self, task: TaskRecord) -> AgentResult:
        self._emit("agent.builder.started", {"task_id": task.task_id})
        
        if not self._llm or not self._context:
            return self._fail(task, "LLM Router or Context Builder not provided to agent")

        max_turns = 50
        fs_tool = self._make_filesystem_tool()
        shell_tool = self._make_shell_tool()
        
        system_prompt = (
            "You are the Builder Agent. Your goal is to write code and execute commands "
            "to fulfill the task description.\n"
            "You can propose ONE tool action at a time in the 'proposed_actions' list. "
            "The system will execute it and return the result.\n"
            "When you are finished, set 'completed': true and propose no actions.\n"
            "Available tools:\n"
            "- shell: set 'tool' to 'shell', 'action_type' to 'shell.run', and 'arguments' to {'command': '<your command>'}\n"
            "- filesystem (write): set 'tool' to 'filesystem', 'action_type' to 'filesystem.write', and 'arguments' to {'path': '<filepath>', 'content': '<file content>'}\n"
            "- filesystem (read): set 'tool' to 'filesystem', 'action_type' to 'filesystem.read', and 'arguments' to {'path': '<filepath>'}\n"
        )
        
        messages = [
            {
                "role": "user",
                "content": f"Task: {task.name}\nDescription: {task.description}\nStart working.",
            }
        ]

        # Task-aware workspace context: deterministic, bounded, read-only
        # excerpts of existing files. Collected once per task so every turn
        # sees the same reference material.
        context_block = ""
        included_files: list[str] = []
        if self.workspace_root is not None:
            try:
                from factory.context.collector import collect_for_task
                task_context = collect_for_task(self.workspace_root, task)
                context_block = task_context.render()
                included_files = task_context.included_paths
            except Exception as exc:  # noqa: BLE001 — context is an aid, never a blocker
                logger.warning(
                    "Context collection failed for task %s: %s",
                    task.task_id, exc,
                )
            if included_files:
                self._emit("agent.builder.context_collected", {
                    "task_id": task.task_id,
                    "files": included_files,
                })
        
        max_turns = 15
        turns = 0
        written: list[str] = []
        errors: list[str] = []
        
        retry_policy = RetryPolicy(max_retries=3)
        
        while turns < max_turns:
            turns += 1
            logger.info("Builder Agent turn %d for task %s", turns, task.task_id)
            
            # 1. Build Context and LLM Request
            request = self._context.build_request(
                task_id=task.task_id,
                system_prompt=system_prompt,
                messages=messages,
                output_schema=BuilderProposal.model_json_schema(),
                project_id=task.project_id,
                agent_id=self.agent_type,
                context_block=context_block or None,
                included_files=included_files,
            )
            
            # 2. Execute LLM Call with Retries
            attempt = 0
            response = None
            proposal = None

            while attempt <= retry_policy.max_retries:
                response = self._llm.complete(request, retry_count=attempt)
                if response.success:
                    try:
                        context_hint = f"BuilderAgent turn {turns} attempt {attempt}"
                        if response.structured_data:
                            proposal = parse_structured_data(
                                response.structured_data,
                                BuilderProposal,
                                context=context_hint,
                            )
                        elif response.content:
                            proposal = parse_structured_response(
                                response.content,
                                BuilderProposal,
                                context=context_hint,
                            )
                        else:
                            raise StructuredOutputError(
                                "LLM returned empty content and no structured data",
                            )
                        break
                    except StructuredOutputError as e:
                        error_msg = str(e)
                        logger.warning("Builder parse error: %s", error_msg)
                        self._emit("agent.builder.parse_error", {
                            "task_id": task.task_id,
                            "attempt": attempt,
                            "error": error_msg,
                        })
                        action = retry_policy.determine_action(attempt, error_msg)
                else:
                    error_msg = response.error or "LLM request failed"
                    logger.warning("LLM request failed: %s", error_msg)
                    action = retry_policy.determine_action(attempt, error_msg)

                if action == RecoveryAction.ESCALATE:
                    break
                elif action == RecoveryAction.RETRY_AUGMENTED:
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Previous output was invalid: {error_msg}. "
                            "Respond with ONLY a valid JSON object matching "
                            "the required schema. No prose, no markdown fences."
                        ),
                    })
                    request = self._context.build_request(
                        task_id=task.task_id,
                        system_prompt=system_prompt,
                        messages=messages,
                        output_schema=BuilderProposal.model_json_schema(),
                        agent_id=self.agent_type,
                        context_block=context_block or None,
                        included_files=included_files,
                    )

                attempt += 1
                retry_policy.wait(attempt)

            if not proposal:
                return self._fail(task, "Failed to get valid BuilderProposal from LLM after retries")


            # Append model's response to transcript
            messages.append({"role": "assistant", "content": response.content})
            
            # Check completion
            if proposal.completed:
                logger.info("Builder finished task %s", task.task_id)
                break
                
            # 3. Execute Proposed Actions
            if not proposal.proposed_actions:
                messages.append({"role": "user", "content": "You didn't set completed=true but proposed no actions. Please propose an action or complete."})
                continue

            # For Phase 2, we just take the first action proposed
            action = proposal.proposed_actions[0]
            action_result, stop_result = self._execute_action(
                action,
                task=task,
                fs_tool=fs_tool,
                shell_tool=shell_tool,
                written=written,
                errors=errors,
            )
            if stop_result is not None:
                # A human approval gates this action: pause the task
                # (COR-003) rather than burning turns or mislabelling it
                # as an ordinary failure.
                return stop_result

            messages.append({
                "role": "user",
                "content": f"Action {action.action_type} executed.\nResult:\n{action_result}"
            })
            
        if turns >= max_turns:
            return self._fail(task, f"Exceeded max turns ({max_turns}) without completing.")

        self._emit("agent.builder.completed", {
            "task_id": task.task_id,
            "written": written,
            "errors": errors,
        })

        summary = f"Completed in {turns} turns. Wrote {len(written)} files."
        if errors:
            summary += f" {len(errors)} errors encountered."

        return self._ok(
            task,
            summary=summary,
            outputs={
                "written_files": written,
                "failed_files": errors,
                "turns": turns,
            },
            evidence=[{"kind": "file", "path": p} for p in written],
        )

    # ------------------------------------------------------------------
    # Tool action execution
    # ------------------------------------------------------------------

    def _execute_action(
        self,
        action: Any,
        *,
        task: TaskRecord,
        fs_tool: Any,
        shell_tool: Any,
        written: list[str],
        errors: list[str],
    ) -> tuple[str, AgentResult | None]:
        """
        Execute one proposed tool action.

        Returns ``(result_text, stop_result)``. ``stop_result`` is
        non-None only when execution must PAUSE for a human approval
        (COR-003) — the caller returns it immediately, the orchestrator
        parks the task in AWAITING_APPROVAL, and no turn/retry budget is
        consumed while the approval is pending.
        """
        try:
            if action.action_type == "filesystem.write":
                path = Path(action.arguments.get("path", ""))
                content = action.arguments.get("content", "")
                res = fs_tool.write(path, content, task_id=task.task_id)
                if res.success:
                    written.append(str(path))
                    return res.output, None
                errors.append(res.error)
                return f"Error: {res.error}", self._approval_stop(task, res)

            if action.action_type == "filesystem.read":
                path = Path(action.arguments.get("path", ""))
                res = fs_tool.read(path, task_id=task.task_id)
                if res.success:
                    return res.output, None
                return f"Error: {res.error}", self._approval_stop(task, res)

            if action.action_type == "shell.run":
                cmd = action.arguments.get("command", "")
                res = shell_tool.run(cmd, task_id=task.task_id)
                if res.success:
                    return res.output, None
                return f"Error: {res.error}", self._approval_stop(task, res)

            return f"Unknown action type: {action.action_type}", None
        except Exception as e:  # noqa: BLE001
            return f"Tool execution failed: {e}", None

    def _approval_stop(self, task: TaskRecord, res: Any) -> AgentResult | None:
        """Return an awaiting-approval result when ``res`` is approval-paused."""
        if not getattr(res, "requires_human_approval", False):
            return None
        request_id = getattr(res, "approval_request_id", "")
        self._emit("agent.builder.awaiting_approval", {
            "task_id": task.task_id,
            "approval_request_id": request_id,
            "error": res.error,
        })
        return self._awaiting_approval(task, res.error, request_id=request_id)
