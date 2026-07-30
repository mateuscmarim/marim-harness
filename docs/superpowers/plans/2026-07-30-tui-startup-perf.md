# TUI Startup Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paint the interactive TUI in ≤ ~0.5s and reach an interactive prompt in ≤ ~2.5s by cleaning the `HarnessApp` import graph, lazy-loading tool modules, and building the harness on a worker after first paint.

**Architecture:** (1) Make `from marim_harness.interfaces.tui.app import HarnessApp` load without `pydantic_ai` / `markdownify` / `bs4` so `default_cmd` can construct the App before agent work. (2) Lazy-resolve tool callables in `tools/provider.py`. (3) Defer heavy `bootstrap` imports into the functions that use them. (4) Introduce `TuiLaunch`; `HarnessApp` paints immediately, runs `build_harness` in a Textual worker, then binds UI and runs today's `on_mount` tail. Construction failure exits non-zero (no retry in v1).

**Tech Stack:** Python 3.10+, Textual workers, pytest + subprocess import invariants, ruff (line-length 100), pyright.

## Global Constraints

- **No behavior change once ready** — tools, resume, MCP, trust, approvals, slash commands identical after the harness is bound.
- **Headless UX unchanged** — still sync `build_harness` then run; may benefit from import cuts automatically.
- **Failure UX (v1):** construction error after paint → clear error + exit non-zero. No in-TUI retry.
- **`Mode` stays import-safe** (no module-level `pydantic_ai` in `permissions.py`).
- Reuse subprocess import-invariant style from `tests/test_cli_startup.py`.
- ruff → pyright → pytest must stay green; complexity cap C901=10; `requires-python >=3.10`.
- Spec: `docs/superpowers/specs/2026-07-30-tui-startup-perf-design.md`.

## File map

| File | Responsibility |
|------|----------------|
| `src/marim_harness/tools/provider.py` | Lazy tool callable resolution; no eager concern-module imports; no runtime `deps` import at module top. |
| `src/marim_harness/usage.py` | Keep `RunUsage` off the module-import critical path where practical (TYPE_CHECKING + runtime import in functions that need the type/value). |
| `src/marim_harness/interfaces/tui/widgets/status_bar.py` | Lazy-import `estimate_tokens` / `resolve_cost` inside methods so importing StatusBar does not load compaction/pydantic_ai. |
| `src/marim_harness/interfaces/tui/stream_render.py` | Lazy-import `pydantic_ai.messages` symbols inside the functions/methods that use them (or a single `_messages()` helper). |
| `src/marim_harness/interfaces/tui/session_view.py` | Import `strip_turn_context` from `runtime.context` (not `harness`); lazy `summary_text`. |
| `src/marim_harness/interfaces/tui/commands.py` | Lazy-import `McpStatus` only in handlers that need it. |
| `src/marim_harness/interfaces/tui/app.py` | `TYPE_CHECKING` for `Harness`; lazy `ToolDenied`; accept `TuiLaunch \| Harness`; early-paint worker; readiness gate. |
| `src/marim_harness/interfaces/tui/launch.py` | Frozen `TuiLaunch` dataclass + env `model_label` helper (keeps app/default_cmd thin). |
| `src/marim_harness/interfaces/cli/default_cmd.py` | TUI path: build `TuiLaunch`, do not call `build_harness` before `App.run()`. |
| `src/marim_harness/runtime/bootstrap.py` | Move heavy top-level imports into `build_harness` / `build_lsp_registry`. |
| `tests/test_cli_startup.py` | Extend with paint-path / provider / bootstrap invariants. |
| `tests/test_tui_startup.py` | Readiness gate + construction-failure exit (new). |
| `tests/test_provider.py` | Stay green; add lazy-resolve coverage if not already implied. |

```
default_cmd ──► TuiLaunch + HarnessApp.run()
                     │
                     ├─ first paint (no pydantic_ai)
                     └─ worker: build_harness → bind_ui → on_mount tail
```

---

### Task 1: Lazy tool modules in `tools/provider.py`

Stop importing all concern modules and `runtime.deps` at module load so `import tools.provider` does not load `markdownify`/`bs4`/`pydantic_ai`.

