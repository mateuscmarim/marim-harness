# CodeMode Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate pydantic-ai-harness's `CodeMode` capability into marim's main agent behind a `MARIM_CODEMODE` flag — ungated tools sandboxed behind one `run_code` tool, gated tools untouched — with live TUI rendering of nested sandbox calls and a headless A/B benchmark deciding keep-or-delete.

**Architecture:** One new deletable module (`runtime/code_mode.py`) holds the tool selector, the capability factory, the nested-call observer, and the metadata parser. Wiring is a conditional `capabilities` append in `build_collaborators`, a `HarnessConfig` field + builder knob, and a bootstrap env read. TUI rendering extends the existing `ToolCallWidget`/`StreamRenderer` with a code-highlighted body and a nested mini-transcript, fed live by a new optional `Deps.ui` callback and on restore by persisted `ToolReturnPart.metadata`.

**Tech Stack:** pydantic-ai 2.8 capabilities API, `pydantic-ai-harness[code-mode]==0.6.0` (Monty sandbox), Textual/Rich TUI, pytest + anyio.

**Spec:** `docs/superpowers/specs/2026-07-10-codemode-experiment-design.md` (approved). Re-verify upstream facts against the pinned 0.6.0 tag when Task 2 installs it — the spec was verified against `master` @ 2026-07-10.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff`, `uv sync`. Never bare `python`/`pip`/`pytest`.
- Python `>=3.10` syntax only (no 3.11+ features like `Self` from typing without `typing_extensions`).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (imports sorted).
- Dependency pin is EXACT: `pydantic-ai-harness[code-mode]==0.6.0` (0.x minors break; bumps are deliberate).
- Every test touching the optional dependency starts with `pytest.importorskip("pydantic_monty")` so the suite passes on installs without the extra.
- CI order before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- All experiment tests live in `tests/test_codemode.py` (single deletable file). Async tests are plain `async def test_…` under `pytestmark = pytest.mark.anyio`.
- Never run a paid model. The benchmark uses `MARIM_PROVIDER=local` (LM Studio) only.
- Preserve the long "why" comments near any code you edit; write new comments in the same style.

---

### Task 1: Config flag — `ModelConfig.code_mode` + `MARIM_CODEMODE` env read

**Files:**
- Modify: `src/marim_harness/config/model.py` (field ~line 116, `_common_kwargs` ~line 206)
- Test: `tests/test_codemode.py` (new file)

**Interfaces:**
- Produces: `ModelConfig.code_mode: bool` (default `False`); `load_config()` reads `MARIM_CODEMODE`. Task 3's bootstrap passthrough consumes `cfg.code_mode`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_codemode.py`:

```python
"""Tests for the CodeMode experiment (spec:
docs/superpowers/specs/2026-07-10-codemode-experiment-design.md).

Everything about the experiment tests in this one file so deleting the
experiment deletes its tests wholesale. Tests that need the optional
`codemode` extra importorskip on pydantic_monty.
"""

import pytest

from marim_harness.config.model import load_config

pytestmark = pytest.mark.anyio  # async tests below use plain `async def`


# --- Task 1: env flag -------------------------------------------------------


def test_code_mode_env_flag_defaults_off(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_CODEMODE", raising=False)
    assert load_config().code_mode is False


def test_code_mode_env_flag_on(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_CODEMODE", "1")
    assert load_config().code_mode is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_codemode.py -v`
Expected: FAIL — `AttributeError: 'ModelConfig' object has no attribute 'code_mode'` (or TypeError on unknown kwarg).

- [ ] **Step 3: Add the field and env read**

In `src/marim_harness/config/model.py`, after the `job_tool_combined: bool = False` field (~line 116), add:

```python
    # CodeMode experiment: sandbox every ungated tool behind one `run_code`
    # tool (model writes Python; the Monty sandbox calls the tools). Gated
    # tools stay native. Requires the `codemode` extra; off by default.
    # Spec: docs/superpowers/specs/2026-07-10-codemode-experiment-design.md.
    code_mode: bool = False
```

In `_common_kwargs()`'s returned dict, after `job_tool_combined=_bool_env("MARIM_JOB_TOOL_COMBINED", False),` add:

```python
        code_mode=_bool_env("MARIM_CODEMODE", False),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_codemode.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/model.py tests/test_codemode.py
git commit -m "feat(codemode): MARIM_CODEMODE flag on ModelConfig (experiment)"
```

---

### Task 2: Dependency + `runtime/code_mode.py` core (selector, factory, metadata parser)

**Files:**
- Modify: `pyproject.toml` (`[project.optional-dependencies]` ~line 45, dev group ~line 67)
- Create: `src/marim_harness/runtime/code_mode.py`
- Test: `tests/test_codemode.py`

**Interfaces:**
- Produces:
  - `ungated_selector(ctx, tool_def) -> bool` — CodeMode tool selector.
  - `build_code_mode() -> AbstractCapability` — lazy-imports upstream; raises `RuntimeError` with install hint when the extra is missing.
  - `NestedToolCall` frozen dataclass: `parent_call_id: str, call_id: str, tool_name: str, args: dict, status: str, result_preview: str = ""` (status ∈ `pending|done|failed|denied`).
  - `nested_from_metadata(metadata: object) -> list[NestedToolCall]` — lenient parser for persisted `run_code` return metadata.
- Consumed by Tasks 3 (factory), 4 (observer lives in this module), 5 (TUI).

- [ ] **Step 1: Add the extra and dev-group entry**

In `pyproject.toml` `[project.optional-dependencies]`, after the `serve = [...]` line:

```toml
# CodeMode experiment: sandbox ungated tools behind a single `run_code` tool.
# EXACT pin — 0.x minors break; bumps are deliberate. Delete with the experiment.
codemode = ["pydantic-ai-harness[code-mode]==0.6.0"]
```

In `[dependency-groups] dev = [...]`, after the serve-test entries:

```toml
    # The CodeMode experiment tests need these even though they live in the
    # optional `codemode` extra, so plain `uv sync` keeps CI exercising them.
    # Tests importorskip on pydantic_monty: if this dep ever breaks a CI leg
    # (e.g. no monty wheel for that Python), drop this line — tests skip.
    "pydantic-ai-harness[code-mode]==0.6.0",
```

Run: `uv sync`
Expected: resolves and installs `pydantic-ai-harness` 0.6.0 + `pydantic-monty`. If resolution fails on the exact pin, STOP and report — do not loosen the pin.

Then verify the upstream facts the spec pinned against `master` still hold on 0.6.0:

