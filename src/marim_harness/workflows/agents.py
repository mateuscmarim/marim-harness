"""Public upstream agent adapter; the existing runner owns all worker execution.

The descriptor supplies signatures only. In particular, this adapter must never
start its own model loop or forward parent usage/model settings: Marim's runner
already owns accounting, model tiers, permission grants, and CLI lifecycles.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic_ai import Agent, StructuredDict
from pydantic_ai.agent import WrapperAgent
from pydantic_ai.run import AgentRunResult

from ..runtime.deps import SubAgentRunner
from .catalog import WorkflowBinding
from .errors import WorkflowResultError
from .invocation import WorkflowInvocation, current_workflow
from .schema import check_valid_schema, validate_report

logger = logging.getLogger(__name__)


def _check_run_options(options: dict[str, Any], invocation: WorkflowInvocation) -> None:
    # DynamicWorkflow passes these four public arguments. A future upstream
    # feature must fail explicitly here instead of appearing to enforce a limit
    # or overriding the runner's configuration without actually doing so.
    for name, value in options.items():
        if name == "deps" and value is invocation.deps:
            continue
        if name in {"model", "usage", "usage_limits"} and value is None:
            continue
        raise ValueError(f"Runner-backed workflow agents do not support run option {name!r}")


async def _announce(
    invocation: WorkflowInvocation, binding: WorkflowBinding, task: str, stream: str
):
    callback = invocation.deps.ui.on_workflow_spawn
    if callback is not None:
        try:
            await callback(stream, binding.agent_type, task, invocation.tool_call_id)
        except Exception:
            logger.warning("Workflow child announcement failed: %s", stream, exc_info=True)


def _complete(invocation: WorkflowInvocation, stream: str, report: str) -> None:
    callback = invocation.deps.ui.on_workflow_spawn_done
    if callback is not None:
        try:
            callback(stream, report)
        except Exception:
            logger.warning("Workflow child completion callback failed: %s", stream, exc_info=True)


class RunnerWorkflowAgent(WrapperAgent[Any, Any]):
    """Describe one named agent and dispatch its task exactly once to the runner."""

    def __init__(self, binding: WorkflowBinding, description: str, spawn: SubAgentRunner):
        if binding.output_schema is not None:
            check_valid_schema(binding.output_schema)
        descriptor: Agent[Any, Any] = Agent(
            name=binding.name,
            description=description,
            output_type=StructuredDict(binding.output_schema) if binding.output_schema else str,
        )
        super().__init__(descriptor)
        self.binding = binding
        self._spawn = spawn

    async def run(self, user_prompt: Any = None, **kwargs: Any) -> AgentRunResult[Any]:
        invocation = current_workflow.get()
        if invocation is None:
            raise RuntimeError("Workflow agent called outside an active workflow")
        _check_run_options(kwargs, invocation)
        if not isinstance(user_prompt, str):
            raise ValueError("Workflow agent tasks must be strings")
        if invocation.aborted:
            raise asyncio.CancelledError("workflow aborted")
        invocation.seq += 1
        stream = f"{invocation.tool_call_id}::wf{invocation.seq}"
        report = "Workflow child cancelled"
        logger.debug("Workflow child started: %s (%s)", stream, self.binding.agent_type)
        try:
            await _announce(invocation, self.binding, user_prompt, stream)
            # Cancellation during an awaited renderer must never dispatch work
            # after the workflow's owner has abandoned the script.
            if invocation.aborted:
                raise asyncio.CancelledError("workflow aborted")
            report = await self._spawn(
                self.binding.agent_type,
                user_prompt,
                stream,
                None,
                None,
                None,
                self.binding.isolation,
                invocation.deps.subagent_depth,
                None,
                self.binding.output_schema,
                None,
            )
            output: object = report
            if self.binding.output_schema is not None:
                output, reason = validate_report(report, self.binding.output_schema)
                if reason is not None:
                    raise WorkflowResultError(f"Workflow agent {self.binding.name}: {reason}")
            return AgentRunResult(output=output)
        except Exception as exc:
            report = f"Workflow child failed: {type(exc).__name__}"
            logger.warning("Workflow child failed: %s (%s)", stream, type(exc).__name__)
            raise
        finally:
            _complete(invocation, stream, report)
            logger.debug("Workflow child finished: %s", stream)
