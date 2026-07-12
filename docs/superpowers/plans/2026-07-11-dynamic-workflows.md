# Dynamic Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A gated `run_workflow` tool: the model writes a Python orchestration script, executed in a pydantic-monty sandbox, whose `agent()` host function spawns sub-agents through the existing `SubagentRunner` — loops, conditionals, and `asyncio.gather` fan-out in one tool call.

**Architecture:** Three-way split per house convention — `workflows/schema.py` (pure: output contracts, report validation, result shaping), `workflows/engine.py` (effectful core: Monty execution, host functions, cancellation), `tools/workflow_tools.py` (thin `ctx.deps` unwrapping). The engine is reached through a new `HarnessServices.run_workflow` seam (mirroring `run_subagent`), built in `build_collaborators` only when `pydantic-monty` imports and `workflows_enabled` is on.

**Tech Stack:** pydantic-monty (Rust sandboxed Python interpreter), jsonschema, existing Pydantic AI / Textual stack.

**Spec:** `docs/superpowers/specs/2026-07-11-dynamic-workflows-design.md` (read it first — the Amended paragraphs are load-bearing).

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`.
- Python floor is 3.10 — no 3.11+ syntax (no `except*`, no `asyncio.timeout()`; use `asyncio.wait_for`, and catch `asyncio.TimeoutError`, which is NOT builtin `TimeoutError` on 3.10).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity ≤ 10 per function — extract named helpers rather than `# noqa`.
- CI order: ruff → pyright → pytest. Run all three before claiming a task done.
- New deps: `pydantic-monty>=0.0.18,<0.1` and `jsonschema>=4` — optional extra `workflows`, duplicated into `[dependency-groups] dev` so `uv sync` installs them for CI (same pattern as `serve`).
- Tests must never call a paid model. Everything runs against fakes / real Monty.
- Long why-comments around invariants are house style — write them where this plan includes them; do not strip them.
- **Monty spike facts (verified 2026-07-11, v0.0.18):** module name is `pydantic_monty`; construct `Monty(code, inputs=[...])`, run `await m.run_async(inputs=..., limits=..., external_functions=...)` (`run_monty_async` is deprecated); host exceptions ARE catchable inside the script; `ResourceLimits` is a TypedDict with keys `max_allocations`, `max_duration_secs`, `max_memory`, `gc_interval`, `max_recursion_depth`; `max_duration_secs` counts VM compute only (does NOT tick while awaiting a host function); **cancelling the `run_async` task directly crashes the interpreter (GIL fatal error) — the engine must NEVER cancel the VM task.**

---

### Task 1: Packaging + pure helpers (`workflows/schema.py`)

**Files:**
- Modify: `pyproject.toml` (optional extra + dev group)
- Create: `src/marim_harness/workflows/__init__.py`
- Create: `src/marim_harness/workflows/errors.py`
- Create: `src/marim_harness/workflows/schema.py`
- Test: `tests/test_workflow_schema.py`

**Interfaces:**
- Consumes: `cap_subagent_output(output: str, max_output_chars: int | None, spill_path: str) -> tuple[str, str | None]` from `marim_harness.workspace.agents`.
- Produces (Tasks 2–3 rely on these exact names):
  - `errors.WorkflowCancelled(Exception)`, `errors.WorkflowResultError(Exception)`
  - `schema.output_contract(schema: dict) -> str`
  - `schema.extract_json(report: str) -> object | None`
  - `schema.validate_report(report: str, schema: dict) -> tuple[object | None, str | None]`
  - `schema.shape_result(value: object, max_chars: int, spill_path: str) -> tuple[str, str | None]` (raises `WorkflowResultError`)

- [ ] **Step 1: Add the dependencies**

In `pyproject.toml`, after the `serve` extra in `[project.optional-dependencies]`:

```toml
# Dynamic workflows: model-authored orchestration scripts sandboxed in
# pydantic-monty. Bare installs leave the run_workflow tool returning an
# install hint (services.run_workflow stays None).
workflows = ["pydantic-monty>=0.0.18,<0.1", "jsonschema>=4"]
```

And duplicate both into `[dependency-groups] dev` (same comment style as the serve duplication: keeps `uv sync` + CI green while the runtime dep stays optional). Run `uv sync` and verify `uv run python -c "import pydantic_monty, jsonschema"` succeeds.

- [ ] **Step 2: Write the failing tests**

`tests/test_workflow_schema.py`:

```python
import pytest

from marim_harness.workflows.errors import WorkflowResultError
from marim_harness.workflows.schema import (
    extract_json,
    output_contract,
    shape_result,
    validate_report,
)

FINDINGS = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"],
}


def test_output_contract_embeds_the_schema_and_demands_bare_json():
    text = output_contract(FINDINGS)
    assert "ONLY a JSON object" in text
    assert '"findings"' in text


def test_extract_json_parses_a_bare_json_report():
    assert extract_json('{"findings": []}') == {"findings": []}


def test_extract_json_falls_back_to_a_fenced_block():
    report = 'Here you go:\n```json\n{"findings": ["a"]}\n```\nDone.'
    assert extract_json(report) == {"findings": ["a"]}


def test_extract_json_returns_none_for_prose():
    assert extract_json("I could not find anything.") is None


def test_validate_report_accepts_matching_json():
    data, err = validate_report('{"findings": ["x"]}', FINDINGS)
    assert err is None
    assert data == {"findings": ["x"]}


def test_validate_report_names_the_schema_violation():
    data, err = validate_report('{"findings": "not-a-list"}', FINDINGS)
    assert data is None
    assert err is not None and "not-a-list" in err


def test_validate_report_flags_non_json():
    data, err = validate_report("no json here", FINDINGS)
    assert data is None
    assert err is not None and "JSON" in err


def test_shape_result_serializes_plain_data():
    text, spill = shape_result({"n": 1}, 1000, "out.json")
    assert '"n": 1' in text
    assert spill is None


def test_shape_result_caps_oversized_output_with_a_pointer():
    big = {"rows": ["x" * 50] * 200}
    text, spill = shape_result(big, 200, ".marim/workflow-output/t.json")
    assert len(text) <= 200
    assert ".marim/workflow-output/t.json" in text
    assert spill is not None


def test_shape_result_rejects_non_serializable_values():
    with pytest.raises(WorkflowResultError):
        shape_result(object(), 1000, "out.json")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_workflow_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.workflows'`

