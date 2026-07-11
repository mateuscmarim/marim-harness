"""Execute a model-authored workflow script in a Monty sandbox.

The script orchestrates sub-agents through host functions the engine
registers on the VM: ``agent()`` delegates to the injected spawn callable
(the same ``SubagentRunner.run`` seam spawn_agent uses), ``log()`` surfaces
progress. Parallelism is plain ``asyncio.gather`` inside the sandbox —
Monty forwards concurrent host calls to the real event loop, and the
runner's own semaphore bounds provider pressure.

Cancellation invariant (verified against pydantic-monty 0.0.18): cancelling
the VM's run_async task crashes the interpreter with a GIL fatal error, so
this engine NEVER cancels the VM task. Aborts flow through the host
functions instead — the abort event makes every in-flight and future
agent() call raise WorkflowCancelled INTO the script; an uncaught host
exception ends the run promptly, and the engine drains the VM with a short
deadline before abandoning it (safe: once its host calls have raised, the
VM holds no external resources)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from pydantic_monty import Monty, MontyRuntimeError, MontySyntaxError, ResourceLimits

from ..runtime.deps import Deps
from ..tools.impl import fs
from .errors import WorkflowCancelled, WorkflowResultError
from .schema import output_contract, shape_result, validate_report

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECS = 1800.0
MAX_RESULT_CHARS = 24_000
# One extra attempt when a schema'd report fails validation.
_SCHEMA_RETRIES = 1
# How long to wait for the VM to finish on its own after an abort.
_DRAIN_SECS = 3.0
# VM compute limits. max_duration_secs counts interpreter time only (it does
# NOT tick while awaiting host functions — spike-verified), so it bounds a
# runaway `while True:` without limiting how long spawns may take.
_VM_LIMITS: ResourceLimits = {"max_duration_secs": 30.0, "max_memory": 256 * 1024 * 1024}


@dataclass
class _RunState:
    """Per-run mutable state shared by the host functions."""

    tool_call_id: str
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    children: set[asyncio.Task] = field(default_factory=set)
    seq: int = 0


class WorkflowEngine:
    """Runs workflow scripts. One engine per harness; state is per-run."""

    def __init__(self, deps: Deps, spawn, *, timeout_secs: float = DEFAULT_TIMEOUT_SECS):
        self.deps = deps
        self._spawn = spawn
        self._timeout = timeout_secs

    async def run(self, script: str, args: object, tool_call_id: str) -> str:
        try:
            monty = Monty(script, inputs=["args"], script_name="workflow.py")
        except MontySyntaxError as exc:
            return f"Workflow script failed to parse: {exc}"
        state = _RunState(tool_call_id=tool_call_id)
        vm = asyncio.ensure_future(
            monty.run_async(
                inputs={"args": args},
                limits=_VM_LIMITS,
                external_functions=self._host_table(state),
            )
        )
        try:
            value = await asyncio.wait_for(asyncio.shield(vm), self._timeout)
        except asyncio.TimeoutError:
            await self._abort_and_drain(state, vm)
            return (f"Workflow timed out after {self._timeout:.0f}s; "
                    "in-flight sub-agents were cancelled.")
        except asyncio.CancelledError:
            # The turn was aborted. Wind the VM down through its host
            # functions (never a direct cancel — see module docstring), then
            # let the cancellation propagate so the turn's resumability
            # invariants hold.
            await self._abort_and_drain(state, vm)
            raise
        except MontyRuntimeError as exc:
            return f"Workflow script raised: {exc}"
        return self._shape(value, tool_call_id)

    # -- host functions -----------------------------------------------------

    def _host_table(self, state: _RunState) -> dict:
        async def agent(task, *, type="general", model=None, schema=None,
                        max_output_chars=None, isolation=None):
            return await self._agent_call(
                state, str(task), type=str(type), model=model, schema=schema,
                max_output_chars=max_output_chars, isolation=isolation,
            )

        def log(message):
            self._log(str(message))

        return {"agent": agent, "log": log}

    async def _agent_call(self, state: _RunState, task: str, *, type: str,
                          model, schema, max_output_chars, isolation):
        report = await self._spawn_child(
            state, type, task + (output_contract(schema) if schema else ""),
            max_output_chars, model, isolation,
        )
        if schema is None:
            return report
        data, err = validate_report(report, schema)
        for _ in range(_SCHEMA_RETRIES):
            if err is None:
                return data
            retry_task = (
                task + output_contract(schema)
                + f"\n\nA previous attempt failed validation: {err}. "
                  "Respond again with ONLY the corrected JSON."
            )
            report = await self._spawn_child(
                state, type, retry_task, max_output_chars, model, isolation,
            )
            data, err = validate_report(report, schema)
        if err is None:
            return data
        raise WorkflowResultError(
            f"agent() output failed schema validation after a retry: {err}"
        )

    async def _spawn_child(self, state: _RunState, type: str, task: str,
                           max_output_chars, model, isolation) -> str:
        if state.abort.is_set():
            raise WorkflowCancelled("workflow aborted")
        state.seq += 1
        stream_id = f"{state.tool_call_id}::wf{state.seq}" if state.tool_call_id else ""
        announce = getattr(self.deps.ui, "on_workflow_spawn", None)
        if announce is not None and stream_id:
            await announce(stream_id, type, task, state.tool_call_id)
        child = asyncio.ensure_future(self._spawn(
            type, task, stream_id, None, max_output_chars, model, isolation,
            self.deps.subagent_depth,
        ))
        state.children.add(child)
        child.add_done_callback(state.children.discard)
        try:
            return await child
        except asyncio.CancelledError:
            # The abort path cancelled this child; surface a catchable
            # script-level exception instead of a bare cancel so the VM winds
            # down through normal exception flow.
            raise WorkflowCancelled("workflow aborted") from None

    def _log(self, message: str) -> None:
        logger.debug("workflow log: %s", message)
        cb = getattr(self.deps.ui, "on_workflow_log", None)
        if cb is not None:
            cb(message)

    # -- teardown & result shaping -------------------------------------------

    async def _abort_and_drain(self, state: _RunState, vm: asyncio.Future) -> None:
        """Wind the VM down without cancelling it: flag the abort so new
        agent() calls refuse, cancel in-flight children (their awaiters
        re-raise WorkflowCancelled into the script), then give the VM a short
        deadline to finish on its own before abandoning it."""
        state.abort.set()
        for child in list(state.children):
            child.cancel()
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(vm), _DRAIN_SECS)
        if not vm.done():
            logger.warning("workflow VM did not drain within %.1fs; abandoned",
                           _DRAIN_SECS)

    def _shape(self, value: object, tool_call_id: str) -> str:
        rel = f".marim/workflow-output/{tool_call_id or 'workflow'}.json"
        try:
            text, spill = shape_result(value, MAX_RESULT_CHARS, rel)
        except WorkflowResultError as exc:
            return f"Workflow completed but its result was unusable: {exc}"
        if spill is not None:
            fs.write_file(self.deps.workspace.root, rel, spill)
        return text