**Files:**
- Modify: `src/marim_harness/tools/provider.py`
- Test: `tests/test_cli_startup.py` (extend), existing `tests/test_provider.py` must stay green

**Interfaces:**
- Consumes: tool callables still live in `fs_tools`, `edit_tools`, … (unchanged signatures)
- Produces: `_resolve_tool(name: str) -> Callable | None`; `BuiltinToolProvider.register` / `register_subagent` / `lsp_toolset` resolve callables on use; module import does not load `markdownify` or `bs4`

- [ ] **Step 1: Write the failing import-invariant tests**

Append to `tests/test_cli_startup.py`:

```python
def _module_loads(module: str, banned: str) -> bool:
    """True if importing `module` in a fresh interpreter leaves `banned` in sys.modules."""
    code = (
        f"import {module}\n"
        "import sys\n"
        f"raise SystemExit(1 if {banned!r} in sys.modules else 0)"
    )
    return subprocess.run([sys.executable, "-c", code]).returncode == 1


def test_tools_provider_import_does_not_load_markdownify():
    # fetch_url's HTML stack must not load until a net tool is actually resolved.
    assert not _module_loads("marim_harness.tools.provider", "markdownify")


def test_tools_provider_import_does_not_load_bs4():
    assert not _module_loads("marim_harness.tools.provider", "bs4")


def test_tools_provider_import_does_not_load_pydantic_ai():
    # provider only needs deps types for annotations; runtime deps import pulls Agent.
    assert not _module_loads("marim_harness.tools.provider", "pydantic_ai")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_cli_startup.py::test_tools_provider_import_does_not_load_markdownify tests/test_cli_startup.py::test_tools_provider_import_does_not_load_bs4 tests/test_cli_startup.py::test_tools_provider_import_does_not_load_pydantic_ai -v`

Expected: FAIL (today provider imports concern modules + `deps` → all three load).

- [ ] **Step 3: Implement lazy resolution**

Replace the eager `from . import advisor_tools, …` block and the live `_SUBAGENT_FNS` callables with a name → `(module_name, attr)` table and a resolver. Keep `from ..runtime.deps import HarnessAgent, SubAgent` behind `TYPE_CHECKING` and use `from __future__ import annotations` so those names are strings at runtime.

Sketch (adapt to file style; keep C901 low by extracting helpers):

```python
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pydantic_ai.toolsets import FunctionToolset

    from ..runtime.deps import Deps, HarnessAgent, SubAgent

from .names import (  # noqa: F401 — re-exports unchanged
    GATED_TOOLS,
    LSP_TOOLS,
    NET_TOOLS,
    READ_TOOLS,
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_TOOLS,
    TOOL_GROUPS,
)

# module basenames under marim_harness.tools
_TOOL_IMPLS: dict[str, tuple[str, str]] = {
    "read_file": ("fs_tools", "read_file"),
    "glob": ("fs_tools", "glob"),
    "tree": ("fs_tools", "tree"),
    "grep": ("fs_tools", "grep"),
    "goto_definition": ("lsp_tools", "goto_definition"),
    "find_references": ("lsp_tools", "find_references"),
    "hover": ("lsp_tools", "hover"),
    "document_symbols": ("lsp_tools", "document_symbols"),
    "workspace_symbols": ("lsp_tools", "workspace_symbols"),
    "diagnostics": ("lsp_tools", "diagnostics"),
    "web_search": ("net_tools", "web_search"),
    "fetch_url": ("net_tools", "fetch_url"),
    "write_file": ("edit_tools", "write_file"),
    "edit_file": ("edit_tools", "edit_file"),
    "bash": ("edit_tools", "bash"),
    "remember": ("memory_tools", "remember"),
    "recall": ("memory_tools", "recall"),
    "forget": ("memory_tools", "forget"),
    "activate_skill": ("skill_tools", "activate_skill"),
    "read_skill_file": ("skill_tools", "read_skill_file"),
    "update_tasks": ("planning_tools", "update_tasks"),
    "ask_user": ("planning_tools", "ask_user"),
    "present_plan": ("planning_tools", "present_plan"),
    "spawn_agent": ("spawn_tools", "spawn_agent"),
    "run_workflow": ("workflow_tools", "run_workflow"),
    "jobs": ("job_tools", "jobs"),
    "job_output": ("job_tools", "job_output"),
    "wait_for_job": ("job_tools", "wait_for_job"),
    "cancel_job": ("job_tools", "cancel_job"),
    "job": ("job_tools", "job"),
    "advisor": ("advisor_tools", "advisor"),
    "prepare_advisor": ("advisor_tools", "prepare_advisor"),
    "build_lsp_toolset": ("lsp_tools", "build_lsp_toolset"),
}

_resolved: dict[str, Any] = {}


def _resolve(name: str) -> Any:
    """Import the concern module and return the named attribute (cached)."""
    try:
        return _resolved[name]
    except KeyError:
        pass
    mod_name, attr = _TOOL_IMPLS[name]
    obj = getattr(import_module(f".{mod_name}", __package__), attr)
    _resolved[name] = obj
    return obj
```