- [ ] **Step 4: Implement**

`src/marim_harness/workflows/__init__.py`:

```python
"""Dynamic workflows: model-authored orchestration scripts sandboxed in Monty.

Deliberately re-exports nothing (matching the runtime package): the engine
imports pydantic_monty at module level, so importers must target submodules
directly and guard the import — see _build_workflow_engine in runtime/harness.py.
"""
```

`src/marim_harness/workflows/errors.py`:

```python
"""Leaf exception types shared by the pure helpers and the engine."""


class WorkflowCancelled(Exception):
    """Raised INTO the workflow script (via its host functions) when the turn
    is aborted. Scripts may catch it, but every subsequent agent() call raises
    it again, so a catching script still winds down promptly."""


class WorkflowResultError(Exception):
    """The workflow produced a value the model can't use (non-serializable
    final expression, or agent() output that failed schema validation after
    the retry). The message is written for the model."""
```

`src/marim_harness/workflows/schema.py`:

```python
"""Pure helpers for workflow scripts: schema output contracts, report
validation, and result shaping. No I/O — the engine owns all effects
(spawning, spill writes, UI callbacks)."""

from __future__ import annotations

import json
import re

import jsonschema

from ..workspace.agents import cap_subagent_output
from .errors import WorkflowResultError

_FENCED = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def output_contract(schema: dict) -> str:
    """The output-contract paragraph appended to a schema'd agent() task: the
    sub-agent must respond with ONLY a JSON object matching the schema."""
    return (
        "\n\nOutput contract: respond with ONLY a JSON object matching this "
        "JSON Schema — no prose before or after it:\n"
        + json.dumps(schema, indent=2)
    )


def extract_json(report: str) -> object | None:
    """The report's JSON payload: the whole report if it parses, else the
    first fenced block that does (models often fence despite instructions).
    None when nothing parses."""
    try:
        return json.loads(report)
    except ValueError:
        pass
    for match in _FENCED.finditer(report):
        try:
            return json.loads(match.group(1))
        except ValueError:
            continue
    return None


def validate_report(report: str, schema: dict) -> tuple[object | None, str | None]:
    """Validate a sub-agent report against the agent() schema. Returns
    (data, None) on success or (None, reason) with a model-readable reason."""
    data = extract_json(report)
    if data is None:
        return None, "the report is not valid JSON (nor contains a JSON code block)"
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        return None, f"the JSON does not match the schema: {exc.message}"
    return data, None


def shape_result(value: object, max_chars: int, spill_path: str) -> tuple[str, str | None]:
    """Serialize the script's final expression for the tool result, capping
    with the same lossless head-plus-pointer spill spawn reports use. Returns
    (text, spill): spill is the full serialization for the caller to persist
    at spill_path, or None when under budget."""
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowResultError(
            "the workflow's final expression is not JSON-serializable "
            f"({exc}); end the script with plain data — dicts, lists, "
            "strings, numbers"
        ) from exc
    return cap_subagent_output(text, max_chars, spill_path)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_workflow_schema.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add pyproject.toml uv.lock src/marim_harness/workflows tests/test_workflow_schema.py
git commit -m "feat(workflows): packaging + pure schema/result helpers"
```

---

### Task 2: Engine happy path (`workflows/engine.py`)

**Files:**
- Create: `src/marim_harness/workflows/engine.py`
- Test: `tests/test_workflow_engine.py`

**Interfaces:**
- Consumes: Task 1's helpers; `Deps` (`runtime/deps.py`) for `deps.ui.on_workflow_spawn` / `deps.ui.on_workflow_log` (added in Tasks 6/8 — the engine reads them with `getattr(..., None)` until then, see Step 3 note), `deps.subagent_depth`, `deps.workspace.root`; `fs.write_file(root, rel, content)` from `marim_harness.tools.impl.fs`; spawn callable with the `SubAgentRunner` shape from `runtime/deps.py`: `(type, task, stream_id, mcp_names, max_output_chars, model, isolation, caller_depth) -> Awaitable[str]`.
- Produces (Tasks 6–9 rely on):
  - `WorkflowEngine(deps, spawn, *, timeout_secs=DEFAULT_TIMEOUT_SECS)`
  - `async WorkflowEngine.run(script: str, args: object, tool_call_id: str) -> str`
  - Constants: `DEFAULT_TIMEOUT_SECS = 1800.0`, `MAX_RESULT_CHARS = 24_000`

- [ ] **Step 1: Write the failing tests**

`tests/test_workflow_engine.py` (this file grows across Tasks 2–5; start with):

