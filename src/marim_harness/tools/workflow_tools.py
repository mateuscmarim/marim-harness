"""The run_workflow tool: thin ctx.deps unwrapping over the workflow engine.

The docstring below is the model-facing product documentation for workflow
scripts — it is the ONLY place the model learns the sandbox's dialect and
host API from, so treat every sentence as UI copy."""

from __future__ import annotations

import math

from pydantic import JsonValue
from pydantic_ai import RunContext

from ..runtime.deps import Deps

_UNAVAILABLE = (
    "Workflows are unavailable in this session: the pydantic-monty sandbox "
    "is not installed (install the marim-harness[workflows] extra) or "
    "MARIM_WORKFLOWS is off. Use spawn_agent for fan-out instead."
)


def _bad_timeout(timeout_secs: float | None) -> str | None:
    """A correctable-error message when the requested timeout is unusable,
    else None. Pure; keeps run_workflow itself thin."""
    if timeout_secs is None:
        return None
    if not math.isfinite(timeout_secs) or timeout_secs <= 0:
        return (f"Invalid timeout_secs={timeout_secs!r}: it must be a positive "
                "number of seconds. Omit it for the default, or request what "
                "the work needs (long multi-agent runs may use e.g. 1800).")
    return None


async def run_workflow(
    ctx: RunContext[Deps], script: str, args: JsonValue = None,
    timeout_secs: float | None = None,
) -> str:
    """Run a Python orchestration script that spawns sub-agents with loops,
    conditionals, and parallel fan-out — deterministic control flow the
    spawn_agent tool can't express. Use it when coordinating several
    sub-agents whose number, order, or inputs depend on data: review sweeps
    over N dimensions, retry-until-valid loops, map-reduce over file lists.
    For one or two independent spawns, spawn_agent alone is simpler.

    The script runs in a sandboxed Python subset (Monty): no classes, no
    match statements, no imports beyond asyncio/json/re/datetime, no
    filesystem or network access. Only these names are available:

    - `await agent(task, *, type="general", model=None, schema=None,
      max_output_chars=None, isolation=None)` — spawn a sub-agent and return
      its report. Parameters mean exactly what they mean on spawn_agent
      (`type` is `explore`/`general`/a custom agent name). With `schema` (a
      JSON Schema dict) the report is validated and returned as parsed data
      (retried once on mismatch) — use it whenever a later step consumes the
      result. Failures raise; wrap calls in try/except to degrade gracefully.
    - `log(message)` — one short progress line to the user. Not awaited.
    - `args` — the value you passed in this tool call's `args` parameter
      (use it instead of interpolating big data into the script text).
      For data that already lives in the workspace — diffs, file bodies,
      command output — don't pass the content at all: put a path or git ref
      in the task and let the sub-agent read it with its own tools. Reports
      from earlier agent() calls are the exception: interpolating those into
      a later task string is free, it never re-enters your context.
    - `asyncio.gather(...)` — run agent() calls concurrently. This is the
      fan-out primitive; concurrency is capped downstream, so gather as wide
      as the work needs.

    The tool's `timeout_secs` parameter is the run's wall-clock budget.
    Omit it for quick sweeps (default 300s). Request what the work needs
    when spawning agents that each take minutes — e.g. 1800 for a
    multi-researcher deep-research run. Requests are clamped to a
    harness-configured ceiling.

    The script's LAST EXPRESSION is this tool's result — end with plain data
    (dict/list/str), JSON-serialized for you and spilled to a workspace file
    if very large. Keep intermediate results in variables; return only what
    you need.

    Common mistakes (each has burned a real run):
    - Ending with print(result): print returns None, so the tool result is
      None. End with the bare value — `result`, not `print(result)`.
    - Wrapping work in asyncio.run(...): the script body already runs in an
      event loop; `await` directly at top level.
    - Reporting through log(): log() is the progress channel only. The
      result must be the final expression.
    - Pasting large content (a diff, a file body) into a task string:
      sub-agents read the workspace themselves — pass the path/ref instead.

    Example — parallel review sweep:

        import asyncio

        SCHEMA = {"type": "object",
                  "properties": {"findings": {"type": "array"}},
                  "required": ["findings"]}

        async def review(dim):
            r = await agent("Review the diff for " + dim + " issues",
                            type="explore", schema=SCHEMA)
            return r["findings"]

        per_dim = await asyncio.gather(*[review(d) for d in
                                         ["bugs", "performance", "style"]])
        log("reviewed " + str(len(per_dim)) + " dimensions")
        {"findings": [f for fs in per_dim for f in fs]}

    Scripts are bounded by timeout_secs and aborted cleanly on interrupt. An
    infinite compute loop is killed by the sandbox. If the script fails to
    parse, fix the reported error and call again."""
    runner = ctx.deps.services.run_workflow
    if runner is None:
        return _UNAVAILABLE
    err = _bad_timeout(timeout_secs)
    if err is not None:
        return err
    return await runner(script, args, ctx.tool_call_id or "", timeout_secs)