Rewrite `_register_read_tools` / `_register_action_tools` / `_register_jobs` / `lsp_toolset` / `register_subagent` to call `_resolve("read_file")` etc. instead of `fs_tools.read_file`.

For subagents:

```python
fn = _resolve(name) if name in _TOOL_IMPLS else None
# only the former _SUBAGENT_FNS keys are valid — keep a frozenset of subagent-eligible names
```

Define `_SUBAGENT_NAMES = frozenset({...})` matching today's `_SUBAGENT_FNS` keys so unknown names still skip.

Update the module docstring: tools are still owned by concern modules; this file resolves them lazily.

- [ ] **Step 4: Run invariants + provider tests**

Run:

```bash
uv run pytest --no-cov \
  tests/test_cli_startup.py::test_tools_provider_import_does_not_load_markdownify \
  tests/test_cli_startup.py::test_tools_provider_import_does_not_load_bs4 \
  tests/test_cli_startup.py::test_tools_provider_import_does_not_load_pydantic_ai \
  tests/test_provider.py tests/test_subagent_tool.py -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/tools/provider.py tests/test_cli_startup.py
git commit -m "$(cat <<'EOF'
perf(tools): lazy-resolve built-in tool callables

Importing tools.provider no longer pulls concern modules, markdownify/bs4,
or pydantic_ai via runtime.deps. Callables resolve on first register/grant.
EOF
)"
```

---

### Task 2: Clean the StatusBar / usage / compaction import edge

Importing `StatusBar` today loads `compaction` → `pydantic_ai`. Defer those imports into the methods that need them.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/status_bar.py`
- Modify: `src/marim_harness/usage.py` only if still required after status_bar fix (prefer fixing call sites first)
- Test: `tests/test_cli_startup.py`

**Interfaces:**
- Produces: `import marim_harness.interfaces.tui.widgets.status_bar` does not load `pydantic_ai`

- [ ] **Step 1: Failing test**

```python
def test_status_bar_import_does_not_load_pydantic_ai():
    assert not _module_loads(
        "marim_harness.interfaces.tui.widgets.status_bar", "pydantic_ai"
    )
```

- [ ] **Step 2: Run — expect FAIL**

`uv run pytest --no-cov tests/test_cli_startup.py::test_status_bar_import_does_not_load_pydantic_ai -v`

- [ ] **Step 3: Lazy imports in status_bar**

Remove top-level:

```python
from ....compaction import estimate_tokens
from ....usage import resolve_cost
```

Inside `_context_tokens`:

```python
from ....compaction import estimate_tokens
```

Inside `_session_cost`:

```python
from ....usage import resolve_cost
```

Keep behavior identical.

- [ ] **Step 4: Run test + status bar tests**

```bash
uv run pytest --no-cov \
  tests/test_cli_startup.py::test_status_bar_import_does_not_load_pydantic_ai \
  tests/test_tui_status_bar.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/status_bar.py tests/test_cli_startup.py
git commit -m "perf(tui): keep StatusBar import off the pydantic_ai path"
```

---

### Task 3: Clean stream_render / session_view / commands import edges

These are imported by `app.py` at module top. Each must stop pulling `pydantic_ai` (and harness → provider) at import time.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py`
- Modify: `src/marim_harness/interfaces/tui/session_view.py`
- Modify: `src/marim_harness/interfaces/tui/commands.py`
- Test: `tests/test_cli_startup.py`

