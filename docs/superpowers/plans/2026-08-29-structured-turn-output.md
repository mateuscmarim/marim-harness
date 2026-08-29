# Structured Turn Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give SDK embedders Claude-Agent-SDK-style structured output: `HarnessBuilder.with_output_type(schema)`, validated data on a new `TurnOutcome` returned by `run_turn`, retry-until-valid semantics, approval loop untouched.

**Architecture:** The agent-level output union stays `[str, DeferredToolRequests]`; each structured turn passes `output_type=[SchemaType, DeferredToolRequests]` per run to `agent.run()` (verified against pydantic-ai ≥2.28: deferred approvals still fire and the continuation round returns the validated object). BaseModel schemas ride pydantic-ai's in-run validation; JSON-Schema dicts ride `StructuredDict` provider constraints plus post-turn jsonschema validation with one corrective turn.

**Tech Stack:** Python ≥3.10, pydantic-ai-slim ≥2.28, pydantic v2, jsonschema, pytest + anyio, TestModel/FunctionModel.

**Spec:** `docs/superpowers/specs/2026-08-29-structured-turn-output-design.md`

## Global Constraints

- `requires-python = ">=3.10"` — no 3.11+-only syntax anywhere.
- pydantic-ai floor is `pydantic-ai-slim[openai,google,mcp]>=2.28,<3`.
- Use `uv` for everything (`uv run …`, `uv sync`); never bare `python`/`pytest`/`pip`.
- Ruff line length 100; rules `E,F,I,UP,B,SIM,C901`. Cyclomatic complexity per function ≤ 10 — extract named helpers rather than adding `# noqa: C901`.
- CI order is ruff → pyright → pytest. Match it before claiming done.
- Core (`runtime/`) must never import the `workflows` package (extra-gated).
- Preserve long explanatory comments on non-obvious invariants when editing nearby code.
- Test files live in `tests/`; the suite runs parallel by default (xdist), serial via `-n 0`.

---

### Task 1: `TurnOutcome` module and public export

**Files:**
- Create: `src/marim_harness/runtime/outcome.py`
- Create: `tests/test_turn_outcome.py`
- Modify: `src/marim_harness/__init__.py` (the `_LAZY` dict)

**Interfaces:**
- Consumes: nothing.
- Produces: `marim_harness.runtime.outcome.TurnOutcome` — frozen dataclass, fields `subtype: Literal["success", "error_max_structured_output_retries", "error_during_execution"]`, `result: str | None`, `structured_output: Any`, `errors: list[str] | None`. Lazy top-level export `marim_harness.TurnOutcome`. Later tasks construct it; keep the field order exactly as defined here.

- [ ] **Step 1: Create the feature branch**

```bash
git switch -c feat/structured-turn-output
```

(Work stays on this branch for every following task.)

- [ ] **Step 2: Write the failing tests**

Create `tests/test_turn_outcome.py`:

```python
import dataclasses

import pytest


def test_turn_outcome_fields_and_defaults():
    from marim_harness.runtime.outcome import TurnOutcome

    ok = TurnOutcome(subtype="success", result="hello",
                     structured_output=None, errors=None)
    assert ok.subtype == "success"
    assert ok.result == "hello"
    assert ok.structured_output is None
    assert ok.errors is None


def test_turn_outcome_is_frozen():
    from marim_harness.runtime.outcome import TurnOutcome

    ok = TurnOutcome(subtype="success", result="x",
                     structured_output=None, errors=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ok.result = "y"


def test_turn_outcome_subtypes():
    from marim_harness.runtime.outcome import TurnOutcome

    # The full subtype vocabulary (error_during_execution is reserved for
    # Claude parity; v1 never emits it).
    for subtype in ("success", "error_max_structured_output_retries",
                    "error_during_execution"):
        TurnOutcome(subtype=subtype, result=None, structured_output=None,
                    errors=None)


def test_turn_outcome_lazy_top_level_export():
    import marim_harness
    from marim_harness.runtime.outcome import TurnOutcome

    assert marim_harness.TurnOutcome is TurnOutcome
    assert "TurnOutcome" in dir(marim_harness)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_turn_outcome.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.runtime.outcome'` (and `AttributeError` for the export test).

- [ ] **Step 4: Implement `outcome.py` and the export**

Create `src/marim_harness/runtime/outcome.py`:

