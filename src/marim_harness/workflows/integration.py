"""Marim's UI/persistence boundary around upstream workflow execution.

Upstream owns every sandbox and pending dispatch. This adapter never drives
Monty or retries a child. Import only behind the optional dependency gate.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset
from pydantic_ai_harness import DynamicWorkflow
from pydantic_core import PydanticSerializationError, to_json

from ..runtime.backend_jobs import drain_task
from ..runtime.deps import Deps, SubAgentRunner
from ..workspace import cap_subagent_output, spill_target, write_spill
from ..workspace.agents import AgentDef
from .agents import RunnerWorkflowAgent
from .catalog import WorkflowBinding, build_workflow_bindings
from .invocation import WorkflowInvocation, current_workflow

logger = logging.getLogger(__name__)
MAX_RESULT_CHARS = 24_000
COMPUTE_TIMEOUT_SECS = 30.0
UNAVAILABLE = (
    "Workflows are unavailable in this session. Install marim-harness[workflows] "
    "and enable MARIM_WORKFLOWS, or use spawn_agent instead."
)


def _notify(callback, *args) -> None:
    if callback is not None:
        try:
            callback(*args)
        except Exception:
            # Rendering must never discard paid-for work and make a child rerun.
            logger.warning("workflow lifecycle callback failed", exc_info=True)


@dataclass
class WorkflowIntegration:
    deps: Deps
    spawn: SubAgentRunner
    roles: Callable[[], Sequence[AgentDef]]
    bindings: Sequence[WorkflowBinding] = ()
    timeout_secs: float = 1800.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_secs) or self.timeout_secs <= 0:
            raise ValueError("workflow_timeout_secs must be a positive finite number")

    def toolset(self) -> AbstractToolset[Deps]:
        # Freeze a catalog per Agent.run, retaining fresh discovery across turns.
        roles = self.roles()
        names = {role.qualified_name for role in roles}
        for role in ("researcher", "explore"):
            if role not in names:
                logger.debug("workflow typed alias unavailable: missing role=%s", role)
        bindings = build_workflow_bindings(roles, self.bindings)
        descriptions = {role.qualified_name: role.description for role in roles}
        agents = [
            RunnerWorkflowAgent(binding, descriptions.get(binding.agent_type, ""), self.spawn)
            for binding in bindings
        ]
        workflow = DynamicWorkflow(
            agents=agents,
            forward_usage=False,  # The runner already banks native/CLI spend.
            inherit_model=False,  # The runner resolves live tiers/role models.
            # Monty's first compute slice can block the event loop before the
            # wall timer gets control, so it must obey shorter host deadlines.
            resource_limits={"max_duration_secs": min(COMPUTE_TIMEOUT_SECS, self.timeout_secs)},
        )
        return _WorkflowExecution(workflow.get_toolset(), self)

    def cap_result(self, text: str, call_id: str) -> str:
        getter = self.deps.services.get_scratchpad
        scratchpad = getter() if getter is not None else None
        path, ref = spill_target(
            scratchpad, self.deps.workspace.root, "workflow-output", f"{call_id or 'workflow'}.json"
        )
        capped, spill = cap_subagent_output(text, MAX_RESULT_CHARS, path)
        if spill is not None:
            write_spill(self.deps.workspace.root, path, ref, spill)
        return capped


@dataclass
class _WorkflowExecution(WrapperToolset[Deps]):
    integration: WorkflowIntegration

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Deps], tool: ToolsetTool[Deps]
    ) -> str:
        integration = self.integration
        if not integration.enabled:
            return UNAVAILABLE
        if ctx.deps.subagent_depth:
            return "run_workflow is available only to the main agent."
        if "code" not in tool_args or "script" in tool_args:
            return (
                "Workflow API changed: use run_workflow(code=...) with named role(task=...) calls."
            )
        state = WorkflowInvocation(ctx.tool_call_id or "", ctx.deps)
        token = current_workflow.set(state)
        started = time.monotonic()
        outcome = "workflow aborted"
        failed = True
        logger.debug("workflow started: call_id=%s", state.tool_call_id)
        try:
            _notify(ctx.deps.ui.on_workflow_start, state.tool_call_id, "Workflow")
            outcome, failed = await self._execute(name, tool_args, ctx, tool)
            return outcome
        except ModelRetry as exc:
            outcome = integration.cap_result(str(exc), state.tool_call_id)
            raise ModelRetry(outcome) from exc
        finally:
            state.aborted = True
            current_workflow.reset(token)
            _notify(ctx.deps.ui.on_workflow_done, state.tool_call_id, outcome, failed)
            logger.debug(
                "workflow finished: call_id=%s failed=%s duration=%.3fs",
                state.tool_call_id,
                failed,
                time.monotonic() - started,
            )

    async def _execute(self, name, tool_args, ctx, tool) -> tuple[str, bool]:
        task = asyncio.create_task(self.wrapped.call_tool(name, tool_args, ctx, tool))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.integration.timeout_secs)
        except asyncio.CancelledError:
            await _cancel_and_drain(task)
            raise
        if not done:
            await _cancel_and_drain(task)
            return (
                f"Workflow timed out after {self.integration.timeout_secs:g}s; "
                "in-flight sub-agents were cancelled.",
                True,
            )
        value = task.result()
        try:
            text = to_json(value, indent=2).decode()
        except PydanticSerializationError as exc:
            raise ModelRetry(
                "Workflow completed but its result cannot be serialized. Return JSON-compatible "
                "data; reuse completed worker results instead of replaying their work."
            ) from exc
        failed = _is_budget_error(value)
        return self.integration.cap_result(text, ctx.tool_call_id or ""), failed


def _is_budget_error(value: object) -> bool:
    # Harness 0.31 returns this terminal envelope on exhausted call budgets.
    # An ordinary user report with an `error` field is still successful data.
    # Contract-tested via the public toolset; no private upstream state access.
    return (
        isinstance(value, dict)
        and set(value) == {"error", "last_error", "completed"}
        and isinstance(value["error"], str)
        and isinstance(value["last_error"], str)
        and isinstance(value["completed"], list)
    )


async def _settle(task: asyncio.Task) -> None:
    # Retrieve cancellation/failure from the abandoned call; the initiating
    # interrupt/timeout is the authoritative outcome, not a cleanup exception.
    await asyncio.gather(task, return_exceptions=True)


async def _cancel_and_drain(task: asyncio.Task) -> None:
    state = current_workflow.get()
    if state is not None:
        state.aborted = True
    # Upstream owns pending children, but raw repeated Task.cancel() can pierce
    # its cleanup shield (verified on Harness 0.31 / Monty 0.0.23). Cancel the
    # owner once, then reuse Marim's session-ownership drain: further Ctrl-C must
    # not release the session while workers still write or persist transcripts.
    task.cancel()
    with anyio.CancelScope(shield=True):
        await drain_task(asyncio.create_task(_settle(task)))