**Interfaces:**
- Produces:
  - `import …stream_render` → no `pydantic_ai`
  - `import …session_view` → no `pydantic_ai`, no `markdownify`
  - `import …commands` → no `pydantic_ai`

- [ ] **Step 1: Failing tests**

```python
def test_stream_render_import_does_not_load_pydantic_ai():
    assert not _module_loads(
        "marim_harness.interfaces.tui.stream_render", "pydantic_ai"
    )


def test_session_view_import_does_not_load_pydantic_ai():
    assert not _module_loads(
        "marim_harness.interfaces.tui.session_view", "pydantic_ai"
    )


def test_commands_import_does_not_load_pydantic_ai():
    assert not _module_loads(
        "marim_harness.interfaces.tui.commands", "pydantic_ai"
    )
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3a: `stream_render.py`**

Move:

```python
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from ...usage import resolve_cost
```

behind a small helper or per-function imports. Preferred pattern (one cache, low noise):

```python
def _pai_messages():
    from pydantic_ai import messages as m
    return m
```

Then `_is_text_start` becomes:

```python
def _is_text_start(event) -> bool:
    m = _pai_messages()
    return isinstance(event, m.PartStartEvent) and isinstance(event.part, m.TextPart)
```

Apply the same to every `isinstance` / constructor use. Lazy-import `resolve_cost` inside the method(s) that call it.

- [ ] **Step 3b: `session_view.py`**

Replace:

```python
from ...compaction import summary_text
from ...runtime.harness import strip_turn_context
```

with:

```python
from ...runtime.context import strip_turn_context
```

and lazy-import `summary_text` inside the method that formats compaction notices:

```python
from ...compaction import summary_text
```

`stream_render` imports stay; after 3a they are clean.

- [ ] **Step 3c: `commands.py`**

Replace top-level `from ...mcp.manager import McpStatus` with a lazy import inside the MCP status handler(s) that reference `McpStatus`. Confirm with grep that no module-level annotation needs it (use `TYPE_CHECKING` if so).

- [ ] **Step 4: Run invariants + a slice of TUI tests**

```bash
uv run pytest --no-cov \
  tests/test_cli_startup.py::test_stream_render_import_does_not_load_pydantic_ai \
  tests/test_cli_startup.py::test_session_view_import_does_not_load_pydantic_ai \
  tests/test_cli_startup.py::test_commands_import_does_not_load_pydantic_ai \
  tests/test_app_decomposition.py tests/test_tui_compact_notice.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  src/marim_harness/interfaces/tui/stream_render.py \
  src/marim_harness/interfaces/tui/session_view.py \
  src/marim_harness/interfaces/tui/commands.py \
  tests/test_cli_startup.py
git commit -m "perf(tui): keep stream/session/commands imports off pydantic_ai"
```

---

### Task 4: Clean `HarnessApp` module import

After Tasks 1–3, `app.py` still imports `Harness`, `ToolDenied`, and possibly other heavy symbols at top level.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (imports + annotations only in this task — behavior change is Task 6)
- Test: `tests/test_cli_startup.py`

**Interfaces:**
- Produces: `from marim_harness.interfaces.tui.app import HarnessApp` does not load `pydantic_ai` or `markdownify`

- [ ] **Step 1: Failing tests**

```python
def test_harness_app_import_does_not_load_pydantic_ai():
    assert not _module_loads(
        "marim_harness.interfaces.tui.app", "pydantic_ai"
    )


def test_harness_app_import_does_not_load_markdownify():
    assert not _module_loads(
        "marim_harness.interfaces.tui.app", "markdownify"
    )