```python
"""The terminal payload of one user turn.

``TurnOutcome`` mirrors the Claude Agent SDK's ``ResultMessage``: the turn's
subtype, the final text (``result``), the validated structured data
(``structured_output``), and failure detail (``errors``). ``run_turn``
returns this in all cases — plain harnesses get ``subtype="success"`` with
``structured_output=None``.

``error_during_execution`` is part of the vocabulary for Claude parity but
v1 never emits it: marim's tool failures feed back to the model as tool
results rather than ending the turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Subtype = Literal[
    "success",
    "error_max_structured_output_retries",
    "error_during_execution",
]


@dataclass(frozen=True)
class TurnOutcome:
    subtype: Subtype
    result: str | None
    structured_output: Any = None
    errors: list[str] | None = None
```

In `src/marim_harness/__init__.py`, add to the `_LAZY` dict (alphabetical position after `"ToolGroups"`):

```python
    "TurnOutcome": ("marim_harness.runtime.outcome", "TurnOutcome"),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_turn_outcome.py -v`
Expected: 4 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/runtime/outcome.py src/marim_harness/__init__.py tests/test_turn_outcome.py
git commit -m "feat(runtime): TurnOutcome terminal-turn payload"
```

---

### Task 2: dict-schema validation helper + jsonschema core dependency

**Files:**
- Modify: `pyproject.toml` (`dependencies` list)
- Create: `src/marim_harness/runtime/structured.py`
- Create: `tests/test_structured_validation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `marim_harness.runtime.structured.validate_dict_output(output: Any, schema: dict) -> list[str]` — empty list means valid; otherwise human-readable error strings. Validator class resolution follows `workflows/schema.py` (`jsonschema.validators.validator_for(schema)`, which honors a schema's own `$schema` and defaults to the latest draft). Task 6 calls it after a dict-schema turn completes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_structured_validation.py`:

```python
from marim_harness.runtime.structured import validate_dict_output

SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
    "required": ["a"],
}


def test_valid_object_passes():
    assert validate_dict_output({"a": 1, "b": "x"}, SCHEMA) == []


def test_wrong_type_reported():
    errors = validate_dict_output({"a": "not-an-int"}, SCHEMA)
    assert errors
    assert any("a" in e for e in errors)


def test_missing_required_reported():
    errors = validate_dict_output({}, SCHEMA)
    assert errors
    assert any("required" in e for e in errors)


def test_non_object_output_reported():
    errors = validate_dict_output(["not", "a", "dict"], SCHEMA)
    assert errors
    assert any("not a JSON object" in e for e in errors)


def test_none_output_reported():
    errors = validate_dict_output(None, SCHEMA)
    assert errors
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_structured_validation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.runtime.structured'`.

- [ ] **Step 3: Promote jsonschema to a core dependency**

In `pyproject.toml`, add to the `dependencies` list (after `"pyyaml>=6"`):

```toml
    "jsonschema>=4",
```

Leave the `[workflows]` extra entry as-is (it becomes redundant but harmless — the extra keeps working for older resolvers). Then sync:

Run: `uv sync`
Expected: resolves cleanly.

- [ ] **Step 4: Implement `structured.py`**

Create `src/marim_harness/runtime/structured.py`:

```python
"""Post-turn validation for dict-schema structured output.

``StructuredDict`` attaches a JSON Schema for provider-side constrained
generation but never validates the emitted object, so a dict-schema turn
gets checked here after the run. Lives in core (not workflows/schema.py)
because core must not import the extra-gated workflows package; validator
class resolution matches it — validator_for(schema), which honors the
schema's own $schema and defaults to the latest draft.
"""

from __future__ import annotations

from typing import Any

from jsonschema.validators import validator_for


def validate_dict_output(output: Any, schema: dict) -> list[str]:
    """Validate a turn's structured output against its JSON Schema.

    Returns ``[]`` when valid, else human-readable error strings. A
    non-dict output fails against the object root rather than crashing.
    """
    if not isinstance(output, dict):
        return [f"structured output is not a JSON object: {type(output).__name__}"]
    validator = validator_for(schema)(schema)
    errors = sorted(validator.iter_errors(output), key=lambda e: list(e.path))
    return [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in errors
    ]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_structured_validation.py -v`
Expected: 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/marim_harness/runtime/structured.py tests/test_structured_validation.py
git commit -m "feat(runtime): dict-schema output validation + jsonschema core dep"
```

---

### Task 3: builder seam — `with_output_type` + `HarnessConfig.output_type`

**Files:**
- Modify: `src/marim_harness/runtime/builder.py` (`__init__`, new setter, `build()` validation + `config_fields`)
- Modify: `src/marim_harness/runtime/harness.py` (`HarnessConfig` dataclass)
- Create: `tests/test_builder_output_type.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `HarnessBuilder.with_output_type(schema) -> HarnessBuilder` (chainable); `HarnessConfig.output_type: Any` (default `None`) carrying the validated schema to `Harness.__init__` (Task 4 wires it onward). Validation at `build()`: a dict must be object-rooted; anything that is neither a dict nor a `pydantic.BaseModel` subclass is a problem.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_builder_output_type.py`:

```python
import pytest
from pydantic import BaseModel
from pydantic_ai.models.test import TestModel

from marim_harness import BuilderError, HarnessBuilder


class Report(BaseModel):
    summary: str


def test_build_accepts_basemodel_schema(tmp_path):
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_output_type(Report)
         .build())
    # Harness does not retain its config; the TurnController is where the
    # schema lands (Task 4 reads it).
    assert h.turn_controller._structured_type is Report
    assert h.turn_controller._output_type_dict is None