```bash
uv run python -c "
from pydantic_ai_harness import CodeMode
import inspect, pydantic_ai_harness.code_mode._toolset as t
src = inspect.getsource(t)
assert \"f'{parent_id}__{call_counter}'\" in src, 'nested id scheme changed'
assert \"'code_mode': True\" in src and \"'tool_calls'\" in src, 'metadata shape changed'
assert \"'code_arg_name': 'code'\" in src, 'code_arg metadata changed'
print('upstream facts hold on the pinned release')
"
```
Expected: `upstream facts hold on the pinned release`. If an assert fires, STOP and report the drift before continuing.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_codemode.py`:

```python
# --- Task 2: selector, factory, metadata parser ------------------------------

from marim_harness.runtime.code_mode import (  # noqa: E402
    NestedToolCall,
    build_code_mode,
    nested_from_metadata,
    ungated_selector,
)


def _tool_def(name: str, kind: str = "function", metadata: dict | None = None):
    from pydantic_ai.tools import ToolDefinition

    return ToolDefinition(name=name, kind=kind, metadata=metadata)


def test_selector_sandboxes_only_plain_function_tools():
    """The selector keys on ToolDefinition.kind: pydantic-ai sets
    kind='unapproved' for every requires_approval tool (including embedder
    custom tools), so gated tools can never be sandboxed — no name list to
    drift. The names.py sets are asserted as a belt-and-braces check."""
    from marim_harness.tools.names import GATED_TOOLS, LSP_TOOLS, READ_TOOLS

    for name in GATED_TOOLS:
        assert ungated_selector(None, _tool_def(name, kind="unapproved")) is False
    for name in READ_TOOLS | LSP_TOOLS:
        assert ungated_selector(None, _tool_def(name)) is True
    # Non-function kinds (output tools, deferred) stay out of the sandbox too.
    assert ungated_selector(None, _tool_def("final_result", kind="output")) is False


def test_build_code_mode_fails_fast_without_extra(monkeypatch):
    import sys

    # None in sys.modules makes `from pydantic_ai_harness import …` raise
    # ImportError — simulating an install without the codemode extra.
    monkeypatch.setitem(sys.modules, "pydantic_ai_harness", None)
    with pytest.raises(RuntimeError, match="uv sync --extra codemode"):
        build_code_mode()


def test_build_code_mode_returns_capability():
    pytest.importorskip("pydantic_monty")
    cap = build_code_mode()
    assert type(cap).__name__ == "CodeMode"
    assert cap.tools is ungated_selector


def test_nested_from_metadata_parses_dict_form():
    """The persisted form: ToolCallPart/ToolReturnPart serialized to dicts by
    the message type adapter on session save/load."""
    meta = {
        "code_mode": True,
        "tool_calls": {
            "abc__1": {"tool_name": "read_file", "args": {"path": "x.py"},
                       "tool_call_id": "abc__1"},
            "abc__2": {"tool_name": "grep", "args": '{"pattern": "foo"}',
                       "tool_call_id": "abc__2"},
        },
        "tool_returns": {
            "abc__1": {"tool_name": "read_file", "content": "line1\nline2",
                       "tool_call_id": "abc__1"},
            # abc__2 has no return: the sandbox raised at that call site.
        },
    }
    calls = nested_from_metadata(meta)
    assert [c.call_id for c in calls] == ["abc__1", "abc__2"]
    first, second = calls
    assert first == NestedToolCall(
        parent_call_id="abc", call_id="abc__1", tool_name="read_file",
        args={"path": "x.py"}, status="done", result_preview="line1 line2",
    )
    assert second.status == "failed"
    assert second.args == {"pattern": "foo"}  # JSON-string args decoded