```python
import asyncio

import pytest

from marim_harness.workflows.engine import WorkflowEngine
from tests.conftest import _make_deps


def _engine(tmp_path, spawn, **kw):
    deps = _make_deps(tmp_path)
    return WorkflowEngine(deps, spawn, **kw), deps


async def _echo_spawn(type, task, stream_id, mcp_names, max_output_chars,
                      model, isolation, caller_depth):
    await asyncio.sleep(0)
    return f"[{type}@{caller_depth}] {task}"


@pytest.mark.anyio
async def test_last_expression_is_the_tool_result(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run('{"answer": 1 + 1}', None, "tc1")
    assert '"answer": 2' in out


@pytest.mark.anyio
async def test_agent_calls_reach_the_spawner_with_synth_stream_ids(tmp_path):
    seen: list[tuple] = []

    async def spawn(*a):
        seen.append(a)
        return "report"

    eng, _ = _engine(tmp_path, spawn)
    script = 'r = await agent("do x", type="explore")\nr'
    out = await eng.run(script, None, "tc1")
    assert '"report"' in out or "report" in out
    (type_, task, stream_id, mcp, cap, model, iso, depth) = seen[0]
    assert type_ == "explore" and task == "do x"
    assert stream_id == "tc1::wf1"
    assert mcp is None and depth == 0


@pytest.mark.anyio
async def test_gather_fans_out_concurrently(tmp_path):
    running = 0
    peak = 0

    async def spawn(type, task, *rest):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return task

    eng, _ = _engine(tmp_path, spawn)
    script = (
        "import asyncio\n"
        'results = await asyncio.gather(*[agent(d) for d in ["a", "b", "c"]])\n'
        "results"
    )
    out = await eng.run(script, None, "tc1")
    assert peak == 3
    assert '"a"' in out and '"c"' in out


@pytest.mark.anyio
async def test_args_are_injected(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("args['target']", {"target": "src/"}, "tc1")
    assert "src/" in out


@pytest.mark.anyio
async def test_oversized_result_spills_to_workspace_file(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    out = await eng.run('["x" * 100] * 500', None, "tc9")
    assert ".marim/workflow-output/tc9.json" in out
    assert (deps.workspace.root / ".marim/workflow-output/tc9.json").exists()


@pytest.mark.anyio
async def test_on_workflow_spawn_fires_before_each_child(tmp_path):
    announced: list[tuple] = []

    async def on_spawn(stream_id, type_, task, parent):
        announced.append((stream_id, type_, task, parent))

    eng, deps = _engine(tmp_path, _echo_spawn)
    deps.ui.on_workflow_spawn = on_spawn
    script = (
        "import asyncio\n"
        'await asyncio.gather(agent("t1"), agent("t2"))\n'
        '"done"'
    )
    await eng.run(script, None, "tcX")
    ids = sorted(a[0] for a in announced)
    assert ids == ["tcX::wf1", "tcX::wf2"]
    assert all(a[3] == "tcX" for a in announced)
```

Note on `_make_deps`: it lives in `tests/conftest.py` and builds a `Deps` with a tmp workspace — read its signature before use and adapt the two-line helper if it takes different arguments. Note on anyio: check how existing async tests are marked (`grep -rn "anyio\|asyncio_mode" pyproject.toml tests/conftest.py`) and use the same marker/mode.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py -v`
Expected: FAIL — `ModuleNotFoundError` on `marim_harness.workflows.engine`.

- [ ] **Step 3: Implement the engine core**

`src/marim_harness/workflows/engine.py`:

```python
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
```

Adaptation notes for the implementer:
- `fs.write_file`'s exact signature: check `src/marim_harness/tools/impl/fs.py` — the runner calls it as `fs.write_file(self.deps.workspace.root, rel, spill)` (`subagents/runner.py:375-384`); mirror that call.
- The `getattr(self.deps.ui, "on_workflow_spawn", None)` reads become plain attribute reads once Task 6/8 add the fields; leave `getattr` until then so this task stands alone.
- If pyright complains about the `ResourceLimits` dict literal, construct it as `ResourceLimits(max_duration_secs=30.0, max_memory=256 * 1024 * 1024)` (it's a `TypedDict`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint (watch C901 on `_agent_call`), type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/workflows/engine.py tests/test_workflow_engine.py
git commit -m "feat(workflows): Monty engine — agent()/log() host functions, gather fan-out"
```

---

### Task 3: Engine schema validation + retry

**Files:**
- Modify: `src/marim_harness/workflows/engine.py` (only if tests expose gaps — the Task 2 code already implements the retry)
- Test: `tests/test_workflow_engine.py` (append)

**Interfaces:** unchanged from Task 2.

- [ ] **Step 1: Write the failing/characterization tests**

Append to `tests/test_workflow_engine.py`:

```python
FINDINGS = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"],
}

SCHEMA_SCRIPT = (
    'r = await agent("review", type="explore", schema=' + repr(FINDINGS) + ")\n"
    'r["findings"]'
)


@pytest.mark.anyio
async def test_schema_valid_report_returns_a_dict_into_the_script(tmp_path):
    async def spawn(type, task, *rest):
        assert "Output contract" in task
        return '{"findings": ["bug in x"]}'

    eng, _ = _engine(tmp_path, spawn)
    out = await eng.run(SCHEMA_SCRIPT, None, "tc1")
    assert "bug in x" in out


@pytest.mark.anyio
async def test_schema_failure_respawns_once_with_the_validation_error(tmp_path):
    calls: list[str] = []

    async def spawn(type, task, *rest):
        calls.append(task)
        if len(calls) == 1:
            return "not json at all"
        return '{"findings": []}'

    eng, _ = _engine(tmp_path, spawn)
    out = await eng.run(SCHEMA_SCRIPT, None, "tc1")
    assert len(calls) == 2
    assert "failed validation" in calls[1]
    assert out == "[]"


@pytest.mark.anyio
async def test_schema_failure_after_retry_raises_into_the_script(tmp_path):
    async def spawn(type, task, *rest):
        return "still not json"

    eng, _ = _engine(tmp_path, spawn)
    script = (
        "try:\n"
        '    r = await agent("review", schema=' + repr(FINDINGS) + ")\n"
        "except Exception as e:\n"
        '    r = "caught: " + str(e)\n'
        "r"
    )
    out = await eng.run(script, None, "tc1")
    assert "caught:" in out and "schema validation" in out
```