```

- [ ] **Step 2: Run — expect FAIL** (until imports cleaned)

- [ ] **Step 3: Make app.py import-light**

1. Add `from __future__ import annotations` if not present.
2. Move `from ...runtime.harness import Harness` under `TYPE_CHECKING`.
3. Remove top-level:

   ```python
   from pydantic_ai import ToolDenied
   from pydantic_ai.tools import DeferredToolApprovalResult
   ```

   Import them inside the approval/result helpers that construct/return those types (grep `ToolDenied` / `DeferredToolApprovalResult` in `app.py` and localize).

4. Grep `app.py` for any other `pydantic_ai` / `harness` / `bootstrap` / `provider` runtime imports at module top; eliminate or TYPE_CHECKING them.

5. Keep `Harness` in type comments only; runtime `isinstance` checks against Harness should use duck typing or a lazy import inside the method.

Do **not** change constructor signature yet (Task 6). Existing tests that pass a live `Harness` must still work.

- [ ] **Step 4: Verify**

```bash
uv run pytest --no-cov \
  tests/test_cli_startup.py::test_harness_app_import_does_not_load_pydantic_ai \
  tests/test_cli_startup.py::test_harness_app_import_does_not_load_markdownify \
  tests/test_app.py -q --tb=no
```

Expected: import invariants PASS; `test_app.py` PASS (constructor still takes Harness).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py tests/test_cli_startup.py
git commit -m "perf(tui): import HarnessApp without pydantic_ai or markdownify"
```

---

### Task 5: Bootstrap import hygiene

Defer heavy top-level imports in `bootstrap.py` into `build_harness` / `build_lsp_registry`.

**Files:**
- Modify: `src/marim_harness/runtime/bootstrap.py`
- Test: `tests/test_cli_startup.py`, existing `tests/test_bootstrap.py`

**Interfaces:**
- Produces: `import marim_harness.runtime.bootstrap` does not load `pydantic_ai` (or documents residual if a light dependency still does — target is no `pydantic_ai`)
- `build_harness` / `build_lsp_registry` signatures unchanged

- [ ] **Step 1: Failing test**

```python
def test_bootstrap_import_does_not_load_pydantic_ai():
    assert not _module_loads("marim_harness.runtime.bootstrap", "pydantic_ai")
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Move imports**

Keep at module top only light deps (`logging`, `Path`, `TYPE_CHECKING`, `CommandPolicy` if still light — verify with subprocess).

Move into `build_harness` (or narrow helpers it calls):

- `make_summarizer`, `make_titler`
- `load_config`, `ModelSource`, `MultiModelSource`, `detect_active_providers`, `build_context_limits`
- `HookRunner`, `load_hooks_config`
- `bundled_lsp_providers`, `LspRegistry`, `plugin_lsp_providers`
- `build_mcp_servers`, `disabled_server_names`, `load_mcp_config`
- `Notifier`, `SessionManager`, `aux_model_for`
- `resolve_project_trust`, `scan_project_surface`
- `Deps`, `TrustState`, `UIHooks`, `WorkspaceConfig`
- `Harness`, `Mode` (Mode is light — may stay top-level)
- `HarnessBuilder` (already function-local — keep)

Move into `build_lsp_registry` what only it needs.

Use `TYPE_CHECKING` for return annotations (`Harness`, `LspRegistry`, `StatsLedger`).

Watch cyclomatic complexity: if `build_harness` grows past C901, extract `_load_startup_collaborators(...)` rather than noqa.

- [ ] **Step 4: Run**

```bash
uv run pytest --no-cov \
  tests/test_cli_startup.py::test_bootstrap_import_does_not_load_pydantic_ai \
  tests/test_bootstrap.py tests/test_lsp_bootstrap_gate.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/runtime/bootstrap.py tests/test_cli_startup.py
git commit -m "perf(bootstrap): defer heavy imports into build_harness"
```

---

### Task 6: `TuiLaunch` + early paint + readiness gate + fatal failure

Wire the user-visible behavior from the spec.

**Files:**
- Create: `src/marim_harness/interfaces/tui/launch.py`
- Modify: `src/marim_harness/interfaces/cli/default_cmd.py`
- Modify: `src/marim_harness/interfaces/tui/app.py`
- Create: `tests/test_tui_startup.py`
- Test: existing `tests/test_app.py` etc. must accept both ready harness and (where updated) launch path

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class TuiLaunch:
    workspace: Path
    mode: Mode | None
    resume: bool
    model_label: str

def env_model_label() -> str: ...
```