def test_build_accepts_object_rooted_dict(tmp_path):
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_output_type(schema)
         .build())
    assert h.turn_controller._output_type_dict == schema
    # The dict was wrapped into pydantic-ai's StructuredDict type.
    assert h.turn_controller._structured_type is not None
    assert h.turn_controller._structured_type is not schema


def test_build_rejects_non_object_rooted_dict(tmp_path):
    with pytest.raises(BuilderError) as exc_info:
        (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_output_type({"type": "array"})
         .build())
    assert any("object-rooted" in p for p in exc_info.value.problems)


def test_build_rejects_other_types(tmp_path):
    with pytest.raises(BuilderError) as exc_info:
        (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_output_type(int)
         .build())
    assert any("with_output_type" in p for p in exc_info.value.problems)


def test_build_without_output_type_leaves_none(tmp_path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()
    assert h.turn_controller._structured_type is None
    assert h.turn_controller._output_type_dict is None
```

Note: these asserts read the controller attributes created in Step 5 below — the task is self-contained: RED at Step 2 (no setter yet), GREEN after Step 5.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_builder_output_type.py -v`
Expected: FAIL — `AttributeError: 'HarnessBuilder' object has no attribute 'with_output_type'`.

- [ ] **Step 3: Add the config field**

In `src/marim_harness/runtime/harness.py`, add to the `HarnessConfig` dataclass (after `forge_backend`):

```python
    # Structured output for embedder turns (HarnessBuilder.with_output_type).
    # A pydantic BaseModel subclass or an object-rooted JSON Schema dict;
    # None ⇒ turns return plain text. Typed Any (like capabilities) to keep
    # this dataclass's imports light; TurnController resolves it into the
    # per-run output_type override.
    output_type: Any = None
```

- [ ] **Step 4: Add the builder setter and validation**

In `src/marim_harness/runtime/builder.py`:

1. In `__init__`, after `self._config_overrides: dict[str, Any] = {}`:

```python
        self._output_type: Any = None
```

2. Add the setter after `with_thinking`:

```python
    def with_output_type(self, schema: Any) -> HarnessBuilder:
        """Structured output for every turn: a pydantic ``BaseModel`` subclass
        or an object-rooted JSON Schema dict. ``run_turn`` then returns a
        ``TurnOutcome`` whose ``structured_output`` is the validated object.
        The schema is a property of this composition — one harness, one
        schema."""
        self._output_type = schema
        return self
```

3. Add a validation pass in `build()`, right before `if problems: raise BuilderError(problems)`:

```python
        self._check_output_type(problems)
```

and the helper next to the other `_check_*` methods:

```python
    def _check_output_type(self, problems: list[str]) -> None:
        from pydantic import BaseModel

        schema = self._output_type
        if schema is None:
            return
        if isinstance(schema, dict):
            if schema.get("type") != "object":
                problems.append(
                    "with_output_type: JSON Schema must be object-rooted "
                    f"(got type {schema.get('type')!r})")
        elif not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            problems.append(
                "with_output_type: expected a pydantic BaseModel subclass or "
                f"an object-rooted JSON Schema dict, got {schema!r}")
```

4. In `build()`'s `config_fields` dict, add the entry (after `titler=...`):

```python
            output_type=self._output_type,
```

- [ ] **Step 5: Wire the passthrough into the controller**

In `src/marim_harness/runtime/controller.py`, add the import (top of file):

```python
from pydantic_ai import StructuredDict
```

Add the `__init__` parameter after `lsp_toolset`:

```python
        output_type: Any = None,
```

and in the body, after `self.lsp_toolset = lsp_toolset`:

```python
        # Structured output for embedder turns (HarnessBuilder.with_output_type).
        # Resolved ONCE here: a dict schema becomes StructuredDict (the
        # provider-constrained dict type pydantic-ai uses for structured
        # output), a BaseModel subclass passes through. Task 4 turns this
        # into the per-run output_type override on every agent.run round.
        self._output_type_dict = output_type if isinstance(output_type, dict) else None
        self._structured_type: Any = (
            StructuredDict(output_type) if isinstance(output_type, dict) else output_type
        )
```

In `src/marim_harness/runtime/harness.py`, TurnController construction (`Harness.__init__`, ~line 594) — add the kwarg:

```python
            get_thinking=lambda: self.thinking_level_id,
            output_type=cfg.output_type,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_builder_output_type.py -v`
Expected: 5 PASS. Also run the existing builder and controller suites to catch regressions:

Run: `uv run pytest --no-cov tests/test_builder.py tests/test_builder_sessions.py tests/test_turn_controller.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/runtime/builder.py src/marim_harness/runtime/harness.py src/marim_harness/runtime/controller.py tests/test_builder_output_type.py
git commit -m "feat(builder): with_output_type seam for structured turns"
```

---

### Task 4: flip `run_turn` to `TurnOutcome` + per-run output override

This is the keystone task: the return type changes for every harness, the structured override rides every `agent.run` round, and all affected callsites migrate. The tree is red between steps 1 and 5 — that is expected; commit only when green.

**Files:**
- Modify: `src/marim_harness/runtime/controller.py`
- Modify: `src/marim_harness/runtime/harness.py` (TurnController construction, `run_turn` docstring)
- Modify: `src/marim_harness/interfaces/cli/headless.py`
- Modify (migration): `tests/test_agent_hooks.py`, `tests/test_agent_mcp.py`, `tests/test_agent.py`, `tests/test_agent_sessions.py`, `tests/test_agent_subagents.py`, `tests/test_builder_turns.py`, `tests/test_live_smoke.py`, `tests/test_plan_mode_e2e.py`, `tests/test_provider_errors.py`, `tests/test_recovery.py`, `tests/test_steering.py`, `tests/test_subagent_model.py`, `tests/test_turn_controller.py`
- Create: `tests/test_structured_turn.py`

**Interfaces:**
- Consumes: `TurnOutcome` (Task 1), `HarnessConfig.output_type` (Task 3).
- Produces: `TurnController.run_turn(...) -> TurnOutcome` for all harnesses; structured runs pass `output_type=[<resolved schema>, DeferredToolRequests]` to every `agent.run` round. (`TurnController.__init__`'s `output_type` parameter and its `_output_type_dict`/`_structured_type` resolution were wired in Task 3 step 5.)

- [ ] **Step 1: Controller changes**

In `src/marim_harness/runtime/controller.py`:

1. Imports (top of file): add

```python
from .outcome import TurnOutcome
```

(`StructuredDict` and the `__init__` parameter/attributes already landed in Task 3 step 5 — verify they are present before proceeding.)

2. New helper next to the other pure helpers (keeps `_run_with_approval`'s complexity flat):

```python
    def _run_output_type(self) -> list[Any] | None:
        """The per-run output override for structured harnesses, else None.
        Always carries the deferred arm: an override without
        DeferredToolRequests raises UserError at the first gated tool call
        instead of deferring it."""
        if self._structured_type is None:
            return None
        return [self._structured_type, DeferredToolRequests]
```

3. `_run_with_approval`: pass the override to BOTH `agent.run` call sites:

```python
                result = await self.agent.run(
                    user_prompt,
                    toolsets=toolsets,
                    message_history=None,
                    deps=self.deps,
                    defer_tools=self._defer_enabled,
                    deferred_tool_results=deferred_results,
                    output_type=self._run_output_type(),
                    usage_limits=self._turn_usage_limits(),
                    model_settings=_turn_model_settings(...),
                    retries=2,
                )
```

and on the retry:

```python
                        result = await self.agent.run(
                            None,
                            toolsets=toolsets,
                            message_history=None,
                            deps=self.deps,
                            defer_tools=self._defer_enabled,
                            deferred_tool_results=deferred_results,
                            output_type=self._run_output_type(),
                            usage_limits=self._turn_usage_limits(),
                            model_settings=retry_settings,
                        )
```

4. `_finish_turn`: replace the final `return str(output)` with:

```python
        if isinstance(output, str):
            return TurnOutcome(subtype="success", result=output,
                               structured_output=None, errors=None)
        # DeferredToolRequests reaching here is the ask-mode-no-UI case
        # (deferrals returned as-is); preserve the historical str() shape.
        if not isinstance(output, DeferredToolRequests):
            return TurnOutcome(subtype="success", result=None,
                               structured_output=output, errors=None)
        return TurnOutcome(subtype="success", result=str(output),
                           structured_output=None, errors=None)
```

5. `run_turn` signature: `-> TurnOutcome` (docstring updated in harness.py, step 2).

6. `_finish_turn` docstring: replace "Turns end in text." with "Turns end in a TurnOutcome."

- [ ] **Step 2: Harness wiring**

In `src/marim_harness/runtime/harness.py`:

1. The `output_type=cfg.output_type` kwarg on the TurnController construction already landed in Task 3 step 5 — verify it is present before proceeding.

2. `Harness.run_turn` docstring — replace the return line:

```
        Returns:
            The terminal TurnOutcome: subtype, final text (result), validated
            structured data (structured_output, when built with_output_type),
            and failure detail.
```

and the return annotation `-> TurnOutcome` (import from `.outcome` under `TYPE_CHECKING` if needed for the annotation; `from __future__ import annotations` is already in effect in this module).

- [ ] **Step 3: Interface callsites**

In `src/marim_harness/interfaces/cli/headless.py` (~line 117):

```python
        outcome = await harness.run_turn(prompt, event_stream_handler=handler)
        output = outcome.result or ""
```

(everything below keeps using `output`). The TUI (`interfaces/tui/app.py:522`) discards the return value — no change needed.

- [ ] **Step 4: Write the structured-turn integration tests**

Create `tests/test_structured_turn.py`:

```python
"""Structured output for embedder turns (HarnessBuilder.with_output_type)."""

import pytest
from pydantic import BaseModel
from pydantic_ai.models.test import TestModel

from marim_harness import HarnessBuilder

pytestmark = pytest.mark.anyio


class Point(BaseModel):
    a: int


async def _run(harness, prompt):
    await harness.connect()
    try:
        return await harness.run_turn(prompt)
    finally:
        await harness.aclose()


async def test_plain_turn_outcome_shape(tmp_path):
    h = HarnessBuilder(workspace=tmp_path,
                       model=TestModel(custom_output_text="hello there")).build()
    outcome = await _run(h, "say hi")
    assert outcome.subtype == "success"
    assert outcome.result == "hello there"
    assert outcome.structured_output is None
    assert outcome.errors is None


async def test_basemodel_structured_turn(tmp_path):
    h = (HarnessBuilder(workspace=tmp_path,
                        model=TestModel(custom_output_args={"a": 3}))
         .with_output_type(Point)
         .build())
    outcome = await _run(h, "give me the point")
    assert outcome.subtype == "success"
    assert outcome.structured_output == Point(a=3)


async def test_structured_turn_survives_approval_round(tmp_path):
    """A gated tool auto-approved mid-turn does not disturb the structured
    result: the per-run output override must ride the continuation round too."""
    from marim_harness import HarnessBuilder

    def touch_file(ctx, path: str) -> str:
        return "touched"

    h = (HarnessBuilder(workspace=tmp_path,
                        model=TestModel(custom_toolcall_name="touch_file",
                                        custom_toolcall_args={"path": "x.txt"},
                                        custom_output_args={"a": 7}))
         .with_tool(touch_file, requires_approval=True)
         .with_output_type(Point)
         .build())
    outcome = await _run(h, "touch the file, then report")
    assert outcome.subtype == "success"
    assert outcome.structured_output == Point(a=7)
```

- [ ] **Step 5: Run the new tests, then migrate the suite**

Run: `uv run pytest --no-cov tests/test_structured_turn.py -v`
Expected: 3 PASS.

Then the migration sweep — the 13 files under "Modify (migration)" above contain every `x = await ….run_turn(…)` callsite. The mechanical fix: wherever the captured value is used as a string, take `.result`:

```python
# before
out = await harness.run_turn("...")
assert "something" in out
# after
out = await harness.run_turn("...")
assert "something" in (out.result or "")
```

Callsites that discard the result (`await harness.run_turn(...)` bare) need no change. Work through the suite until green:

Run: `uv run pytest -x -q`
Expected: full suite green (fix file by file; `.result` is the only migration).

- [ ] **Step 6: Lint + type-check, commit**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean (fix any `result | None` typing fallout in tests by asserting or `or ""`).

```bash
git add -A
git commit -m "feat(runtime): run_turn returns TurnOutcome; per-run structured output override"
```

---

### Task 5: BaseModel exhaustion → error outcome

**Files:**
- Modify: `src/marim_harness/runtime/controller.py`
- Modify: `tests/test_structured_turn.py`

**Interfaces:**
- Consumes: `TurnOutcome` subtypes (Task 1), the controller's structured wiring (Task 4).
- Produces: when a BaseModel-schema turn exhausts pydantic-ai's validation retries, `run_turn` returns `TurnOutcome(subtype="error_max_structured_output_retries", result=None, structured_output=None, errors=[...])` instead of raising `UnexpectedModelBehavior`. Infra failures still raise.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_structured_turn.py`:

```python
async def test_basemodel_exhaustion_returns_error_subtype(tmp_path):
    """custom_output_args violates the model on every attempt, so pydantic-ai
    exhausts its retries and the turn ends as an error outcome, not a raise."""
    h = (HarnessBuilder(workspace=tmp_path,
                        model=TestModel(custom_output_args={"a": "not-an-int"}))
         .with_output_type(Point)
         .build())
    outcome = await _run(h, "give me the point")
    assert outcome.subtype == "error_max_structured_output_retries"
    assert outcome.structured_output is None
    assert outcome.errors
```

- [ ] **Step 2: Run it and capture the real exception shape**

Run: `uv run pytest --no-cov tests/test_structured_turn.py::test_basemodel_exhaustion_returns_error_subtype -v`
Expected: FAIL — the test raises. Read the raised exception: confirm it is `pydantic_ai.exceptions.UnexpectedModelBehavior` and note its `__cause__` (a pydantic `ValidationError` or a nested chain) — the classifier in step 3 must match what you observe.

- [ ] **Step 3: Implement the classifier and the outcome path**

In `src/marim_harness/runtime/controller.py`:

1. Helper near `_run_output_type`:

```python
    def _is_structured_exhaustion(self, exc: BaseException) -> bool:
        """Validation-exhaustion of a structured turn: pydantic-ai retried the
        model's output against the schema and gave up. Only meaningful when a
        structured output is active; every other UnexpectedModelBehavior keeps
        the generic failure path."""
        from pydantic import ValidationError
        from pydantic_ai.exceptions import UnexpectedModelBehavior

        if self._structured_type is None or not isinstance(exc, UnexpectedModelBehavior):
            return False
        cause: BaseException | None = exc.__cause__
        while cause is not None:
            if isinstance(cause, ValidationError):
                return True
            cause = cause.__cause__
        return "Exceeded retries" in str(exc)
```

(Adjust the `"Exceeded retries"` string only if step 2 showed a different message and no ValidationError in the chain.)

2. In `_run_with_approval`'s `while True`, split the exception handling — the new clause comes FIRST (it must win over the generic `except Exception`):

```python
            except Exception as exc:
                if self._is_structured_exhaustion(exc):
                    # Validation exhaustion is a terminal turn result, not an
                    # infra failure: bank the spend, persist the (resumable)
                    # history, and report through the outcome. No error note —
                    # there is nothing the model can act on next turn.
                    self.session.add_usage(round_usage)
                    self._reclaim_undelivered_steers()
                    self._clear_stash()
                    if self.session.history:
                        await self._flush_resumable(deadline=0.5)
                        await asyncio.to_thread(self.session.persist)
                    return TurnOutcome(
                        subtype="error_max_structured_output_retries",
                        result=None,
                        structured_output=None,
                        errors=[str(exc)],
                    )
                retry = await self._handle_run_failure(
                    exc, captured, resumable, deferred_results, round_usage, retried
                )
                ...
```

(the existing `_handle_run_failure` call and everything after it stay exactly as they are — only the new `if` block is inserted at the top of the `except Exception` body).

- [ ] **Step 4: Run the feature tests**

Run: `uv run pytest --no-cov tests/test_structured_turn.py -v`
Expected: all PASS.

- [ ] **Step 5: Full suite + lint, commit**

Run: `uv run pytest -x -q && uv run ruff check src tests && uv run pyright`
Expected: green.

```bash
git add src/marim_harness/runtime/controller.py tests/test_structured_turn.py
git commit -m "feat(runtime): BaseModel schema exhaustion surfaces as error outcome"
```

---

### Task 6: dict-schema tier — post-turn validation + one corrective turn

**Files:**
- Modify: `src/marim_harness/runtime/controller.py`
- Modify: `tests/test_structured_turn.py`

**Interfaces:**
- Consumes: `validate_dict_output` (Task 2), the controller's structured wiring (Task 4).
- Produces: dict-schema turns are validated after completion; on failure ONE corrective round runs through the same `_run_with_approval` machinery; a second failure yields `TurnOutcome(subtype="error_max_structured_output_retries", errors=[validation errors])`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_structured_turn.py`:

```python
DICT_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
}


async def test_dict_schema_happy_path(tmp_path):
    h = (HarnessBuilder(workspace=tmp_path,
                        model=TestModel(custom_output_args={"a": 5}))
         .with_output_type(DICT_SCHEMA)
         .build())
    outcome = await _run(h, "give me a")
    assert outcome.subtype == "success"
    assert outcome.structured_output == {"a": 5}


async def test_dict_schema_corrective_retry_succeeds(tmp_path):
    """First response violates the schema; the corrective round fixes it."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    state = {"calls": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        state["calls"] += 1
        if state["calls"] == 1:
            args = {"a": "not-an-int"}
        else:
            args = {"a": 1}
        # StructuredDict output rides the output tool; its default name is
        # 'final_result' in pydantic-ai (pydantic_ai/_output.py).
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=args)])

    h = (HarnessBuilder(workspace=tmp_path, model=FunctionModel(fn))
         .with_output_type(DICT_SCHEMA)
         .build())
    outcome = await _run(h, "give me a")
    assert outcome.subtype == "success"
    assert outcome.structured_output == {"a": 1}
    assert state["calls"] == 2


async def test_dict_schema_exhaustion(tmp_path):
    """TestModel emits the same invalid object every round, so the single
    corrective attempt also fails and the turn ends as an error outcome."""
    h = (HarnessBuilder(workspace=tmp_path,
                        model=TestModel(custom_output_args={"a": "bad"}))
         .with_output_type(DICT_SCHEMA)
         .build())
    outcome = await _run(h, "give me a")
    assert outcome.subtype == "error_max_structured_output_retries"
    assert outcome.structured_output is None
    assert outcome.errors
    assert any("a" in e for e in outcome.errors)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_structured_turn.py -k dict_schema -v`
Expected: happy path may already pass; the corrective and exhaustion tests FAIL.

- [ ] **Step 3: Implement the corrective loop**

In `src/marim_harness/runtime/controller.py`:

1. Import at the top:

```python
from .structured import validate_dict_output
```

2. New method next to `_run_output_type` (extracted so `run_turn` stays flat — C901):

```python
    async def _correct_dict_output(
        self,
        outcome: TurnOutcome,
        toolsets,
        event_stream_handler,
    ) -> TurnOutcome:
        """Dict-schema enforcement after a clean turn: validate the emitted
        object, and if it fails, run ONE corrective round through the same
        approval machinery (its own persist/flush/rollback comes with it).
        A second failure reports the validation errors through the outcome.
        BaseModel schemas never reach here — pydantic-ai already validated
        them in-run."""
        if self._output_type_dict is None or outcome.subtype != "success":
            return outcome
        errors = validate_dict_output(outcome.structured_output, self._output_type_dict)
        if not errors:
            return outcome
        corrective = (
            "Your previous response failed schema validation:\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\nRespond again with ONLY a JSON object matching the schema."
        )
        resumable = list(self.session.history)
        retry_outcome = await self._run_with_approval(
            corrective, deferred_results=None, toolsets=toolsets,
            event_stream_handler=event_stream_handler, resumable=resumable,
        )
        if retry_outcome.subtype != "success":
            return retry_outcome
        errors = validate_dict_output(retry_outcome.structured_output,
                                      self._output_type_dict)
        if errors:
            return TurnOutcome(
                subtype="error_max_structured_output_retries",
                result=retry_outcome.result,
                structured_output=None,
                errors=errors,
            )
        return retry_outcome
```

3. In `run_turn`, wrap the `_run_with_approval` call (currently `return await self._run_with_approval(...)`):

```python
            outcome = await self._run_with_approval(
                user_prompt, deferred_results=None, toolsets=toolsets,
                event_stream_handler=event_stream_handler, resumable=resumable,
            )
            return await self._correct_dict_output(outcome, toolsets, event_stream_handler)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_structured_turn.py -v`
Expected: all PASS. If the FunctionModel scripted `ToolCallPart` is rejected by this pydantic-ai version, adjust the part shape to what TestModel emits for structured output (inspect `r.all_messages()` in a scratch run) — the semantics (invalid first call, valid second) must stay.

- [ ] **Step 5: Full suite + lint, commit**

Run: `uv run pytest -x -q && uv run ruff check src tests && uv run pyright`
Expected: green.

```bash
git add src/marim_harness/runtime/controller.py tests/test_structured_turn.py
git commit -m "feat(runtime): dict-schema validation with one corrective turn"
```

---

### Task 7: docs, CHANGELOG, version bump, final CI-order gate

**Files:**
- Modify: `docs/sdk/turns.md`, `docs/sdk/builder.md`, `docs/embedding.md`
- Modify: `CHANGELOG.md`, `pyproject.toml`

**Interfaces:**
- Consumes: the implemented feature (Tasks 1–6).
- Produces: user-facing documentation and the 0.5.0 release metadata.

- [ ] **Step 1: `docs/sdk/turns.md` — add a "Structured output" section**

Append after the "Steering" section:

````markdown
## Structured output

`with_output_type` makes every turn end in validated structured data instead
of free text — the schema is a property of the harness (Claude-Agent-SDK
style), and tools plus the approval loop work exactly as before mid-turn:

```python
from pydantic import BaseModel
from marim_harness import HarnessBuilder


class Audit(BaseModel):
    summary: str
    files_changed: list[str]


harness = (HarnessBuilder(workspace=Path("."), model="anthropic:claude-sonnet-4-6")
           .with_output_type(Audit)          # or an object-rooted JSON Schema dict
           .build())

outcome = await harness.run_turn("audit the last commit")
assert outcome.subtype == "success"
audit: Audit = outcome.structured_output
```

`run_turn` always returns a `TurnOutcome` (even without a schema — then
`structured_output` is `None` and `result` carries the text):

| Field | Meaning |
|---|---|
| `subtype` | `"success"` · `"error_max_structured_output_retries"` · `"error_during_execution"` (reserved, not emitted) |
| `result` | final assistant text; `None` when the run ended in pure structured output |
| `structured_output` | the validated model instance (BaseModel schema) or dict (JSON Schema) |
| `errors` | failure detail on error subtypes |

Enforcement differs by schema kind: a `BaseModel` is validated by
pydantic-ai inside the run (with retries); a JSON Schema dict constrains
generation provider-side and is checked after the turn — one corrective
round runs if it fails. Infra/provider errors still raise; only schema
failures become error outcomes. JSON Schemas must be object-rooted.
````

- [ ] **Step 2: `docs/sdk/builder.md` — document the setter**

Add a row to the method table (in the composition block, alphabetical neighborhood):

```markdown
| `with_output_type(schema)` | Validate every turn's output against a pydantic model or JSON Schema — see Turns, "Structured output" |
```

- [ ] **Step 3: `docs/embedding.md` — one paragraph**

In the composition section, add:

```markdown
### Structured output

`with_output_type(schema)` (a pydantic `BaseModel` subclass or an
object-rooted JSON Schema dict) makes `run_turn` return a `TurnOutcome`
whose `structured_output` is the validated object — retry-until-valid,
tools and approval unaffected mid-run. Every `run_turn` returns a
`TurnOutcome`; plain harnesses get the text in `.result`.
```

- [ ] **Step 4: CHANGELOG + version bump**

In `CHANGELOG.md`, replace `## [Unreleased]` with:

```markdown
## [Unreleased]

## [0.5.0] - 2026-08-29

### Added

- Structured output for embedder turns: `HarnessBuilder.with_output_type`
  accepts a pydantic `BaseModel` subclass or an object-rooted JSON Schema
  dict; turns validate against it (BaseModel: pydantic-ai in-run retries;
  dict: post-turn validation with one corrective round) and report through
  the new `TurnOutcome` subtypes, mirroring the Claude Agent SDK's
  `ResultMessage`.

### Changed (breaking)

- `Harness.run_turn` now returns a `TurnOutcome` instead of `str` — the
  final text moved to `outcome.result` (`outcome.structured_output` carries
  validated data when `with_output_type` is set). Migration: replace
  `out = await harness.run_turn(...)` with `out = (await
  harness.run_turn(...)).result` where you used the text.
```

In `pyproject.toml`, bump: `version = "0.5.0"`.

- [ ] **Step 5: Final gate — CI order**

Run: `uv run ruff check src tests`
Run: `uv run pyright`
Run: `uv run pytest`
Expected: all green. Then the docs check:

Run: `uv run python docs.py`
Expected: no broken links / render errors.

- [ ] **Step 6: Commit**

```bash
git add docs/sdk/turns.md docs/sdk/builder.md docs/embedding.md CHANGELOG.md pyproject.toml
git commit -m "docs+release: structured turn output, v0.5.0"
```

---

## Self-Review Notes

- Spec coverage: §1 API → Tasks 1, 3, 4; §2 mechanics → Task 4; §3 enforcement tiers → Tasks 2, 5, 6; §4 error handling → Tasks 5, 6 (raise-vs-outcome split in both); §5 components → tasks map 1:1; §6 testing → every task is TDD, integration tests per `docs/sdk/testing.md`; §7 rollout → Task 7.
- `error_during_execution` is defined (Task 1) and documented (Task 7) but never emitted — per spec.
- Phase-2 items (spawn_agent schema, claude-cli fallback, CLI flag) are deliberately absent.