- [ ] **Step 2: Run, fix, re-run**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py -v`. The Task 2 implementation should already pass these; if a test fails, fix `_agent_call` (not the test) until green.

- [ ] **Step 3: Commit**

```bash
git add tests/test_workflow_engine.py src/marim_harness/workflows/engine.py
git commit -m "test(workflows): schema validation, retry, and in-script error paths"
```

---

### Task 4: Engine failures and limits

**Files:**
- Modify: `src/marim_harness/workflows/engine.py` (gap-fixes only)
- Test: `tests/test_workflow_engine.py` (append)

**Interfaces:** unchanged.

- [ ] **Step 1: Write the tests**

Append:

```python
@pytest.mark.anyio
async def test_syntax_error_returns_a_fixable_tool_message(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("def broken(:\n    pass", None, "tc1")
    assert "failed to parse" in out


@pytest.mark.anyio
async def test_uncaught_script_exception_names_the_line(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run('x = 1\nraise ValueError("boom")\nx', None, "tc1")
    assert "Workflow script raised" in out and "boom" in out


@pytest.mark.anyio
async def test_agent_failure_is_catchable_in_script(tmp_path):
    async def spawn(*a):
        raise RuntimeError("spawn exploded")

    eng, _ = _engine(tmp_path, spawn)
    script = (
        "try:\n"
        '    r = await agent("x")\n'
        "except Exception as e:\n"
        '    r = "recovered: " + str(e)\n'
        "r"
    )
    out = await eng.run(script, None, "tc1")
    assert "recovered:" in out and "spawn exploded" in out


@pytest.mark.anyio
async def test_sandbox_denies_filesystem_and_imports(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run('open("/etc/passwd").read()', None, "tc1")
    # Monty denies `open` (no OS access is configured on run_async); whether
    # it fails at parse or at run, it must surface as a tool-visible error,
    # never as file contents.
    assert "root:" not in out
    assert "raised" in out or "failed to parse" in out
    out2 = await eng.run("import socket\nsocket", None, "tc1")
    assert "raised" in out2 or "failed to parse" in out2


@pytest.mark.anyio
async def test_infinite_loop_is_killed_by_vm_limits(tmp_path, monkeypatch):
    import marim_harness.workflows.engine as engine_mod
    monkeypatch.setattr(engine_mod, "_VM_LIMITS", {"max_duration_secs": 0.5})
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("while True:\n    pass", None, "tc1")
    assert "Workflow script raised" in out


@pytest.mark.anyio
async def test_wall_clock_timeout_cancels_children(tmp_path):
    cancelled = asyncio.Event()

    async def slow_spawn(*a):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=0.3)
    out = await eng.run('await agent("x")\n"done"', None, "tc1")
    assert "timed out" in out
    assert cancelled.is_set()
```

- [ ] **Step 2: Run, fix gaps, re-run**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py -v`. Task 2's code should pass; fix the engine where it doesn't. Watch the timeout test: `asyncio.TimeoutError` (3.10) — if it fails, check what `wait_for` raises on your interpreter and catch both (`except (asyncio.TimeoutError, TimeoutError)` is safe and 3.14-proof).

- [ ] **Step 3: Commit**

```bash
git add tests/test_workflow_engine.py src/marim_harness/workflows/engine.py
git commit -m "test(workflows): parse/runtime failures, VM limits, wall-clock timeout"
```

---

### Task 5: Engine cancellation (the invariant-critical task)

**Files:**
- Modify: `src/marim_harness/workflows/engine.py` (gap-fixes only)
- Test: `tests/test_workflow_engine.py` (append)

**Interfaces:** unchanged. Read the module docstring's cancellation invariant before touching anything.

- [ ] **Step 1: Write the tests**

Append:

```python
@pytest.mark.anyio
async def test_cancelling_the_run_aborts_children_and_reraises(tmp_path):
    started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def slow_spawn(*a):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            child_cancelled.set()
            raise
        return "never"

    eng, _ = _engine(tmp_path, slow_spawn)
    script = (
        "import asyncio\n"
        'await asyncio.gather(agent("a"), agent("b"))\n'
        '"done"'
    )
    run = asyncio.ensure_future(eng.run(script, None, "tc1"))
    await started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    # The drain must have completed: children saw the cancel, and the whole
    # thing neither hung nor crashed the interpreter (the Monty GIL bug).
    assert child_cancelled.is_set()


@pytest.mark.anyio
async def test_post_abort_agent_calls_refuse_immediately(tmp_path):
    # A script that catches WorkflowCancelled and tries to keep spawning must
    # be refused by every subsequent agent() call.
    calls = 0
    release = asyncio.Event()

    async def spawn(type, task, *rest):
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()  # parked until cancelled
        return "r"

    eng, _ = _engine(tmp_path, spawn)
    script = (
        "out = []\n"
        "try:\n"
        '    out.append(await agent("first"))\n'
        "except Exception:\n"
        "    try:\n"
        '        out.append(await agent("second"))\n'
        "    except Exception as e:\n"
        '        out.append("refused: " + str(e))\n'
        "out"
    )
    run = asyncio.ensure_future(eng.run(script, None, "tc1"))
    await asyncio.sleep(0.1)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    # Only the first spawn ever launched; the second was refused pre-spawn.
    assert calls == 1
```

- [ ] **Step 2: Run, fix, re-run**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py -v -x`
These exercise `_abort_and_drain` and the `state.abort` pre-check in `_spawn_child`. If the first test hangs: the VM isn't finishing after children raise — check that `_spawn_child` converts the child's `CancelledError` into `WorkflowCancelled` (a raw `CancelledError` from a host function may wedge Monty's dispatch loop). Never "fix" a hang by cancelling `vm` — that is the crash the design exists to avoid.

- [ ] **Step 3: Commit**

```bash
git add tests/test_workflow_engine.py src/marim_harness/workflows/engine.py
git commit -m "test(workflows): abort winds the VM down through host functions, never cancels it"
```

---

### Task 6: Services seam + config/env wiring

**Files:**
- Modify: `src/marim_harness/runtime/deps.py` (WorkflowRunner type + services field + UIHooks fields)
- Modify: `src/marim_harness/runtime/harness.py` (HarnessConfig fields + guarded engine build + services wiring)
- Modify: `src/marim_harness/config/model.py` (env read)
- Modify: `src/marim_harness/runtime/bootstrap.py` (config override pass-through)
- Modify: `.env.example`
- Test: `tests/test_workflow_wiring.py` (new), plus one assertion in `tests/test_config.py`

**Interfaces:**
- Consumes: `WorkflowEngine` from Task 2; existing `build_collaborators`/`build_services` structure (`runtime/harness.py:217+`), `with_config_overrides` (`runtime/builder.py:188-199`), `_bool_env` (`config/model.py`).
- Produces (Task 7–8 rely on):
  - `deps.py`: `WorkflowRunner = Callable[[str, object, str], Awaitable[str]]`; `HarnessServices.run_workflow: WorkflowRunner | None = None`; `UIHooks.on_workflow_spawn: Callable[[str, str, str, str], Awaitable[None]] | None = None`; `UIHooks.on_workflow_log: Callable[[str], None] | None = None`
  - `HarnessConfig.workflows_enabled: bool = True`, `HarnessConfig.workflow_timeout_secs: float = 1800.0`
  - Env: `MARIM_WORKFLOWS` (default on)

- [ ] **Step 1: Write the failing tests**

`tests/test_workflow_wiring.py`:

```python
from marim_harness.runtime.deps import HarnessServices, UIHooks


def test_services_default_run_workflow_none():
    assert HarnessServices().run_workflow is None


def test_ui_hooks_default_workflow_callbacks_none():
    ui = UIHooks()
    assert ui.on_workflow_spawn is None
    assert ui.on_workflow_log is None


def test_harness_wires_run_workflow_when_monty_available(tmp_path):
    # Build a harness the way other wiring tests do (find the smallest
    # existing fixture: grep tests/ for build_collaborators or Harness( with
    # TestModel) and assert services.run_workflow is not None because the dev
    # environment has pydantic-monty installed.
    from pydantic_ai.models.test import TestModel
    from marim_harness.runtime.harness import Harness

    h = Harness(model=TestModel(), workspace=tmp_path)
    assert h.deps.services.run_workflow is not None


def test_harness_respects_workflows_disabled(tmp_path):
    from pydantic_ai.models.test import TestModel
    from marim_harness.runtime.harness import Harness

    h = Harness(model=TestModel(), workspace=tmp_path, workflows_enabled=False)
    assert h.deps.services.run_workflow is None
```

Adapt the two harness-construction tests to the real minimal constructor: check how `tests/test_agent.py` builds a `Harness` (model + workspace kwargs) and copy that shape. `Harness.__init__` accepts legacy `**kwargs` as `HarnessConfig` shorthand, so `workflows_enabled=False` threads through.

In `tests/test_config.py`, add (mirroring the existing `MARIM_FORGE` test if present — grep for `forge_enabled`):

```python
def test_workflows_env_gate(monkeypatch):
    monkeypatch.setenv("MARIM_WORKFLOWS", "0")
    cfg = load_config()  # use the same loader the forge_enabled test uses
    assert cfg.workflows_enabled is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_workflow_wiring.py -v`
Expected: FAIL — no `run_workflow` field.

- [ ] **Step 3: Implement**

`runtime/deps.py` — next to `BackgroundAgentRunner`:

```python
# (script, args, tool_call_id) -> the workflow's final result, shaped for the
# model. None when workflows are disabled (MARIM_WORKFLOWS=0) or
# pydantic-monty is not installed — the run_workflow tool returns an install
# hint in that case. Wired by the Harness (see _build_workflow_engine).
WorkflowRunner = Callable[[str, object, str], Awaitable[str]]
```

On `HarnessServices`:

```python
    # Lets the run_workflow tool execute a model-authored orchestration
    # script in the Monty sandbox. None ⇒ workflows unavailable.
    run_workflow: WorkflowRunner | None = None
```

On `UIHooks` (both used by the engine; the TUI wires them in Task 8):

```python
    # (stream_id, type, task, parent_tool_call_id) -> None. Fired by the
    # workflow engine BEFORE each child spawn so the TUI can claim a card for
    # a stream id that has no spawn_agent tool call to intercept (cards are
    # otherwise created only when a literal spawn_agent call renders).
    on_workflow_spawn: "Callable[[str, str, str, str], Awaitable[None]] | None" = None
    # (message) -> None. A workflow script's log() line. None when headless
    # (the engine falls back to DEBUG logging).
    on_workflow_log: "Callable[[str], None] | None" = None
```

`runtime/harness.py` — `HarnessConfig` fields (near `forge_enabled`):

```python
    # Dynamic workflows: the run_workflow tool's engine. Enabled by default,
    # but the engine only builds when pydantic-monty is importable (the
    # [workflows] extra); otherwise services.run_workflow stays None and the
    # tool answers with an install hint. MARIM_WORKFLOWS=0 turns it off.
    workflows_enabled: bool = True
    # Overall wall-clock ceiling for one run_workflow call. VM compute is
    # separately bounded by the engine's ResourceLimits; this bounds total
    # duration including sub-agent time.
    workflow_timeout_secs: float = 1800.0
```

Engine build helper (module-level in `harness.py`, called from where `run_subagent`/`run_background_agent` are wired — `harness.py:190-195`):

```python
def _build_workflow_engine(cfg: HarnessConfig, deps: Deps, subagents):
    """The workflow engine, or None when disabled or pydantic-monty is not
    installed. The import is guarded HERE (not in the tool) so availability
    is decided once at build time and the tool only checks the seam."""
    if not cfg.workflows_enabled:
        return None
    try:
        from ..workflows.engine import WorkflowEngine
    except ImportError:
        logger.info(
            "workflows unavailable: pydantic-monty not installed "
            "(uv add 'marim-harness[workflows]')"
        )
        return None
    return WorkflowEngine(deps, subagents.run, timeout_secs=cfg.workflow_timeout_secs)
```

and where services are populated:

```python
        engine = _build_workflow_engine(cfg, deps, subagents)
        ...
        run_workflow=engine.run if engine is not None else None,
```

`config/model.py` — add to the config dataclass next to `forge_enabled` and to the env-reading dict: `workflows_enabled=_bool_env("MARIM_WORKFLOWS", True)`.

`runtime/bootstrap.py` — add `workflows_enabled=cfg.workflows_enabled` to the existing `with_config_overrides(...)` call (bootstrap.py:126-171).

`.env.example` — add:

```bash
# Dynamic workflows (the run_workflow tool; needs the [workflows] extra). Default on.
#MARIM_WORKFLOWS=1
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_workflow_wiring.py tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/deps.py src/marim_harness/runtime/harness.py \
        src/marim_harness/config/model.py src/marim_harness/runtime/bootstrap.py \
        .env.example tests/test_workflow_wiring.py tests/test_config.py
git commit -m "feat(workflows): services seam, HarnessConfig knobs, MARIM_WORKFLOWS gate"
```

---

### Task 7: The `run_workflow` tool + registration

**Files:**
- Create: `src/marim_harness/tools/workflow_tools.py`
- Modify: `src/marim_harness/tools/names.py` (TOOL_GROUPS entry)
- Modify: `src/marim_harness/tools/provider.py` (ToolGroups field + gated registration)
- Test: `tests/test_workflow_tool.py` (new) + additions to `tests/test_provider.py`

**Interfaces:**
- Consumes: `ctx.deps.services.run_workflow` (Task 6).
- Produces: tool name `"run_workflow"`; `ToolGroups.workflow: bool = True`; `TOOL_GROUPS["workflow"] = frozenset({"run_workflow"})`. Main-agent only (NOT added to `_SUBAGENT_FNS` or `SUBAGENT_TOOLS`).

- [ ] **Step 1: Write the failing tests**

`tests/test_workflow_tool.py`:

```python
from types import SimpleNamespace

import pytest

from marim_harness.tools.workflow_tools import run_workflow
from tests.conftest import _make_deps


def _ctx(deps, tool_call_id="tc1"):
    return SimpleNamespace(deps=deps, tool_call_id=tool_call_id)


@pytest.mark.anyio
async def test_unavailable_seam_returns_install_hint(tmp_path):
    deps = _make_deps(tmp_path)
    deps.services.run_workflow = None
    out = await run_workflow(_ctx(deps), "1 + 1")
    assert "workflows" in out.lower() and "install" in out.lower()


@pytest.mark.anyio
async def test_delegates_script_args_and_tool_call_id(tmp_path):
    deps = _make_deps(tmp_path)
    seen = {}

    async def fake_runner(script, args, tool_call_id):
        seen.update(script=script, args=args, tool_call_id=tool_call_id)
        return "result"

    deps.services.run_workflow = fake_runner
    out = await run_workflow(_ctx(deps, "abc"), "1 + 1", args={"k": 1})
    assert out == "result"
    assert seen == {"script": "1 + 1", "args": {"k": 1}, "tool_call_id": "abc"}
```

Additions to `tests/test_provider.py` (near the group-toggle tests, ~line 1000):

```python
def test_workflow_group_registers_the_gated_tool():
    agent = _agent()  # use the same agent fixture the neighboring tests use
    BuiltinToolProvider().register(agent)
    assert "run_workflow" in _tool_names(agent)


def test_workflow_group_off_removes_it():
    agent = _agent()
    BuiltinToolProvider(ToolGroups(workflow=False)).register(agent)
    assert "run_workflow" not in _tool_names(agent)


def test_run_workflow_is_not_grantable_to_subagents():
    from marim_harness.tools.names import SUBAGENT_TOOLS
    assert "run_workflow" not in SUBAGENT_TOOLS


@pytest.mark.anyio
async def test_run_workflow_is_denied_in_plan_mode():
    # Approval flows through the generic resolve_approvals path (it is
    # name-agnostic for requires_approval tools — verified against
    # runtime/permissions.py), and plan mode's _plan_decision denies any
    # gated tool it doesn't special-case. Pin that: workflows must not run
    # in plan mode.
    from types import SimpleNamespace

    from pydantic_ai import DeferredToolRequests, ToolDenied

    from marim_harness.runtime.permissions import Mode, resolve_approvals

    call = SimpleNamespace(tool_name="run_workflow", tool_call_id="c1", args={})
    requests = DeferredToolRequests(approvals=[call])
    results = await resolve_approvals(requests, Mode.plan, None)
    assert isinstance(results.approvals["c1"], ToolDenied)
```

(If `DeferredToolRequests` won't accept a `SimpleNamespace`, build the call the way the existing permissions tests do — `grep -rn "resolve_approvals" tests/` and copy that fixture.)

(The existing `test_tool_groups_match_dataclass_fields` and `test_each_group_toggles_exactly_its_tools` pick the new group up automatically — they must stay green.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov tests/test_workflow_tool.py tests/test_provider.py -v`
Expected: new tests FAIL (no module / no group).

- [ ] **Step 3: Implement**

`src/marim_harness/tools/names.py` — add to `TOOL_GROUPS`:

```python
    "workflow": frozenset({"run_workflow"}),
```

(Do NOT add it to `SUBAGENT_TOOLS` — workflows are main-agent only, like spawn.)

`src/marim_harness/tools/provider.py` — `ToolGroups` gets `workflow: bool = True`; in `_register_action_tools`, after the spawn block:

```python
        if g.workflow:
            agent.tool(requires_approval=True)(workflow_tools.run_workflow)
```

with the corresponding `from . import workflow_tools` import.

`src/marim_harness/tools/workflow_tools.py`:

```python
"""The run_workflow tool: thin ctx.deps unwrapping over the workflow engine.

The docstring below is the model-facing product documentation for workflow
scripts — it is the ONLY place the model learns the sandbox's dialect and
host API from, so treat every sentence as UI copy."""

from __future__ import annotations

from pydantic import JsonValue
from pydantic_ai import RunContext

from ..runtime.deps import Deps

_UNAVAILABLE = (
    "Workflows are unavailable in this session: the pydantic-monty sandbox "
    "is not installed (install the marim-harness[workflows] extra) or "
    "MARIM_WORKFLOWS is off. Use spawn_agent for fan-out instead."
)


async def run_workflow(
    ctx: RunContext[Deps], script: str, args: JsonValue = None
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
    - `asyncio.gather(...)` — run agent() calls concurrently. This is the
      fan-out primitive; concurrency is capped downstream, so gather as wide
      as the work needs.

    The script's LAST EXPRESSION is this tool's result — end with plain data
    (dict/list/str), JSON-serialized for you and spilled to a workspace file
    if very large. Keep intermediate results in variables; return only what
    you need.

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

    Scripts are wall-clock bounded and aborted cleanly on interrupt. An
    infinite compute loop is killed by the sandbox. If the script fails to
    parse, fix the reported error and call again."""
    runner = ctx.deps.services.run_workflow
    if runner is None:
        return _UNAVAILABLE
    return await runner(script, args, ctx.tool_call_id or "")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_workflow_tool.py tests/test_provider.py -v`
Expected: PASS, including the pre-existing mirror/toggle tests.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/tools/workflow_tools.py src/marim_harness/tools/names.py \
        src/marim_harness/tools/provider.py tests/test_workflow_tool.py tests/test_provider.py
git commit -m "feat(workflows): gated run_workflow tool, workflow tool group"
```

---

### Task 8: TUI — cards for workflow spawns + log lines

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (`bind_ui` params, ~line 434)
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (claim method)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`bind_ui` call, ~line 113)
- Test: extend whichever test file already exercises `StreamRenderer`/card claiming (`grep -rln "mount_spawn_widget\|_claim_spawn" tests/`); create `tests/test_workflow_tui.py` if none fits.

**Interfaces:**
- Consumes: `UIHooks.on_workflow_spawn` / `on_workflow_log` fields (Task 6); `mount_spawn_widget(args) -> SubAgentWidget` and `ensure_pane` / `tool_widgets` registry (`stream_render.py:743-757`, `:254-271`).
- Produces: `StreamRenderer.claim_workflow_spawn(stream_id: str, type_: str, task: str, parent_id: str) -> Awaitable[None]`.

Background (why this exists): cards are created ONLY when a literal `spawn_agent` tool call renders (`stream_render.py:317`, `:395`); `on_subagent_event` silently drops unknown stream ids (`stream_render.py:819-821`). Workflow children have synthesized ids (`<tool_call_id>::wfN`), so without this seam they'd be invisible.

- [ ] **Step 1: Write the failing test**

Follow the conventions of the existing stream_render/TUI tests (they run the Textual app headless via `run_test()` — copy the harness fixture from `tests/test_approval.py`'s `_Harness(App)` pattern). The test:

```python
@pytest.mark.anyio
async def test_claim_workflow_spawn_registers_a_card():
    app = _Harness()
    async with app.run_test():
        r = app.renderer  # adapt: however tests reach the StreamRenderer
        await r.claim_workflow_spawn("tc1::wf1", "explore", "review bugs", "tc1")
        widget = r.tool_widgets["tc1::wf1"]
        assert widget.stream_id == "tc1::wf1"
        assert widget in r.subagents
        # events for this id now route instead of dropping:
        # (mirror an existing on_subagent_event routing assertion here)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest --no-cov <chosen test file> -v` — FAIL (no `claim_workflow_spawn`).

- [ ] **Step 3: Implement**

`stream_render.py`, next to `mount_spawn_widget`:

```python
    async def claim_workflow_spawn(
        self, stream_id: str, type_: str, task: str, parent_id: str
    ) -> None:
        """A card for a workflow-spawned sub-agent. Workflow children have no
        spawn_agent tool call for the sinks to intercept (the engine
        synthesizes their stream ids), so the engine announces each spawn
        through this callback BEFORE launching it — otherwise its whole run
        would be dropped by on_subagent_event's unknown-id guard. The card
        mounts top-level; ``parent_id`` (the run_workflow tool_call_id) is
        accepted for future tree grouping but not yet used for nesting."""
        widget = self.mount_spawn_widget({"type": type_, "task": task})
        widget.stream_id = stream_id
        widget.parent_id = None
        self.tool_widgets[stream_id] = widget
        self.ensure_pane(widget)
        await self._mount_top_level(widget)
```

For `_mount_top_level`: find how the top-level sink resolves its `container` (the `#log` mount target passed into `intercept_tool` — trace `_TopLevelSink` construction around `stream_render.py:300-320`) and either reuse an existing accessor or add a small one; do NOT duplicate the query selector in two places.

`runtime/harness.py` `bind_ui`: add parameters `on_workflow_spawn=None, on_workflow_log=None` (typed like the UIHooks fields) and assign them alongside the other `self.deps.ui.*` assignments.

`interfaces/tui/app.py` in the `bind_ui(...)` call:

```python
            on_workflow_spawn=self._on_workflow_spawn,
            on_workflow_log=lambda msg: self.notify(msg, title="workflow", timeout=4),
```

with an app method delegating to the renderer (match how the neighboring `on_subagent_event` callback is wired — same threading/`call_from_thread` discipline if any):

```python
    async def _on_workflow_spawn(self, stream_id: str, type_: str,
                                 task: str, parent_id: str) -> None:
        await self.renderer.claim_workflow_spawn(stream_id, type_, task, parent_id)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov <chosen test file> tests/test_workflow_engine.py -v`
Expected: PASS (engine tests confirm the callback contract still holds).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/harness.py src/marim_harness/interfaces/tui/stream_render.py \
        src/marim_harness/interfaces/tui/app.py tests/
git commit -m "feat(tui): cards + log toasts for workflow-spawned sub-agents"
```

---

### Task 9: Acceptance test, docs, full sweep

**Files:**
- Test: `tests/test_workflow_acceptance.py`
- Modify: `CLAUDE.md` (subsystem bullet)

**Interfaces:** consumes everything above; produces nothing new.

- [ ] **Step 1: Write the acceptance test (the spec's "done" bar)**

`tests/test_workflow_acceptance.py` — the parallel review sweep end-to-end through the REAL engine + REAL tool, fake spawner:

```python
import asyncio
import json
from types import SimpleNamespace

import pytest

from marim_harness.tools.workflow_tools import run_workflow
from marim_harness.workflows.engine import WorkflowEngine
from tests.conftest import _make_deps

SWEEP = """
import asyncio

SCHEMA = {"type": "object",
          "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
          "required": ["findings"]}

async def review(dim):
    r = await agent("Review the diff for " + dim + " issues",
                    type="explore", schema=SCHEMA)
    return r["findings"]

per_dim = await asyncio.gather(*[review(d) for d in ["bugs", "performance", "style"]])
log("reviewed " + str(len(per_dim)) + " dimensions")
{"findings": [f for fs in per_dim for f in fs]}
"""

CANNED = {
    "bugs": ["off-by-one in pager"],
    "performance": ["N+1 query in listing"],
    "style": [],
}


@pytest.mark.anyio
async def test_parallel_review_sweep_end_to_end(tmp_path):
    announced = []

    async def spawn(type, task, stream_id, mcp, cap, model, iso, depth):
        assert type == "explore" and "Output contract" in task
        dim = next(d for d in CANNED if d in task)
        await asyncio.sleep(0.01)
        return json.dumps({"findings": CANNED[dim]})

    async def on_spawn(stream_id, type_, task, parent):
        announced.append(stream_id)

    deps = _make_deps(tmp_path)
    deps.ui.on_workflow_spawn = on_spawn
    engine = WorkflowEngine(deps, spawn)
    deps.services.run_workflow = engine.run

    ctx = SimpleNamespace(deps=deps, tool_call_id="sweep1")
    out = await run_workflow(ctx, SWEEP)

    data = json.loads(out)
    assert data == {"findings": ["off-by-one in pager", "N+1 query in listing"]}
    assert sorted(announced) == ["sweep1::wf1", "sweep1::wf2", "sweep1::wf3"]
```

- [ ] **Step 2: Run it**

Run: `uv run pytest --no-cov tests/test_workflow_acceptance.py -v`
Expected: PASS (everything already built; failures here are integration bugs — fix the engine/tool, not the test).

- [ ] **Step 3: Document the subsystem**

`CLAUDE.md`, in "Supporting subsystems", after the `subagents/` bullet:

```markdown
- `workflows/` — dynamic workflows: the gated `run_workflow` tool executes a
  model-authored Python script in a pydantic-monty sandbox (`engine.py`);
  `agent()`/`log()` host functions delegate to `SubagentRunner.run` through
  the `services.run_workflow` seam. Schema validation of agent() reports is
  engine-level (`schema.py`, jsonschema). Never cancel the Monty VM task —
  aborts flow through host functions (see engine.py's module docstring).
  Optional extra `[workflows]`; `MARIM_WORKFLOWS` gates it.
```

- [ ] **Step 4: Full local CI sweep**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all green, coverage in line with the repo's ~93%. Fix anything that isn't.

- [ ] **Step 5: Commit**

```bash
git add tests/test_workflow_acceptance.py CLAUDE.md
git commit -m "test(workflows): parallel review sweep acceptance + subsystem docs"
```

---

## Post-plan notes for the executor

- Branch: `feat/dynamic-workflows` (spec + this plan already committed there).
- The Monty behaviors marked "spike-verified" were tested against pydantic-monty 0.0.18 on 2026-07-11 in this repo's Python 3.10. If a newer patch release changes `run_async`/cancellation behavior, re-run the Task 5 tests first — they encode the dangerous assumptions.
- Live smoke (optional, after all tasks): run the TUI with the local LM Studio model per the project memory (never a paid model without explicit approval) and ask it to "use run_workflow to review this diff for bugs, perf, and style in parallel".