- `HarnessApp.__init__(self, launch: TuiLaunch | Harness, history: PromptHistory | None = None)`
  - If given a `Harness` (tests / embedders): behave as today (bind_ui immediately; `self._ready` already set).
  - If given `TuiLaunch`: `self.harness = None` until worker finishes; do not call `bind_ui` in `__init__`.
- `default_cmd` TUI branch: `HarnessApp(TuiLaunch(...), history=...).run()` — no pre-run `build_harness`.

- [ ] **Step 1: Write failing unit tests** (`tests/test_tui_startup.py`)

```python
"""Early-paint / readiness tests for HarnessApp."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from marim_harness.interfaces.history import PromptHistory
from marim_harness.interfaces.tui.launch import TuiLaunch
from marim_harness.runtime.permissions import Mode


def test_env_model_label_format(monkeypatch):
    from marim_harness.interfaces.tui.launch import env_model_label

    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("MARIM_MODEL", "anthropic/claude-sonnet-4-6")
    label = env_model_label()
    assert "openrouter" in label
    assert "claude" in label.lower() or "anthropic" in label


@pytest.mark.anyio
async def test_launch_path_disables_prompt_until_ready(tmp_path: Path, monkeypatch):
    """With TuiLaunch, PromptInput starts disabled; fake ready enables it."""
    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.interfaces.tui.widgets import PromptInput

    launch = TuiLaunch(
        workspace=tmp_path, mode=Mode.ask, resume=False, model_label="test/model"
    )

    # Prevent real build_harness inside the worker.
    async def _fake_build(self):
        raise RuntimeError("build should be stubbed by test")

    monkeypatch.setattr(HarnessApp, "_build_harness_async", _fake_build)

    app = HarnessApp(launch, history=PromptHistory())
    # Drive only compose/mount pieces: stub the worker start so on_mount's
    # build path is replaced by the test.
    async def _noop_start(self):
        return None

    monkeypatch.setattr(HarnessApp, "_start_harness_build", _noop_start)

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptInput)
        assert prompt.disabled or not app.is_harness_ready
        assert app.harness is None


@pytest.mark.anyio
async def test_construction_failure_exits(tmp_path: Path, monkeypatch):
    """Worker failure exits the app with a non-zero code (v1 fatal)."""
    from marim_harness.interfaces.tui.app import HarnessApp

    launch = TuiLaunch(
        workspace=tmp_path, mode=Mode.ask, resume=False, model_label="test/model"
    )

    async def _boom(self):
        raise RuntimeError("simulated construct failure")

    monkeypatch.setattr(HarnessApp, "_build_harness_async", _boom)

    app = HarnessApp(launch, history=PromptHistory())
    # run_test + force the build path; assert exit
    with pytest.raises(SystemExit) as ei:
        async with app.run_test() as pilot:
            await pilot.pause()
            # allow worker to surface
            await pilot.pause(0.1)
    assert ei.value.code not in (0, None)


@pytest.mark.anyio
async def test_existing_harness_ctor_still_ready(tmp_path: Path):
    """Tests that pass a live Harness keep today's ready-immediately behavior."""
    from pydantic_ai.models.test import TestModel

    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider
    from tests.conftest import _make_deps

    harness = Harness(
        TestModel(call_tools=[]),
        BuiltinToolProvider(),
        _make_deps(tmp_path),
        instructions="test",
    )
    app = HarnessApp(harness)
    assert app.is_harness_ready
    assert app.harness is harness
```

Refine the failure-exit test to match Textual's actual exit API used in the implementation (`app.exit(return_code=1)` then outer `run()` returning / SystemExit). Prefer asserting `app.return_code == 1` after the worker completes if `run_test` does not raise — **adjust the test to the mechanism you implement, but keep the contract: non-zero exit**.

- [ ] **Step 2: Run — expect FAIL** (no `launch.py` / no readiness API)

- [ ] **Step 3: Add `launch.py`**

```python
"""Light TUI launch context — no agent / pydantic_ai imports."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ...runtime.permissions import Mode


@dataclass(frozen=True)
class TuiLaunch:
    workspace: Path
    mode: Mode | None
    resume: bool
    model_label: str


def env_model_label() -> str:
    """Display-only provider/model label from env (pre-harness status bar)."""
    provider = (os.getenv("MARIM_PROVIDER") or "openrouter").strip().lower()
    model = (os.getenv("MARIM_MODEL") or "").strip()
    if model:
        return f"{provider}:{model}"
    return provider
```

