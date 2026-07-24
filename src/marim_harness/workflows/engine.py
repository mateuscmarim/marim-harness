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
VM holds no external resources).

VM duration limit (empirically re-verified against pydantic-monty 0.0.18,
2026-07-11 — an earlier version of this comment claimed max_duration_secs
counts interpreter time only and does not tick while awaiting host
functions; that is false): the check only runs when the interpreter
regains control (so it can't preempt a call mid-flight, and a
gather()'d group is one control-return point checked only once every
member has resolved), but once it runs it accounts real wall-clock time,
including time spent awaiting agent()/log() host calls. So it doubles as
a soft wall-clock budget for the whole script, not just a runaway-loop
guard — so it serves as the run's whole-script wall-clock budget — see
`_effective_timeout`."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field

from pydantic_monty import (
    Monty,
    MontyRuntimeError,
    MontySyntaxError,
    MontyTypingError,
    ResourceLimits,
)

from ..runtime.deps import Deps
from ..tools.impl import fs
from ..workspace.agents import cap_subagent_output
from .errors import WorkflowCancelled, WorkflowResultError
from .schema import check_valid_schema, shape_result, validate_report

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECS = 1800.0
MAX_RESULT_CHARS = 24_000
# One extra attempt when a schema'd report fails validation.
_SCHEMA_RETRIES = 1
# How long to wait for the VM to finish on its own after an abort.
_DRAIN_SECS = 3.0
_VM_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
# Per-call default when run() is not given a timeout: short enough that an
# ordinary sweep fails fast, long enough for a handful of real agent()
# calls. A caller doing genuinely long work (a multi-researcher deep-research
# run) requests more via timeout_secs, up to the engine's configured ceiling.
DEFAULT_RUN_TIMEOUT_SECS = 300.0


# Bound on the retained print() tail. Scripts shouldn't print at all (log()
# is the progress channel; the last expression is the result), so this exists
# only for the recovery path below -- big enough to hold any real payload a
# script mistakenly printed, small enough that a print-happy loop can't grow
# the engine's memory unbounded.
_PRINT_TAIL_CHARS = 100_000


@dataclass
class _RunState:
    """Per-run mutable state shared by the host functions."""

    tool_call_id: str
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    children: set[asyncio.Task] = field(default_factory=set)
    seq: int = 0


class _PrintTail:
    """Bounded capture of the script's print() output (stdout and stderr
    merged, oldest chunks dropped past the cap). A workflow script that ends
    on ``print(result)`` instead of a bare ``result`` expression evaluates to
    None -- but the payload it computed (possibly through several expensive
    sub-agent runs) went through print, so this tail lets ``_shape`` return
    that output with a corrective note instead of an error that forces the
    model to re-run the whole workflow."""

    def __init__(self, cap: int = _PRINT_TAIL_CHARS):
        self._cap = cap
        self._chunks: deque[str] = deque()
        self._size = 0

    def append(self, stream: str, text: str) -> None:
        self._chunks.append(text)
        self._size += len(text)
        while self._size > self._cap and self._chunks:
            self._size -= len(self._chunks.popleft())

    def text(self) -> str:
        return "".join(self._chunks)


# The host surface declared to static validation. Monty.type_check() knows
# nothing about run_async's external_functions/inputs wiring, so without these
# declarations every real script would be rejected for using agent/log/args.
# MUST stay in sync with _host_table()'s signatures and run()'s inputs list.
# The prefix does not shift diagnostics: reported line numbers still point at
# the user script's own lines (verified against pydantic-monty 0.0.18).
#
# agent() is declared with overloads so its return is PRECISE where the truth
# is knowable statically: no schema= -> the report is a plain str, so indexing
# it with a string key (a bug that burned a real 27k-token spawn) is rejected
# before anything runs; with schema= the parsed shape is only known at run
# time, so that arm stays Any and schema'd indexing passes. When schema's
# staticness is itself ambiguous (a `dict | None` variable), Monty resolves
# the union of both arms — permissive, the right failure direction for a
# validation gate.
_VALIDATION_PREFIX = (
    "from typing import Any, overload\n"
    "@overload\n"
    "async def agent(task, *, type='general', model=None, schema: None = None, "
    "max_output_chars=None, isolation=None) -> str: ...\n"
    "@overload\n"
    "async def agent(task, *, type='general', model=None, schema: dict, "
    "max_output_chars=None, isolation=None) -> Any: ...\n"
    "def log(message) -> None: ...\n"
    "args: Any = None\n"
)