def test_nested_from_metadata_is_lenient():
    assert nested_from_metadata(None) == []
    assert nested_from_metadata({"code_mode": False, "tool_calls": {}}) == []
    assert nested_from_metadata({"code_mode": True, "tool_calls": "garbage"}) == []
    # A malformed entry is skipped, not fatal.
    meta = {"code_mode": True,
            "tool_calls": {"a__1": {"no_name": True},
                           "a__2": {"tool_name": "glob", "args": {}}},
            "tool_returns": {}}
    assert [c.tool_name for c in nested_from_metadata(meta)] == ["glob"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_codemode.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.runtime.code_mode'`.

- [ ] **Step 4: Create the module**

Create `src/marim_harness/runtime/code_mode.py`:

```python
"""The CodeMode experiment: sandbox ungated tools behind one ``run_code`` tool.

Everything marim-side for the experiment lives here — the tool selector, the
capability factory, the TUI observer (Task 4 adds it), and the metadata
parser — so deleting the experiment is: remove this module, the ``codemode``
extra, the ``code_mode`` config field/knob/env read, the ``on_codemode_call``
UI callback, the TUI branches, ``tests/test_codemode.py``, and
``scripts/bench_codemode.py``. Grep for ``code_mode``/``MARIM_CODEMODE``.

Spec: docs/superpowers/specs/2026-07-10-codemode-experiment-design.md.
Pinned upstream: pydantic-ai-harness[code-mode]==0.6.0 (exact — its 0.x
minors break; the nested-id and metadata shapes below are keyed to it and
guarded by tests in tests/test_codemode.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.tools import RunContext, ToolDefinition

# Cap on the one-line result preview shown per nested call in the TUI.
_PREVIEW_CAP = 120


def ungated_selector(ctx: "RunContext[Any] | None", tool_def: "ToolDefinition") -> bool:
    """Sandbox a tool iff it is a plain function tool.

    pydantic-ai sets ``ToolDefinition.kind`` to ``'unapproved'`` for every
    tool registered with ``requires_approval=True`` (see Tool.tool_def), so
    this single check excludes ALL gated tools — builtins, forge, and any
    embedder custom tool — with no name list to drift. It also excludes
    output tools and externally-deferred tools (non-'function' kinds).
    Gated tools MUST stay native: the sandbox cannot round-trip an
    ApprovalRequired (upstream converts it to an error), so a sandboxed
    gated tool would fail even in auto mode.
    """
    return tool_def.kind == "function"


def build_code_mode() -> "AbstractCapability[Any]":
    """Construct the upstream CodeMode capability over the ungated selector.

    Lazy import so default installs never import the optional package; a
    missing extra fails fast at build time (never silently off) with the
    install command in the message.
    """
    try:
        from pydantic_ai_harness import CodeMode
    except ImportError as exc:
        raise RuntimeError(
            "MARIM_CODEMODE is enabled but the codemode extra is not "
            "installed. Install it with: uv sync --extra codemode"
        ) from exc
    # Fully closed sandbox: no mount, no os_access — marim's tool functions
    # run on the host as always; the sandbox only orchestrates calls.
    # dynamic_catalog stays False (default): marim's ungated toolset is fixed
    # at build time, so the static run_code description is cache-friendlier.
    return CodeMode(tools=ungated_selector)


@dataclass(frozen=True)
class NestedToolCall:
    """One tool call executed inside a ``run_code`` sandbox, as shown in the
    TUI: emitted live by CodeModeObserver (status 'pending' then a settle),
    or rebuilt from persisted metadata on session restore."""

    parent_call_id: str
    call_id: str
    tool_name: str
    args: dict
    status: str  # 'pending' | 'done' | 'failed' | 'denied'
    result_preview: str = ""


def _preview(value: object) -> str:
    """A single-line, capped preview of a nested call's result."""
    text = " ".join(str(value).split())
    return text if len(text) <= _PREVIEW_CAP else text[: _PREVIEW_CAP - 1] + "…"


def nested_from_metadata(metadata: object) -> list[NestedToolCall]:
    """Rebuild the nested-call list from a ``run_code`` ToolReturnPart's
    metadata (upstream shape: ``{'code_mode': True, 'tool_calls': {id: part},
    'tool_returns': {id: part}}``).

    Lenient by design — this is display sugar, not load-bearing logic: an
    unknown shape yields ``[]`` and a malformed entry is skipped. Handles
    both live part objects (same-process render) and their dict form (a
    persisted session reloaded through the message type adapter).
    """
    if not isinstance(metadata, dict) or not metadata.get("code_mode"):
        return []
    calls = metadata.get("tool_calls")
    if not isinstance(calls, dict):
        return []
    returns = metadata.get("tool_returns")
    if not isinstance(returns, dict):
        returns = {}

    def _get(obj: object, key: str, default: object = None) -> object:
        return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

    out: list[NestedToolCall] = []
    for call_id, call in calls.items():
        name = _get(call, "tool_name")
        if not isinstance(name, str):
            continue
        args = _get(call, "args")
        if isinstance(args, str):
            # A ToolCallPart may carry args as a JSON string; decode best-effort.
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        ret = returns.get(call_id)
        if ret is None:
            # No return recorded: the sandbox raised at this call site.
            status, preview = "failed", ""
        else:
            outcome = _get(ret, "outcome")
            status = outcome if outcome in ("failed", "denied") else "done"
            preview = _preview(_get(ret, "content", ""))
        out.append(NestedToolCall(
            parent_call_id=str(call_id).rpartition("__")[0],
            call_id=str(call_id), tool_name=name, args=args,
            status=str(status), result_preview=preview,
        ))
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_codemode.py -v`
Expected: all PASS (7 tests so far).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/marim_harness/runtime/code_mode.py tests/test_codemode.py
git commit -m "feat(codemode): codemode extra + selector/factory/metadata module (experiment)"
```

---

### Task 3: Harness wiring — `HarnessConfig.code_mode`, capability append, builder knob, bootstrap passthrough

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (HarnessConfig ~line 116, capabilities list ~line 268)
- Modify: `src/marim_harness/runtime/builder.py` (new knob after `with_lsp`, ~line 115)
- Modify: `src/marim_harness/runtime/bootstrap.py` (`.with_config_overrides(...)` ~line 129)
- Test: `tests/test_codemode.py`

**Interfaces:**
- Consumes: `build_code_mode` (Task 2), `ModelConfig.code_mode` (Task 1).
- Produces: `HarnessConfig.code_mode: bool = False`; `HarnessBuilder.with_code_mode(*, enabled: bool = True) -> HarnessBuilder`. Task 4 extends the same capabilities branch with the observer.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_codemode.py`:

```python
# --- Task 3: harness wiring ---------------------------------------------------


async def test_code_mode_wiring_sandboxes_only_ungated(tmp_path, monkeypatch):
    """Flag on: run_code replaces the ungated tools in what the model sees;
    gated tools stay native so the approval loop is untouched."""
    pytest.importorskip("pydantic_monty")
    from pydantic_ai.models.test import TestModel

    from marim_harness import HarnessBuilder
    from marim_harness.tools.names import GATED_TOOLS

    # with_defaults() keeps global instructions on; point the config home at
    # tmp so the test never reads the developer's real marim config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    m = TestModel(call_tools=[])
    harness = (
        HarnessBuilder(workspace=tmp_path, model=m)
        .with_defaults()
        .with_code_mode()
        .build()
    )
    await harness.run_turn("hi")
    names = {t.name for t in m.last_model_request_parameters.function_tools}
    assert "run_code" in names
    assert "read_file" not in names  # sandboxed away
    assert "grep" not in names
    assert GATED_TOOLS <= names  # write_file/edit_file/bash still native


async def test_code_mode_off_by_default(tmp_path, monkeypatch):
    from pydantic_ai.models.test import TestModel

    from marim_harness import HarnessBuilder

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    m = TestModel(call_tools=[])
    harness = HarnessBuilder(workspace=tmp_path, model=m).with_defaults().build()
    await harness.run_turn("hi")
    names = {t.name for t in m.last_model_request_parameters.function_tools}
    assert "run_code" not in names
    assert "read_file" in names


async def test_bootstrap_reads_codemode_env(tmp_path, monkeypatch):
    """MARIM_CODEMODE=1 threads env → ModelConfig → bootstrap → HarnessConfig →
    the capability, end to end through build_harness."""
    pytest.importorskip("pydantic_monty")
    from pydantic_ai.models.test import TestModel

    from marim_harness.config.model import ModelSource
    from marim_harness.runtime import bootstrap
    from marim_harness.runtime.permissions import Mode

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_CODEMODE", "1")
    m = TestModel(call_tools=[])
    monkeypatch.setattr(ModelSource, "build", lambda self, mid: m)
    monkeypatch.setattr(bootstrap, "make_summarizer", lambda model: None)
    monkeypatch.setattr(bootstrap, "make_titler", lambda model: None)

    harness = bootstrap.build_harness(tmp_path / "ws", mode=Mode.auto)
    await harness.run_turn("hi")
    names = {t.name for t in m.last_model_request_parameters.function_tools}
    assert "run_code" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_codemode.py -v -k "wiring or off_by_default or bootstrap_reads"`
Expected: `test_code_mode_wiring…` and `test_bootstrap_reads…` FAIL (`AttributeError: … no attribute 'with_code_mode'` / `run_code not in names`); `test_code_mode_off_by_default` may already PASS.

- [ ] **Step 3: Add the HarnessConfig field**

In `src/marim_harness/runtime/harness.py`, after the `lsp_tools_enabled`-adjacent fields — concretely after `lsp_enabled: bool = True` (~line 118) — add:

```python
    # CodeMode experiment: when True, build_collaborators appends the CodeMode
    # capability (every ungated tool sandboxed behind one `run_code` tool) and
    # its TUI observer. Requires the `codemode` extra — the build fails fast
    # with an install hint when it's missing, never silently off. Gated tools
    # are never sandboxed (see runtime/code_mode.ungated_selector).
    code_mode: bool = False
```

- [ ] **Step 4: Append the capability conditionally**

In `build_collaborators` (same file), the `Agent(...)` construction at ~line 238 passes `capabilities=[...]` inline. Hoist it to a local list just above the `agent = Agent(` line and append conditionally. Replace:

```python
        capabilities=[
            ProcessHistory(_drop_nameless_tool_calls),
            ProcessHistory(suggest_unknown_tool_retry),
            DiscoveredInstructionsCapability(mcp),
        ],
```

with `capabilities=capabilities,` and insert before `agent = Agent(` (keeping the existing explanatory comment block above the Agent call untouched):

```python
    capabilities: list = [
        ProcessHistory(_drop_nameless_tool_calls),
        ProcessHistory(suggest_unknown_tool_retry),
        DiscoveredInstructionsCapability(mcp),
    ]
    if cfg.code_mode:
        # CodeMode experiment (deliberately last: it wraps the assembled
        # toolset). Import deferred so default installs never touch the
        # optional package; build_code_mode fail-fasts when the extra is
        # missing. Sub-agents are untouched — SubagentRunner composes its
        # own Agents and never sees this list.
        from .code_mode import build_code_mode

        capabilities.append(build_code_mode())
```

- [ ] **Step 5: Add the builder knob**

In `src/marim_harness/runtime/builder.py`, after `with_lsp` (~line 114), add:

```python
    def with_code_mode(self, *, enabled: bool = True) -> HarnessBuilder:
        """CodeMode experiment: sandbox every ungated tool behind one
        ``run_code`` tool (gated tools stay native). Requires the ``codemode``
        extra — build() fails fast with an install hint when it's missing."""
        self._config_overrides["code_mode"] = enabled
        return self
```

- [ ] **Step 6: Bootstrap passthrough**

In `src/marim_harness/runtime/bootstrap.py`, inside the `.with_config_overrides(` call (~line 129), directly after `forge_enabled=cfg.forge_enabled,`:

```python
            code_mode=cfg.code_mode,
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_codemode.py -v`
Expected: all PASS.

- [ ] **Step 8: Regression check on neighboring suites**

Run: `uv run pytest --no-cov tests/test_builder.py tests/test_builder_turns.py tests/test_bootstrap.py tests/test_agent.py -q`
Expected: all PASS (the capabilities hoist is behavior-neutral with the flag off).

- [ ] **Step 9: Commit**

```bash
git add src/marim_harness/runtime/harness.py src/marim_harness/runtime/builder.py src/marim_harness/runtime/bootstrap.py tests/test_codemode.py
git commit -m "feat(codemode): wire CodeMode capability behind HarnessConfig.code_mode (experiment)"
```

---

### Task 4: `CodeModeObserver` + `on_codemode_call` UI callback + upstream tripwire test

**Files:**
- Modify: `src/marim_harness/runtime/code_mode.py` (add observer)
- Modify: `src/marim_harness/runtime/deps.py` (`UIHooks` field, ~line 185)
- Modify: `src/marim_harness/runtime/harness.py` (`bind_ui` param + observer append in the Task-3 branch)
- Test: `tests/test_codemode.py`

**Interfaces:**
- Consumes: `NestedToolCall`, the Task-3 `if cfg.code_mode:` branch.
- Produces: `CodeModeObserver` (an `AbstractCapability`); `Deps.ui.on_codemode_call: Callable[[NestedToolCall], None] | None`; `Harness.bind_ui(on_codemode_call=...)`. Task 5's `StreamRenderer.on_codemode_call(call: NestedToolCall)` is bound to it.

- [ ] **Step 1: Write the failing observer tests**

Append to `tests/test_codemode.py`:

```python
# --- Task 4: observer + nested-id tripwire ------------------------------------

from types import SimpleNamespace  # noqa: E402


def _ctx(recorder):
    return SimpleNamespace(deps=SimpleNamespace(ui=SimpleNamespace(on_codemode_call=recorder)))


def _call_part(tool_name: str, call_id: str, args: dict | None = None):
    from pydantic_ai.messages import ToolCallPart

    return ToolCallPart(tool_name=tool_name, args=args or {}, tool_call_id=call_id)


async def test_observer_emits_only_nested_calls():
    from marim_harness.runtime.code_mode import CodeModeObserver

    events = []
    ctx = _ctx(events.append)
    obs = CodeModeObserver()
    run_code_def = _tool_def("run_code", metadata={"code_arg_name": "code"})
    plain_def = _tool_def("read_file")

    # run_code itself starts: tracked, not emitted (its card streams normally).
    await obs.before_tool_execute(
        ctx, call=_call_part("run_code", "abc"), tool_def=run_code_def, args={"code": "x"})
    assert events == []

    # A nested call under it: pending then done.
    nested = _call_part("read_file", "abc__1", {"path": "x.py"})
    args = await obs.before_tool_execute(ctx, call=nested, tool_def=plain_def,
                                         args={"path": "x.py"})
    assert args == {"path": "x.py"}  # observe-only: args pass through
    result = await obs.after_tool_execute(ctx, call=nested, tool_def=plain_def,
                                          args={"path": "x.py"}, result="body")
    assert result == "body"  # observe-only: result passes through
    assert [(e.call_id, e.status) for e in events] == [("abc__1", "pending"), ("abc__1", "done")]
    assert events[1].result_preview == "body"
    assert events[0].parent_call_id == "abc"

    # run_code settles: id no longer active.
    await obs.after_tool_execute(
        ctx, call=_call_part("run_code", "abc"), tool_def=run_code_def,
        args={"code": "x"}, result="out")
    # A later top-level call whose id merely contains "__" is NOT nested.
    await obs.before_tool_execute(
        ctx, call=_call_part("grep", "abc__2"), tool_def=plain_def, args={})
    assert len(events) == 2


async def test_observer_failed_nested_call_reraises():
    from marim_harness.runtime.code_mode import CodeModeObserver

    events = []
    ctx = _ctx(events.append)
    obs = CodeModeObserver()
    run_code_def = _tool_def("run_code", metadata={"code_arg_name": "code"})
    await obs.before_tool_execute(
        ctx, call=_call_part("run_code", "p"), tool_def=run_code_def, args={"code": "x"})
    boom = RuntimeError("boom")
    # The error hook MUST re-raise: returning would suppress the error and
    # smuggle the exception in as the tool result (pydantic-ai contract).
    with pytest.raises(RuntimeError, match="boom"):
        await obs.on_tool_execute_error(
            ctx, call=_call_part("glob", "p__1"), tool_def=_tool_def("glob"),
            args={}, error=boom)
    assert [(e.call_id, e.status) for e in events] == [("p__1", "failed")]


async def test_observer_silent_headless():
    from marim_harness.runtime.code_mode import CodeModeObserver

    obs = CodeModeObserver()
    ctx = SimpleNamespace(deps=SimpleNamespace(ui=SimpleNamespace(on_codemode_call=None)))
    td = _tool_def("read_file")
    # No callback bound: hooks are no-ops, never raise.
    await obs.before_tool_execute(ctx, call=_call_part("x", "a__1"), tool_def=td, args={})
    await obs.after_tool_execute(ctx, call=_call_part("x", "a__1"), tool_def=td,
                                 args={}, result=1)


async def test_upstream_nested_id_scheme_and_metadata():
    """Version-pin tripwire against the REAL upstream toolset: nested ids are
    f'{parent}__{n}' and the run_code return carries the code_mode metadata.
    If a pin bump changes either, this fails loudly (the observer's nested
    detection and nested_from_metadata both key on these shapes)."""
    pytest.importorskip("pydantic_monty")
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness import CodeMode

    def call(messages, info) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="run_code", args={"code": "await greet()"})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(call), capabilities=[CodeMode()])

    @agent.tool_plain
    def greet() -> str:
        """Say hi."""
        return "hi"

    result = await agent.run("go")
    returns = [p for m in result.all_messages() for p in m.parts
               if isinstance(p, ToolReturnPart) and p.tool_name == "run_code"]
    assert returns, "no run_code return recorded"
    part = returns[0]
    meta = part.metadata
    assert isinstance(meta, dict) and meta.get("code_mode") is True
    assert set(meta["tool_calls"]) == {f"{part.tool_call_id}__1"}
    # And marim's parser reads the live-object form of that metadata.
    parsed = nested_from_metadata(meta)
    assert [(c.tool_name, c.status) for c in parsed] == [("greet", "done")]
    assert parsed[0].result_preview == "hi"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_codemode.py -v -k observer`
Expected: FAIL — `ImportError: cannot import name 'CodeModeObserver'`.

- [ ] **Step 3: Implement the observer**

Append to `src/marim_harness/runtime/code_mode.py` (and extend the module imports: `from dataclasses import dataclass, field`; add a runtime import `from pydantic_ai.capabilities import AbstractCapability` — this is core pydantic-ai, always installed, so it does NOT go behind the lazy-import guard):

```python
def _is_code_tool(tool_def: "ToolDefinition") -> bool:
    """A tool whose argument is model-written code — upstream flags these via
    ToolDefinition.metadata['code_arg_name'] (CodeMode's run_code today)."""
    metadata = getattr(tool_def, "metadata", None)
    return bool(metadata and "code_arg_name" in metadata)


@dataclass
class CodeModeObserver(AbstractCapability[Any]):
    """Streams nested sandbox tool calls to the UI as they execute.

    Inner sandbox calls dispatch through a real ToolManager that inherits the
    agent's root capability, so these hooks fire once per nested call. Pure
    display sugar: headless (no callback bound) it is a no-op, and it never
    alters args, results, or error propagation. Nested detection keys on the
    upstream id scheme f'{parent_id}__{n}' — pinned-version behavior, guarded
    by test_upstream_nested_id_scheme_and_metadata.
    """

    # tool_call_ids of run_code calls currently executing. The Harness runs
    # one turn at a time and sub-agents build their own Agents (no observer),
    # so a plain set is safe here.
    _active: set = field(default_factory=set)

    async def before_tool_execute(self, ctx, *, call, tool_def, args):
        if _is_code_tool(tool_def):
            self._active.add(call.tool_call_id)
        else:
            self._emit(ctx, call, args, status="pending")
        return args

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        if _is_code_tool(tool_def):
            self._active.discard(call.tool_call_id)
        else:
            self._emit(ctx, call, args, status="done", result=result)
        return result

    async def on_tool_execute_error(self, ctx, *, call, tool_def, args, error):
        if _is_code_tool(tool_def):
            self._active.discard(call.tool_call_id)
        else:
            self._emit(ctx, call, args, status="failed", result=error)
        # Contract: raising propagates the error; RETURNING would suppress it
        # and use the exception object as the tool result. Observe-only, so
        # always re-raise.
        raise error

    def _emit(self, ctx, call, args: dict, *, status: str, result: object = None) -> None:
        """Push one nested-call event to the UI callback, if this call is
        nested under an active run_code and a UI is bound. Top-level calls
        are skipped — their cards already render from the event stream."""
        ui = getattr(ctx.deps, "ui", None)
        callback = getattr(ui, "on_codemode_call", None)
        if callback is None:
            return
        parent, sep, tail = call.tool_call_id.rpartition("__")
        if not sep or parent not in self._active or not tail.isdigit():
            return
        callback(NestedToolCall(
            parent_call_id=parent, call_id=call.tool_call_id,
            tool_name=call.tool_name, args=dict(args), status=status,
            result_preview="" if result is None else _preview(result),
        ))
```

- [ ] **Step 4: Add the UIHooks field and bind_ui param**

In `src/marim_harness/runtime/deps.py`, `UIHooks` (~line 185), after `on_present_plan`:

```python
    # CodeMode experiment: one nested sandbox tool call (pending → settled),
    # rendered as a line inside the parent run_code card. None headless.
    on_codemode_call: "Callable[[NestedToolCall], None] | None" = None
```

Add to the module's `TYPE_CHECKING` block (create the import line next to the existing ones):

```python
    from .code_mode import NestedToolCall
```

In `src/marim_harness/runtime/harness.py` `bind_ui` (~line 413): add the parameter after `on_ttft`:

```python
        on_codemode_call: "Callable[[NestedToolCall], None] | None" = None,
```

and in the body, after `self.deps.ui.on_ttft = on_ttft`:

```python
        self.deps.ui.on_codemode_call = on_codemode_call
```

with `NestedToolCall` added to harness.py's `TYPE_CHECKING` imports:

```python
    from .code_mode import NestedToolCall
```

- [ ] **Step 5: Register the observer alongside the capability**

In the Task-3 branch in `build_collaborators`, change:

```python
        from .code_mode import build_code_mode

        capabilities.append(build_code_mode())
```

to:

```python
        from .code_mode import CodeModeObserver, build_code_mode

        capabilities.extend([build_code_mode(), CodeModeObserver()])
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_codemode.py -v`
Expected: all PASS (including the tripwire test actually executing Monty).

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/runtime/code_mode.py src/marim_harness/runtime/deps.py src/marim_harness/runtime/harness.py tests/test_codemode.py
git commit -m "feat(codemode): nested-call observer + on_codemode_call UI callback (experiment)"
```

---

### Task 5: TUI rendering — code card, nested mini-transcript, live + restore paths

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/tool_summary.py` (`_TOOL_LABELS` ~line 18, `_TARGET_ARG` ~line 30)
- Modify: `src/marim_harness/interfaces/tui/widgets/tools.py` (`ToolCallWidget.__init__` ~line 38, `_primary_renderable` ~line 230, `_render_body` ~line 373; new methods)
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (new `on_codemode_call` near `on_ttft` ~line 452; `_on_tool_result` ~line 907)
- Modify: `src/marim_harness/interfaces/tui/session_view.py` (`ToolReturnPart` branch ~line 118)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`bind_ui` call ~line 116)
- Test: `tests/test_codemode.py`