- [ ] **Step 4: Implement readiness + worker in `HarnessApp`**

Core shape:

```python
def __init__(self, launch: TuiLaunch | Harness, history: PromptHistory | None = None) -> None:
    super().__init__()
    self._history = history if history is not None else PromptHistory()
    self.status = StatusBar()
    self.stream = StreamRenderer(self)
    self.session = SessionView(self)
    # ... queue, wake placeholders that need harness: defer WakeDriver until ready
    self._launch: TuiLaunch | None
    self.harness: Harness | None
    self._harness_ready: asyncio.Event = asyncio.Event()
    self._build_worker = None

    if isinstance(launch, TuiLaunch):  # use type name check via lazy import if needed
        self._launch = launch
        self.harness = None
        self.status.model_name = launch.model_label
        self.status.mode = launch.mode.value if launch.mode else ""
        # do NOT bind_ui yet; WakeDriver needs harness — create in _on_harness_ready
    else:
        self._launch = None
        self.harness = launch
        self._harness_ready.set()
        self._bind_harness_ui(launch)
        # existing wake/notifier setup that referenced harness.*
```

Extract today's `bind_ui(...)` block into `_bind_harness_ui(self, harness)`.

`on_mount`:

```python
async def on_mount(self) -> None:
    # themes, intervals, focus — anything that does not need harness
    ...
    if self.harness is None:
        self.query_one(PromptInput).disabled = True
        self.status.model_name = self._launch.model_label  # type: ignore[union-attr]
        self.sub_title = str(self._launch.workspace)
        # mount welcome only (no history yet)
        ...
        self._build_worker = self.run_worker(
            self._build_and_bind(),
            group="harness-build",
            exclusive=True,
            exit_on_error=False,  # we handle failure ourselves
        )
        return
    await self._post_harness_mount()  # today's body from history replay onward
```

```python
async def _build_and_bind(self) -> None:
    try:
        harness = await self._build_harness_async()
    except Exception as exc:
        logger.exception("harness construction failed")
        self._append_log(ErrorMessage(f"Failed to start: {exc}"))  # or NoticeMessage
        self.exit(return_code=1)
        return
    self.harness = harness
    self._bind_harness_ui(harness)
    await self._post_harness_mount()
    self.query_one(PromptInput).disabled = False
    self._harness_ready.set()

async def _build_harness_async(self):
    """Run sync build_harness off the event loop."""
    import asyncio
    from ...runtime.bootstrap import build_harness

    assert self._launch is not None
    launch = self._launch
    return await asyncio.to_thread(
        build_harness,
        launch.workspace,
        mode=launch.mode,
        resume=launch.resume,
    )
```

**WakeDriver / autonomous_wake:** today's `__init__` reads `harness.autonomous_wake`. On the launch path, set defaults (`autonomous_wake=False` or from env if already available without harness) and construct `WakeDriver` inside `_bind_harness_ui` / `_on_harness_ready`.

**Guard harness access:** add:

```python
@property
def is_harness_ready(self) -> bool:
    return self.harness is not None and self._harness_ready.is_set()
```

At the top of agent-facing actions (`_start_turn`, `action_cycle_mode`, model picker, …), if not ready: `self._append_log(NoticeMessage("Still starting…"))` and return. Pure UI (quit, scroll) stays open.

**Quit mid-build:** in the quit path / `on_unmount`, cancel `self._build_worker` if still running.

**Status bar while harness is None:** `status_bar._context_tokens` / `_session_cost` currently assume `app.harness`. Guard:

```python
if getattr(app, "harness", None) is None:
    return 0  # or None for cost
```

Do this in the same task so idle clock ticks during startup do not crash.

- [ ] **Step 5: Update `default_cmd.py` TUI branch**