def _script_title(script: str) -> str:
    """A short human label for the run's card: the script's first comment
    line when it opens with one (models usually title their scripts), else a
    line count. Pure; unit-tested directly."""
    for line in script.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            text = s.lstrip("#").strip()
            if text:
                return text
        break
    return f"workflow script ({len(script.splitlines())} lines)"


def _effective_timeout(requested: float | None, ceiling: float) -> float:
    """The wall-clock budget one run actually gets: the caller's request
    (defaulting to DEFAULT_RUN_TIMEOUT_SECS) clamped to the engine's
    configured ceiling. Pure; unit-tested directly."""
    return min(requested if requested is not None else DEFAULT_RUN_TIMEOUT_SECS, ceiling)


class WorkflowEngine:
    """Runs workflow scripts. One engine per harness; state is per-run."""

    def __init__(self, deps: Deps, spawn, *, timeout_secs: float = DEFAULT_TIMEOUT_SECS):
        self.deps = deps
        self._spawn = spawn
        # The CEILING on any one run's wall-clock budget. Each run() call may
        # request its own timeout_secs (deep research legitimately needs 30
        # minutes); the request is clamped to this, and omitted requests get
        # DEFAULT_RUN_TIMEOUT_SECS. See _effective_timeout.
        self._timeout = timeout_secs

    async def run(self, script: str, args: object, tool_call_id: str,
                  timeout_secs: float | None = None) -> str:
        try:
            monty = Monty(script, inputs=["args"], script_name="workflow.py")
        except MontySyntaxError as exc:
            return f"Workflow script failed to parse: {exc}"
        # Static validation before anything runs: catches unresolved names —
        # the classic model-authored-script bug — and real type errors, with
        # line/column diagnostics, at zero sub-agent cost. Host-return values
        # type as Any, so ordinary dynamic scripts pass untouched.
        try:
            monty.type_check(_VALIDATION_PREFIX)
        except MontyTypingError as exc:
            return f"Workflow script failed validation (nothing was executed):\n{exc}"
        except Exception as exc:  # noqa: BLE001
            # type_check can raise RuntimeError when its own infrastructure
            # fails. Validation is a cheap pre-flight, not the authority —
            # never let its breakage block a script the interpreter could run.
            logger.warning("workflow script validation errored; skipping",
                           exc_info=True)
        # Announce the run only after a successful parse + validation: both
        # failures are returned above with no run to track, so no card is ever
        # claimed for them and _announce_done below fires exactly once per
        # announced run.
        self._announce_start(tool_call_id, _script_title(script))
        state = _RunState(tool_call_id=tool_call_id)
        effective = _effective_timeout(timeout_secs, self._timeout)
        vm_limits: ResourceLimits = {
            # The VM's duration cap IS the run's effective timeout: it is the
            # only thing that can stop a non-yielding compute loop (the outer
            # asyncio.wait below can't preempt one — Monty holds the GIL and
            # direct cancellation is unsafe; see the module docstring), and
            # since it also counts host-await wall time it doubles as the
            # whole-script budget. Trade-off accepted with the per-call knob:
            # a spin loop can now burn up to the requested duration instead
            # of a fixed few minutes, bounded by the configured ceiling.
            "max_duration_secs": effective,
            "max_memory": _VM_MEMORY_LIMIT_BYTES,
        }
        prints = _PrintTail()
        vm = asyncio.ensure_future(
            monty.run_async(
                inputs={"args": args},
                limits=vm_limits,
                external_functions=self._host_table(state),
                print_callback=prints.append,
            )
        )
        # asyncio.wait (not wait_for + shield): both leave the VM task
        # uncancelled on timeout/cancel, but on Python 3.14 an abandoned
        # shield attaches a callback that reports the task's eventual
        # exception to the loop's exception handler even after it has been
        # retrieved -- and the deliberate wind-down below ENDS with the VM
        # raising (WorkflowCancelled surfaces as MontyRuntimeError), so every
        # abort would log a spurious "exception in shielded future".
        try:
            done, _ = await asyncio.wait({vm}, timeout=effective)
        except asyncio.CancelledError:
            # The turn was aborted. Wind the VM down through its host
            # functions (never a direct cancel — see module docstring), then
            # let the cancellation propagate so the turn's resumability
            # invariants hold.
            await self._abort_and_drain(state, vm)
            self._announce_done(tool_call_id, "workflow aborted", failed=True)
            raise
        if not done:
            await self._abort_and_drain(state, vm)
            outcome = (f"Workflow timed out after {effective:.0f}s; "
                       "in-flight sub-agents were cancelled.")
            self._announce_done(tool_call_id, outcome, failed=True)
            return outcome
        try:
            value = vm.result()
        except MontyRuntimeError as exc:
            # Scripts are model-authored, so the tool return is the model's
            # only debugging surface: lead with the exception summary (the
            # card headline), then the full traceback -- display() renders
            # CPython-style file/line frames with source excerpts, and Monty
            # collapses repeated frames so deep recursion stays bounded.
            outcome = f"Workflow script raised: {exc}\n\n{exc.display()}"
            self._announce_done(tool_call_id, outcome, failed=True)
            return outcome
        shaped = self._shape(value, tool_call_id, prints.text())
        self._announce_done(tool_call_id, shaped, failed=False)
        return shaped

    # -- host functions -----------------------------------------------------

    def _host_table(self, state: _RunState) -> dict:
        # Called from run(), on the loop. Monty marshals ASYNC host functions
        # (agent) back onto this loop before they run, but SYNC ones (log)
        # execute directly on its interpreter thread, where no loop is
        # running. UI callbacks assume the app's event loop -- the TUI's log
        # handler mounts widgets -- so log() must hand the line back to the
        # loop instead of calling into the UI from a loop-less thread: doing
        # so raised RuntimeError("no running event loop") INTO the script,
        # failing a workflow whose agent() work had already succeeded. Bonus
        # of the threadsafe hop: a raising UI callback now lands in the
        # loop's exception handler (a render bug, per the spec's posture)
        # instead of killing the run.
        loop = asyncio.get_running_loop()

        async def agent(task, *, type="general", model=None, schema=None,
                        max_output_chars=None, isolation=None):
            return await self._agent_call(
                state, str(task), type=str(type), model=model, schema=schema,
                max_output_chars=max_output_chars, isolation=isolation,
            )

        def log(message):
            loop.call_soon_threadsafe(self._log, state.tool_call_id, str(message))

        return {"agent": agent, "log": log}

    async def _agent_call(self, state: _RunState, task: str, *, type: str,
                          model, schema, max_output_chars, isolation):
        if schema is not None:
            check_valid_schema(schema)
        # Enforcement rides the spawn seam (runner-side structured output,
        # with the prompt contract as the runner's own claude-cli/non-object
        # fallback); validate_report below stays as defense in depth — on the
        # native path the JSON round-trips trivially, on the fallback path it
        # does exactly the work it did when the engine owned the contract.
        report = await self._spawn_child(
            state, type, task, max_output_chars, model, isolation, schema,
        )
        if schema is None:
            return report
        data, err = validate_report(report, schema)
        for _ in range(_SCHEMA_RETRIES):
            if err is None:
                return data
            retry_task = (
                task
                + f"\n\nA previous attempt failed validation: {err}. "
                  "Respond again with ONLY the corrected JSON."
            )
            report = await self._spawn_child(
                state, type, retry_task, max_output_chars, model, isolation, schema,
            )
            data, err = validate_report(report, schema)
        if err is None:
            return data
        raise WorkflowResultError(
            f"agent() output failed schema validation after a retry: {err}"
        )

    async def _spawn_child(self, state: _RunState, type: str, task: str,
                           max_output_chars, model, isolation,
                           output_schema: dict | None = None) -> str:
        if state.abort.is_set():
            raise WorkflowCancelled("workflow aborted")
        state.seq += 1
        stream_id = f"{state.tool_call_id}::wf{state.seq}" if state.tool_call_id else ""
        announce = getattr(self.deps.ui, "on_workflow_spawn", None)
        if announce is not None and stream_id:
            await announce(stream_id, type, task, state.tool_call_id)
        # Re-check after the await: an abort landing while announce() was in
        # flight must not let the child slip through. The child task is
        # created and registered in state.children only below, so
        # _abort_and_drain (which cancels only already-registered children)
        # would otherwise miss it entirely -- an uncancelled, unmonitored
        # spawn past workflow abandonment.
        if state.abort.is_set():
            raise WorkflowCancelled("workflow aborted")
        child = asyncio.ensure_future(self._spawn(
            type, task, stream_id, None, max_output_chars, model, isolation,
            self.deps.subagent_depth, output_schema=output_schema,
        ))
        state.children.add(child)
        child.add_done_callback(state.children.discard)
        try:
            report = await child
        except asyncio.CancelledError:
            # The abort path cancelled this child; surface a catchable
            # script-level exception instead of a bare cancel so the VM winds
            # down through normal exception flow.
            raise WorkflowCancelled("workflow aborted") from None
        done = getattr(self.deps.ui, "on_workflow_spawn_done", None)
        if done is not None and stream_id:
            done(stream_id, report)
        return report

    def _log(self, tool_call_id: str, message: str) -> None:
        logger.debug("workflow log: %s", message)
        cb = getattr(self.deps.ui, "on_workflow_log", None)
        if cb is not None:
            cb(tool_call_id, message)

    def _announce_start(self, tool_call_id: str, title: str) -> None:
        cb = getattr(self.deps.ui, "on_workflow_start", None)
        if cb is None:
            return
        # Guard the render callback exactly as log() is guarded (its threadsafe
        # hop deflects a raise into the loop's exception handler): a raising UI
        # callback is a render bug, not a script failure, and must not kill a
        # workflow whose real work succeeded.
        try:
            cb(tool_call_id, title)
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow start callback raised; ignoring: %s", exc, exc_info=True)

    def _announce_done(self, tool_call_id: str, outcome: str, *, failed: bool) -> None:
        cb = getattr(self.deps.ui, "on_workflow_done", None)
        if cb is None:
            return
        # Same guard as _announce_start. On the SUCCESS path this also protects
        # the already-computed result: a raising done-callback must not lose the
        # `shaped` value run() is about to return.
        try:
            cb(tool_call_id, outcome, failed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("workflow done callback raised; ignoring: %s", exc, exc_info=True)

    # -- teardown & result shaping -------------------------------------------

    async def _abort_and_drain(self, state: _RunState, vm: asyncio.Future) -> None:
        """Wind the VM down without cancelling it: flag the abort so new
        agent() calls refuse, cancel in-flight children (their awaiters
        re-raise WorkflowCancelled into the script), then give the VM a short
        deadline to finish on its own before abandoning it."""
        state.abort.set()
        for child in list(state.children):
            child.cancel()
        # Retrieve the VM's eventual exception whenever it lands: the
        # expected wind-down IS the script raising WorkflowCancelled (a
        # MontyRuntimeError once Monty wraps it), and an unretrieved task
        # exception is reported to the loop's exception handler at GC --
        # which anyio's pytest runner escalates to a test failure. The
        # callback covers both the drained and the abandoned branch below.
        vm.add_done_callback(lambda f: f.cancelled() or f.exception())
        # asyncio.wait, not wait_for+shield -- see the comment in run().
        await asyncio.wait({vm}, timeout=_DRAIN_SECS)
        if not vm.done():
            # Never cancel or await an abandoned VM (see module docstring).
            logger.warning("workflow VM did not drain within %.1fs; abandoned",
                           _DRAIN_SECS)

    def _shape(self, value: object, tool_call_id: str, printed: str = "") -> str:
        rel = f".marim/workflow-output/{tool_call_id or 'workflow'}.json"
        # A None final value with printed output is almost always a script
        # that ended on print(result) instead of a bare `result` expression.
        # The payload -- possibly the product of several expensive sub-agent
        # runs -- already went through print, so return it (with a corrective
        # note) rather than an error that would make the model re-run the
        # whole workflow just to fix its last line.
        if value is None and printed.strip():
            text, spill = cap_subagent_output(printed, MAX_RESULT_CHARS, rel)
            if spill is not None:
                fs.write_file(self.deps.workspace.root, rel, spill)
            return (
                "Note: the workflow's final expression was None -- the tool "
                "returns the script's LAST EXPRESSION, so end the script with "
                "the value itself (not print(value)) next time. Returning the "
                "script's printed output instead:\n\n" + text
            )
        try:
            text, spill = shape_result(value, MAX_RESULT_CHARS, rel)
        except WorkflowResultError as exc:
            return f"Workflow completed but its result was unusable: {exc}"
        if spill is not None:
            fs.write_file(self.deps.workspace.root, rel, spill)
        return text