**Interfaces:**
- Consumes: `NestedToolCall`, `nested_from_metadata` (Tasks 2/4); `Harness.bind_ui(on_codemode_call=...)`.
- Produces: `ToolCallWidget.note_nested(call: NestedToolCall) -> None`, `ToolCallWidget.set_nested_from_metadata(metadata: object) -> None`, `StreamRenderer.on_codemode_call(call: NestedToolCall) -> None`.

Design note (small, deliberate deviation from the spec's "check `code_arg_name` tool metadata" wording): the stream events carry only `ToolCallPart` (name + args), never a `ToolDefinition`, so the *widget* keys on `tool_name == "run_code"`. The metadata-generic detection is used where a `ToolDefinition` actually exists (the observer, Task 4). If a second code-carrying tool ever appears, generalize then.

- [ ] **Step 1: Write the failing widget tests**

Append to `tests/test_codemode.py`:

```python
# --- Task 5: TUI rendering ------------------------------------------------------


def _render_to_text(renderable) -> str:
    from rich.console import Console

    console = Console(width=200, force_terminal=False, no_color=True)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def test_run_code_card_highlights_code_and_labels():
    from marim_harness.interfaces.tui.widgets.tool_summary import humanize_tool
    from marim_harness.interfaces.tui.widgets.tools import ToolCallWidget

    assert humanize_tool("run_code") == "Code"
    w = ToolCallWidget("run_code", {"code": "x = await read_file(path='a.py')\nx"})
    body = _render_to_text(w._render_body())
    # The code arg renders as the body (not a raw `code: '…'` repr line).
    assert "await read_file" in body
    assert "code:" not in body


def test_run_code_card_renders_nested_lines():
    from marim_harness.interfaces.tui.widgets.tools import ToolCallWidget
    from marim_harness.runtime.code_mode import NestedToolCall

    w = ToolCallWidget("run_code", {"code": "await grep(pattern='foo')"})
    w.note_nested(NestedToolCall("abc", "abc__1", "grep", {"pattern": "foo"}, "pending"))
    body = _render_to_text(w._render_body())
    assert "Grep" in body and "foo" in body
    # Settling the same call upserts (no duplicate line) and flips the glyph.
    w.note_nested(NestedToolCall("abc", "abc__1", "grep", {"pattern": "foo"}, "done", "3 hits"))
    body = _render_to_text(w._render_body())
    assert body.count("Grep") == 1
    assert "✓" in body


def test_run_code_card_restores_nested_from_metadata():
    from marim_harness.interfaces.tui.widgets.tools import ToolCallWidget

    w = ToolCallWidget("run_code", {"code": "await read_file(path='x.py')"})
    w.set_nested_from_metadata({
        "code_mode": True,
        "tool_calls": {"abc__1": {"tool_name": "read_file",
                                  "args": {"path": "x.py"}, "tool_call_id": "abc__1"}},
        "tool_returns": {"abc__1": {"tool_name": "read_file", "content": "text",
                                    "tool_call_id": "abc__1"}},
    })
    w.finish("42")
    body = _render_to_text(w._render_body())
    assert "Read" in body and "x.py" in body and "✓" in body and "42" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_codemode.py -v -k run_code_card`
Expected: FAIL — `humanize_tool("run_code") == "Run Code"`, then `AttributeError: note_nested`.

- [ ] **Step 3: Labels**

In `src/marim_harness/interfaces/tui/widgets/tool_summary.py`:

- `_TOOL_LABELS`: add `"run_code": "Code",` (alongside the other entries).
- `_TARGET_ARG`: add `"run_code": "code",` with the neighboring comment style:

```python
    # run_code's target is the code itself (whitespace-flattened and clipped by
    # summarize); pinned so the arg-order fallback can't surface something else.
    "run_code": "code",
```

- [ ] **Step 4: Widget — nested state, code body, mini-transcript**

In `src/marim_harness/interfaces/tui/widgets/tools.py`:

**(a)** In `ToolCallWidget.__init__`, after `self.reveal = False` (~line 59):

```python
        # CodeMode experiment: nested sandbox calls shown as a mini-transcript
        # inside this card (run_code only). Keyed by nested call id so a
        # pending line upserts in place when the call settles. Fed live by
        # StreamRenderer.on_codemode_call and on restore/finish from the
        # persisted run_code return metadata (set_nested_from_metadata).
        self._nested: dict[str, object] = {}
```

**(b)** In `_primary_renderable` (~line 230), before the final `return None`:

```python
        if self.tool_name == "run_code":
            # The arg is model-written Python (upstream flags it via the tool
            # definition's code_arg metadata; the stream only carries the
            # name, so the widget keys on it). Highlight it like file source.
            code = str(self.args.get("code", ""))
            if not highlight:
                return code
            return self._highlight(code, "sandbox.py")
```

**(c)** Extend `_highlights_a_body` (~line 325) so the off-thread highlight pass covers the code body (mirror the existing branches):

```python
        if self.tool_name == "run_code":
            return bool(self.args.get("code"))
```

**(d)** New methods, placed after `set_reveal` (~line 407):

```python
    def note_nested(self, call) -> None:
        """Upsert one nested sandbox call (a runtime.code_mode.NestedToolCall)
        and re-render. Live-path sugar: the persisted metadata re-renders the
        same lines on restore, so a dropped event is cosmetic, not lossy."""
        self._nested[call.call_id] = call
        self._refresh_body()

    def set_nested_from_metadata(self, metadata: object) -> None:
        """Replace the nested lines from a run_code return's persisted
        metadata — the restore path, and the live path's final authoritative
        state at finish()."""
        from ....runtime.code_mode import nested_from_metadata

        calls = nested_from_metadata(metadata)
        if calls:
            self._nested = {c.call_id: c for c in calls}
            self._refresh_body()

    def _nested_renderable(self) -> Text:
        """The mini-transcript: one compact `↳ {glyph} {Label} · {target}` line
        per nested call, result preview trailing dim."""
        out = Text()
        for i, call in enumerate(self._nested.values()):
            if i:
                out.append("\n")
            if call.status == "failed":
                glyph, style = "✗", _FAIL_FG
            elif call.status == "denied":
                glyph, style = "✕", ""
            elif call.status == "done":
                glyph, style = "✓", ""
            else:
                glyph, style = "…", "dim"
            s = summarize(call.tool_name, call.args)
            out.append("  ↳ ")
            out.append(f"{glyph} ", style=style)
            out.append(f"{s.label} · {s.target}" if s.target else s.label)
            if call.result_preview:
                out.append(f"  {call.result_preview}", style="dim")
        return out
```

**(e)** In `_render_body` (~line 373), thread the nested lines in. After `primary = self._primary_renderable(highlight=highlight)` the existing code is:

```python
        if primary is not None:
            if not self.result_text:
                return primary
            # Text() keeps the result literal inside the Group (markup=False).
            return Group(primary, "", Text(self.result_text))
```

Replace with:

```python
        if primary is not None:
            # run_code interleaves its nested-call mini-transcript between the
            # code and the result; empty for every other primary-bodied tool.
            sections: list = [primary]
            if self._nested:
                sections += ["", self._nested_renderable()]
            if self.result_text:
                # Text() keeps the result literal inside the Group (markup=False).
                sections += ["", Text(self.result_text)]
            if len(sections) == 1:
                return primary
            return Group(*sections)
```

- [ ] **Step 5: Run widget tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_codemode.py -v -k run_code_card`
Expected: 3 PASS.

- [ ] **Step 6: Stream renderer — live callback + finish/restore metadata**

In `src/marim_harness/interfaces/tui/stream_render.py`:

**(a)** After `def on_ttft(...)` (~line 452), add:

```python
    def on_codemode_call(self, call) -> None:
        """One nested sandbox call from CodeModeObserver: append/refresh its
        line inside the parent run_code card. Display sugar — if the parent
        card isn't mounted yet (event raced the mount) the event is dropped;
        the persisted metadata re-renders the full list at finish anyway."""
        widget = self.tool_widgets.get(call.parent_call_id)
        if isinstance(widget, ToolCallWidget):
            widget.note_nested(call)
```

**(b)** In `_on_tool_result` (~line 907), right before `widget.finish(content, status=status)` executes for the plain-widget path, add the metadata hand-off. The existing block:

```python
            else:
                status = status_from_part(event.part)
```

gains, immediately after the `status = status_from_part(event.part)` line (and before the SubAgent failure check):

```python
                # A run_code return carries the authoritative nested-call list
                # in its metadata; swap it in before finish() renders the body.
                meta = getattr(event.part, "metadata", None)
                if isinstance(widget, ToolCallWidget) and isinstance(meta, dict) \
                        and meta.get("code_mode"):
                    widget.set_nested_from_metadata(meta)
```

**(c)** In `src/marim_harness/interfaces/tui/session_view.py`, in the `ToolReturnPart` branch (~line 118), immediately before `widget.finish(content, status=status)` (~line 143):

```python
                # Restore path for run_code: rebuild the nested mini-transcript
                # from the persisted return metadata (mirrors the live path).
                meta = getattr(part, "metadata", None)
                if isinstance(widget, ToolCallWidget) and isinstance(meta, dict) \
                        and meta.get("code_mode"):
                    widget.set_nested_from_metadata(meta)
```

(`ToolCallWidget` is already imported in session_view.py.)

**(d)** In `src/marim_harness/interfaces/tui/app.py`, in the `self.harness.bind_ui(` call (~line 116), after `on_ttft=self.stream.on_ttft,`:

```python
            on_codemode_call=self.stream.on_codemode_call,
```

- [ ] **Step 7: Run the TUI suites + full experiment file**

Run: `uv run pytest --no-cov tests/test_codemode.py tests/test_app.py tests/test_stream_render.py tests/test_session_view.py -q`
(If `tests/test_stream_render.py`/`tests/test_session_view.py` don't exist under those names, run `uv run pytest --no-cov tests/ -q -k "app or render or session_view"` instead.)
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/tool_summary.py src/marim_harness/interfaces/tui/widgets/tools.py src/marim_harness/interfaces/tui/stream_render.py src/marim_harness/interfaces/tui/session_view.py src/marim_harness/interfaces/tui/app.py tests/test_codemode.py
git commit -m "feat(codemode): run_code code card + nested mini-transcript in TUI (experiment)"
```

---

### Task 6: Benchmark script, `usage.requests`, docs

**Files:**
- Modify: `src/marim_harness/usage.py` (`usage_summary` ~line 84)
- Create: `scripts/bench_codemode.py`
- Modify: `.env.example` (Misc section at the tail)
- Modify: `CLAUDE.md` (env-knobs paragraph in Commands)
- Test: `tests/test_codemode.py`

**Interfaces:**
- Consumes: headless `marim -p … --output-format json` whose `usage` object comes from `usage_summary`.
- Produces: `usage_summary(...)["requests"]: int`; `scripts/bench_codemode.py` (run manually, not in CI).

- [ ] **Step 1: Write the failing usage test**

Append to `tests/test_codemode.py`:

```python
# --- Task 6: benchmark plumbing ----------------------------------------------


def test_usage_summary_carries_request_count():
    from pydantic_ai.usage import RunUsage

    from marim_harness.usage import usage_summary

    d = usage_summary(RunUsage(requests=7, input_tokens=10, output_tokens=2), None)
    assert d["requests"] == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_codemode.py::test_usage_summary_carries_request_count -v`
Expected: FAIL — `KeyError: 'requests'`.

- [ ] **Step 3: Add the field**

In `src/marim_harness/usage.py` `usage_summary`, first line of the returned dict:

```python
    return {
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        ...
```

Also update the docstring's first line: `"""A JSON-friendly usage breakdown: the model-request count, the raw input/output/total counts, …"""`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest --no-cov tests/test_codemode.py::test_usage_summary_carries_request_count tests/test_usage.py -v`
Expected: all PASS (existing usage tests assert individual keys, not full-dict equality; if one does compare the whole dict, add `"requests": 0` to its expectation).

- [ ] **Step 5: Write the benchmark script**

Create `scripts/bench_codemode.py`:

```python
#!/usr/bin/env python3
"""A/B benchmark for the CodeMode experiment — deleted with the experiment.

Runs 4 fixed read-heavy prompts against this repo, headless, flag off vs on,
N repetitions each, on the free local model (MARIM_PROVIDER=local — never a
paid model). Prints a markdown table of model requests / tokens / wall-clock.

Keep rule (spec): >=2x fewer model requests on average with no judged quality
loss; otherwise delete the experiment.

Usage:
    uv run python scripts/bench_codemode.py [--reps 3] [--model MODEL_ID]

Requires: `uv sync --extra codemode`, LM Studio serving on the local
provider's base URL (see .env / MARIM_BASE_URL).
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PROMPTS = [
    "Summarize what each module in src/marim_harness/runtime/ is responsible for.",
    "Find every caller of resolve_approvals and describe how each uses it.",
    "List every tool registered on the main agent and the file where each is registered.",
    "Which test files cover session resumability, and what does each one check?",
]


def _count_run_code_retries(data_home: Path) -> int:
    """Count retry-prompt parts against run_code in the run's persisted session
    — Monty syntax/type errors surface as ModelRetry, so this is the spec's
    'small-local-model code quality' failure metric. Best-effort: an unreadable
    session yields 0."""
    total = 0
    for session_file in (data_home / "marim-harness" / "sessions").glob("*.json"):
        try:
            text = session_file.read_text(encoding="utf-8")
        except OSError:
            continue
        # Lenient scan over the serialized message parts, whatever the outer shape.
        for chunk in text.split('"part_kind"'):
            if '"retry-prompt"' in chunk[:40] and '"run_code"' in chunk:
                total += 1
    return total


def run_once(prompt: str, code_mode: bool, model: str | None) -> dict:
    """One headless run; returns {'requests', 'input_tokens', 'output_tokens',
    'run_code_retries', 'wall_s', 'ok'}. A failed run (nonzero exit /
    unparseable JSON) reports ok=False and is excluded from averages but
    counted in the table. Each run gets its own XDG_DATA_HOME so the session
    it persists can be scanned for run_code retries without touching the
    developer's real session store."""
    import os

    with tempfile.TemporaryDirectory(prefix="bench-codemode-") as tmp:
        env = os.environ.copy()
        env["MARIM_CODEMODE"] = "1" if code_mode else "0"
        env["MARIM_PROVIDER"] = "local"
        env["XDG_DATA_HOME"] = tmp
        if model:
            env["MARIM_MODEL"] = model
        t0 = time.monotonic()
        proc = subprocess.run(
            ["uv", "run", "marim", "-p", prompt, "--output-format", "json", "--mode", "auto"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=1200,
        )
        wall = time.monotonic() - t0
        retries = _count_run_code_retries(Path(tmp))
    try:
        payload = json.loads(proc.stdout)
        usage = payload.get("usage") or {}
        return {
            "requests": usage.get("requests"), "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"), "run_code_retries": retries,
            "wall_s": wall,
            "ok": proc.returncode == 0 and usage.get("requests") is not None,
        }
    except (json.JSONDecodeError, ValueError):
        print(f"  !! unparseable output (rc={proc.returncode}): "
              f"{proc.stdout[:200]!r} / {proc.stderr[-200:]!r}", file=sys.stderr)
        return {"requests": None, "input_tokens": None, "output_tokens": None,
                "run_code_retries": retries, "wall_s": wall, "ok": False}


def mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 1) if vals else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--model", default=None, help="MARIM_MODEL override for the local provider")
    args = ap.parse_args()

    rows = []
    for i, prompt in enumerate(PROMPTS, 1):
        for arm, flag in (("off", False), ("on", True)):
            runs = []
            for rep in range(args.reps):
                print(f"prompt {i} · codemode {arm} · rep {rep + 1}/{args.reps} …",
                      file=sys.stderr)
                runs.append(run_once(prompt, flag, args.model))
            ok = [r for r in runs if r["ok"]]
            rows.append({
                "prompt": f"P{i}", "arm": arm, "ok": f"{len(ok)}/{len(runs)}",
                "requests": mean([r["requests"] for r in ok]),
                "in_tok": mean([r["input_tokens"] for r in ok]),
                "out_tok": mean([r["output_tokens"] for r in ok]),
                "retries": mean([r["run_code_retries"] for r in runs]),
                "wall_s": mean([r["wall_s"] for r in ok]),
            })

    print("\n| prompt | codemode | ok | avg requests | avg in tok | avg out tok "
          "| avg run_code retries | avg wall s |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['prompt']} | {r['arm']} | {r['ok']} | {r['requests']} "
              f"| {r['in_tok']} | {r['out_tok']} | {r['retries']} | {r['wall_s']} |")

    # The keep-rule request ratio, per prompt and overall.
    print("\n| prompt | requests off | requests on | ratio (off/on) |")
    print("|---|---|---|---|")
    ratios = []
    for i in range(0, len(rows), 2):
        off, on = rows[i], rows[i + 1]
        if off["requests"] and on["requests"]:
            ratio = round(off["requests"] / on["requests"], 2)
            ratios.append(ratio)
        else:
            ratio = "n/a"
        print(f"| {off['prompt']} | {off['requests']} | {on['requests']} | {ratio} |")
    if ratios:
        print(f"\nOverall mean ratio: {round(statistics.mean(ratios), 2)} "
              f"(keep rule: >= 2.0 with no judged quality loss)")


if __name__ == "__main__":
    main()
```

No unit test for the script (it is a manual measurement harness, excluded from coverage by living in `scripts/`); its JSON contract is covered by the `usage_summary` test above.

- [ ] **Step 6: Docs**

`.env.example` — in the `# --- Misc ---` section, after the `MARIM_JOB_TOOL_COMBINED` lines:

```bash
# CodeMode experiment: sandbox ungated tools behind one `run_code` tool the
# model drives with Python (needs `uv sync --extra codemode`; gated tools
# stay native so approvals are unchanged).
# MARIM_CODEMODE=1
```

`CLAUDE.md` — in the Commands section's env paragraph (the one listing `MARIM_PROVIDER`/`MARIM_MODEL`), append one sentence:

```
`MARIM_CODEMODE=1` (experiment; needs `uv sync --extra codemode`) sandboxes the
ungated tools behind a single `run_code` tool — gated tools and approvals are
unchanged, and it does not apply to the `claude-cli` provider (marim's tools
don't run there).
```

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/usage.py scripts/bench_codemode.py .env.example CLAUDE.md tests/test_codemode.py
git commit -m "feat(codemode): A/B benchmark script + usage request count + docs (experiment)"
```

---

### Task 7: Full verification in CI order

**Files:** none new — fixes only, if anything fails.

- [ ] **Step 1: Lint**

Run: `uv run ruff check src tests scripts`
Expected: clean. Fix with `uv run ruff check --fix src tests scripts` + manual edits if needed.

- [ ] **Step 2: Type-check**

Run: `uv run pyright`
Expected: clean. Likely candidates if not: the `Callable` import in deps.py/harness.py TYPE_CHECKING blocks, the `_nested: dict[str, object]` widget field (annotate method params as the actual `NestedToolCall` behind TYPE_CHECKING if pyright complains about attribute access — `from ....runtime.code_mode import NestedToolCall`).

- [ ] **Step 3: Full test suite**

Run: `uv run pytest`
Expected: all PASS with coverage on. If the new dev-group dep breaks collection on this machine, report — per the Global Constraint the fallback is dropping the dev-group line (tests skip via importorskip), but that decision goes back to the user first.

- [ ] **Step 4: Commit any fixes**

```bash
git add -A
git commit -m "chore(codemode): lint/type fixes from full verification"
```

(Skip the commit if Steps 1–3 were already clean.)

- [ ] **Step 5: Report benchmark readiness**

Do NOT run the benchmark automatically — it needs LM Studio serving locally. Report to the user: implementation complete, benchmark ready via `uv run python scripts/bench_codemode.py` with LM Studio up, keep rule ≥2× fewer requests with no quality loss.