```python
    from ..tui.app import HarnessApp
    from ..tui.launch import TuiLaunch, env_model_label
    from ...runtime.permissions import Mode

    mode = Mode(args.mode) if args.mode else None
    launch = TuiLaunch(
        workspace=workspace,
        mode=mode,
        resume=args.resume,
        model_label=env_model_label(),
    )
    HarnessApp(launch, history=PromptHistory(default_history_path())).run()
    return 0
```

Remove the TUI-path `build_harness` call. Keep headless path as-is (`build_harness` then `run_headless`).

Ensure `Mode` import stays after argparse (already deferred) so `--help` remains fast.

- [ ] **Step 6: Run startup + app tests**

```bash
uv run pytest --no-cov \
  tests/test_tui_startup.py \
  tests/test_app.py \
  tests/test_app_decomposition.py \
  tests/test_app_turn_race.py \
  tests/test_trust_panel.py \
  tests/test_cli_startup.py -q
```

Expected: PASS. Fix any test helpers that assume `app.harness` is never None only after mount if they use `TuiLaunch` (they should keep passing a live `Harness`).

- [ ] **Step 7: Commit**

```bash
git add \
  src/marim_harness/interfaces/tui/launch.py \
  src/marim_harness/interfaces/tui/app.py \
  src/marim_harness/interfaces/tui/widgets/status_bar.py \
  src/marim_harness/interfaces/cli/default_cmd.py \
  tests/test_tui_startup.py \
  tests/test_cli_startup.py
git commit -m "$(cat <<'EOF'
feat(tui): paint before harness construction

TuiLaunch drives HarnessApp; build_harness runs on a worker after first
paint. Prompt stays disabled until ready; construction failure exits 1.
EOF
)"
```

---

### Task 7: Measurement + full verification

**Files:** none required (optional note in commit message); do not add a flaky CI timing gate.

- [ ] **Step 1: Import-path timing (fresh interpreters)**

```bash
.venv/bin/python -c "
import time, subprocess, sys
def t(code):
    s=time.perf_counter()
    subprocess.check_call([sys.executable,'-c',code])
    print(round(time.perf_counter()-s,3),'s')
print('HarnessApp import:', end=' ')
t('from marim_harness.interfaces.tui.app import HarnessApp')
print('provider import:', end=' ')
t('import marim_harness.tools.provider')
print('bootstrap import:', end=' ')
t('import marim_harness.runtime.bootstrap')
"
```

Expected: HarnessApp import well under 1s (target path to first frame ≤0.5s once Textual runs); provider/bootstrap without pydantic_ai.

- [ ] **Step 2: Manual TUI smoke**

```bash
uv run marim
# confirm: screen appears quickly with "starting…" / disabled prompt
# then prompt enables; /help works; one trivial turn works; ctrl+c quit works
# optional: MARIM_DEBUG=1 and break build_harness to see exit 1 path
```

- [ ] **Step 3: Full quality gate**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all green.

- [ ] **Step 4: Final commit only if measurement notes or tiny fixes remain**

```bash
# if needed:
git commit -m "test(tui): lock startup import invariants after measurement"
```

---

## Spec coverage checklist

| Spec item | Task |
|-----------|------|
| Early paint before `build_harness` | 6 |
| Lazy tool modules / no markdownify on ready path | 1 |
| Bootstrap import hygiene | 5 |
| Readiness gate | 6 |
| Fatal construction failure | 6 |
| `TuiLaunch` light context | 6 |
| `bind_ui` after harness exists | 6 |
| Headless unchanged | 6 (`default_cmd` headless branch) |
| Import invariants (provider, app, bootstrap) | 1–5, 7 |
| Clean `HarnessApp` import graph (required for paint target) | 2–4 |
| Status bar safe while `harness is None` | 6 |
| Quit cancels build worker | 6 |
| Measurement pass | 7 |
| No lazy TUI math/settings (out of scope) | — |
| No retry UI (out of scope) | — |

## Placeholder / consistency review

- No TBD steps; failure-exit test allows adapting assert style to Textual `exit(return_code=)` vs `SystemExit`.
- `TuiLaunch` / `env_model_label` / `is_harness_ready` / `_build_harness_async` / `_bind_harness_ui` / `_post_harness_mount` names are consistent across tasks.
- Dual constructor (`TuiLaunch | Harness`) preserves existing unit tests without a mass rewrite.
