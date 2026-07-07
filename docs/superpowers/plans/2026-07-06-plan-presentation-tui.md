# TUI Plan Presentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent's plan (from plan mode's `present_plan`) a first-class TUI element: a dedicated inline Plan card with the handoff choices built in, a compact persistent plan title on the TaskPanel, and a full-height on-demand `PlanScreen` overlay on Ctrl+P.

**Architecture:** A new `CurrentPlan` value object on `Deps` holds the plan narrative (summary/steps/path); step *progress* stays in `deps.tasks` (single source of truth). `present_plan` sets `deps.plan` and, when a UI is attached, drives the handoff through a new `on_present_plan` callback (parallel to `ask_user`) that mounts a `PlanCard` inline panel. The TaskPanel gains a one-line plan title; a `PlanScreen` (pushed like Settings, not a modal) shows the full plan with live progress.

**Tech Stack:** Python ≥3.10, Textual (optional `tui` extra), pytest + `pytest.mark.anyio` + Textual `run_test()` pilot.

**Spec:** `docs/superpowers/specs/2026-07-06-plan-presentation-tui-design.md`

## Global Constraints

- `requires-python = ">=3.10"` — no 3.11+-only syntax (use `X | None`, not `Optional` churn; no `asyncio.timeout`).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (imports sorted).
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`.
- Verify order before claiming done (matches CI): ruff → pyright → pytest.
- No live/paid models in any test — `FunctionModel`/`TestModel` or direct calls only.
- Preserve existing long "why" comments when editing near them.
- The four plan-execution choice labels are the single source of truth in `_PLAN_CHOICES` (`tools/planning_tools.py`); they must stay in exact sync with `_PLAN_EXEC_MODES` keys (`"Execute hands-off (auto)"` → `Mode.auto`, `"Execute step-by-step (ask)"` → `Mode.ask`). The `PlanCard` renders whatever `Choice` list it is handed — never hardcode these labels in the TUI layer.
- `present_plan` must remain correct headless: when no UI is attached, it saves the plan and stays in plan mode. Every new callback is `None` there and each reader guards with `is None`.

## File Structure

Create:
- `src/marim_harness/interfaces/tui/plan_card.py` — `PlanCard` inline panel (summary + steps + choices)
- `src/marim_harness/interfaces/tui/plan_screen.py` — `PlanScreen` full-height overlay
- Tests: `tests/test_plan_card.py`, `tests/test_plan_screen.py`, `tests/test_present_plan_tool.py`

Modify:
- `src/marim_harness/runtime/deps.py` — `CurrentPlan` dataclass, `OnPresentPlanFn` type, `UIHooks.on_present_plan`, `Deps.plan`
- `src/marim_harness/tools/planning_tools.py` — set `deps.plan`; drive handoff via `on_present_plan` when wired
- `src/marim_harness/runtime/harness.py` — `bind_ui` gains `on_present_plan`
- `src/marim_harness/interfaces/tui/app.py` — `_present_plan` method, wire in `bind_ui`, plan title in `_render_tasks`, `action_show_plan` + Ctrl+P binding
- `src/marim_harness/interfaces/tui/widgets/panels.py` — `TaskPanel` plan title line
- `tests/conftest.py` — add `on_present_plan` to `_UI_HOOK_FIELDS`

---

### Task 1: `CurrentPlan` state + `on_present_plan` callback plumbing

**Files:**
- Modify: `src/marim_harness/runtime/deps.py` (add `Choice` import, `CurrentPlan`, `OnPresentPlanFn`, `UIHooks.on_present_plan`, `Deps.plan`)
- Modify: `tests/conftest.py` (`_UI_HOOK_FIELDS`)
- Test: `tests/test_deps_plan.py`

**Interfaces:**
- Produces:
  - `CurrentPlan` — frozen dataclass `summary: str`, `steps: list[str]`, `path: str | None`.
  - `OnPresentPlanFn = Callable[[str, list[str], list[Choice]], Awaitable[str]]` — returns the chosen execution-choice label.
  - `UIHooks.on_present_plan: OnPresentPlanFn | None = None`.
  - `Deps.plan: CurrentPlan | None = None`.
  Tasks 2, 4, 5, 6 consume these.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deps_plan.py
"""The CurrentPlan holder and the on_present_plan callback field on Deps/UIHooks."""

from marim_harness.runtime.deps import CurrentPlan, Deps, UIHooks, WorkspaceConfig
from pathlib import Path


def test_current_plan_holds_narrative():
    plan = CurrentPlan(summary="do the thing", steps=["a", "b"], path="/tmp/p.md")
    assert plan.summary == "do the thing"
    assert plan.steps == ["a", "b"]
    assert plan.path == "/tmp/p.md"


def test_deps_plan_defaults_none_and_uihooks_callback_defaults_none():
    deps = Deps(workspace=WorkspaceConfig(root=Path("/tmp")))
    assert deps.plan is None
    assert deps.ui.on_present_plan is None


def test_deps_plan_is_assignable():
    deps = Deps(workspace=WorkspaceConfig(root=Path("/tmp")))
    deps.plan = CurrentPlan(summary="s", steps=["x"], path=None)
    assert deps.plan.steps == ["x"]
    assert isinstance(deps.ui, UIHooks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_deps_plan.py -v`
Expected: FAIL — `ImportError: cannot import name 'CurrentPlan'`

- [ ] **Step 3: Implement in `runtime/deps.py`**

Add `Choice` to the existing ask_user import (currently `from ..ask_user import Question`):

```python
from ..ask_user import Choice, Question
```

Add the `OnPresentPlanFn` type alias next to the other callback aliases (near `AskUserFn`, around line 71), with a docstring comment matching the file's style:

```python
# (summary, steps, choices) -> the chosen execution-choice label. Wired by the
# TUI (mounts a PlanCard inline panel); None when headless, where present_plan
# falls back to ask_user and then to "save and stay in plan mode". The choices
# are passed through so the card never hardcodes the plan-execution labels
# (their single source of truth is tools/planning_tools._PLAN_CHOICES).
OnPresentPlanFn = Callable[[str, list[str], list[Choice]], Awaitable[str]]
```

Add a `CurrentPlan` dataclass just above `@dataclass class UIHooks` (around line 126):

```python
@dataclass(frozen=True)
class CurrentPlan:
    """The plan narrative from the most recent present_plan this session: the
    summary paragraph, the ordered steps, and the plan-file path (None if the
    write failed). Step *progress* is NOT here — it lives in ``Deps.tasks``, the
    single source of truth for done/in-progress/pending. This holds only what
    the pinned TaskPanel title and the PlanScreen overlay need to show the
    'why' after the transient PlanCard scrolls away."""

    summary: str
    steps: list[str]
    path: str | None
```

Add the field to `UIHooks` (after `on_mode_change`):

```python
    on_present_plan: "OnPresentPlanFn | None" = None
```

Add the field to `Deps` (after `jobs`, before `services` — keep it near the other live UI-reflected state):

```python
    # The most recent plan presented this session (present_plan sets it); read
    # by the TaskPanel title and the PlanScreen overlay. None until a plan is
    # presented. Narrative only — step progress lives in ``tasks``.
    plan: "CurrentPlan | None" = None
```

In `tests/conftest.py`, add `"on_present_plan"` to the `_UI_HOOK_FIELDS` set so `_make_deps(..., on_present_plan=fn)` routes it into `UIHooks`:

```python
_UI_HOOK_FIELDS = {
    "request_approval", "ask_user", "on_present_plan", "on_subagent_event",
    "on_subagent_notice", "on_subagent_model", "on_subagent_usage",
    "detach_fanout", "interactive", "notifier",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_deps_plan.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/deps.py tests/conftest.py tests/test_deps_plan.py
git commit -m "feat(deps): CurrentPlan holder + on_present_plan callback"
```

---

### Task 2: `present_plan` sets `deps.plan` and drives the handoff via `on_present_plan`

**Files:**
- Modify: `src/marim_harness/tools/planning_tools.py:84-157` (the `present_plan` body)
- Test: `tests/test_present_plan_tool.py`

**Interfaces:**
- Consumes: `CurrentPlan` from Task 1; `deps.ui.on_present_plan`.
- Produces: `present_plan` now sets `ctx.deps.plan = CurrentPlan(summary, clean, str(path) if path else None)` before the handoff, and prefers `on_present_plan(summary, clean, _PLAN_CHOICES)` over `ask_user` when it is wired. The choice→mode mapping (`_PLAN_EXEC_MODES`) is unchanged. Task 4's app callback returns one of the four `_PLAN_CHOICES` labels.

- [ ] **Step 1: Write the failing test**

`present_plan` only ever touches `ctx.deps`, so a `SimpleNamespace(deps=deps)` is a sufficient stand-in for `RunContext[Deps]` (Python does not enforce the annotation at runtime — this keeps the test off the full turn engine).

```python
# tests/test_present_plan_tool.py
"""present_plan's UI-handoff branch: sets deps.plan, prefers on_present_plan over
ask_user, and stays correct when neither is wired (headless)."""

from types import SimpleNamespace

import pytest
from conftest import _make_deps

from marim_harness.runtime.permissions import Mode
from marim_harness.tools.planning_tools import _PLAN_CHOICES, present_plan

pytestmark = pytest.mark.anyio


async def test_on_present_plan_preferred_and_sets_plan(tmp_path):
    seen = {}

    async def fake_present(summary, steps, choices):
        seen["summary"] = summary
        seen["steps"] = steps
        seen["choices"] = [c.label for c in choices]
        return "Execute hands-off (auto)"

    deps = _make_deps(tmp_path, mode=Mode.plan, on_present_plan=fake_present)
    ctx = SimpleNamespace(deps=deps)

    result = await present_plan(ctx, "Refactor the parser.", ["Extract tokenizer", "Add tests"])

    # deps.plan carries the narrative.
    assert deps.plan is not None
    assert deps.plan.summary == "Refactor the parser."
    assert deps.plan.steps == ["Extract tokenizer", "Add tests"]
    # on_present_plan was called with the summary, steps, and the canonical choices.
    assert seen["summary"] == "Refactor the parser."
    assert seen["steps"] == ["Extract tokenizer", "Add tests"]
    assert seen["choices"] == [c.label for c in _PLAN_CHOICES]
    # The chosen label flipped the mode in place.
    assert deps.workspace.mode is Mode.auto
    assert "auto" in result


async def test_falls_back_to_ask_user_when_no_present_plan(tmp_path):
    async def fake_ask(questions):
        return {questions[0].header: "Execute step-by-step (ask)"}

    deps = _make_deps(tmp_path, mode=Mode.plan, ask_user=fake_ask)  # on_present_plan unset
    ctx = SimpleNamespace(deps=deps)

    await present_plan(ctx, "s", ["one"])
    assert deps.workspace.mode is Mode.ask  # ask_user answer honored
    assert deps.plan is not None            # plan still recorded


async def test_headless_saves_and_stays_in_plan_mode(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.plan)  # neither callback wired
    ctx = SimpleNamespace(deps=deps)

    result = await present_plan(ctx, "s", ["one"])
    assert deps.workspace.mode is Mode.plan  # unchanged
    assert deps.plan is not None             # narrative still stored
    assert "plan mode" in result.lower()


async def test_dismissed_card_keeps_planning(tmp_path):
    async def fake_present(summary, steps, choices):
        return "Keep planning"

    deps = _make_deps(tmp_path, mode=Mode.plan, on_present_plan=fake_present)
    ctx = SimpleNamespace(deps=deps)

    await present_plan(ctx, "s", ["one"])
    assert deps.workspace.mode is Mode.plan  # no flip on "Keep planning"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_present_plan_tool.py -v`
Expected: FAIL — `AttributeError: 'Deps' object has no attribute 'plan'` is already fixed by Task 1, so the real failure is the mode not flipping / `on_present_plan` not called (present_plan still only calls ask_user).

- [ ] **Step 3: Implement in `tools/planning_tools.py`**

Add the import at the top (next to the other runtime.deps import):

```python
from ..runtime.deps import CurrentPlan, Deps
```

Replace the body from `ctx.deps.tasks.replace(...)` through the `answers = await ctx.deps.ui.ask_user(...)` / `choice = ...` lines (currently lines 122-134) with:

```python
    ctx.deps.tasks.replace([Task(text=s) for s in clean])
    # Record the plan narrative so the pinned TaskPanel title and the PlanScreen
    # overlay can show the "why" after the transient PlanCard scrolls away. Step
    # progress stays in ctx.deps.tasks (set just above) — the single source of
    # truth — so this holds only summary/steps/path.
    ctx.deps.plan = CurrentPlan(summary=summary, steps=clean, path=str(path) if path else None)

    # Prefer the dedicated plan card (on_present_plan) when a UI is attached; it
    # renders the plan + choices as one deliberate inline moment. Fall back to the
    # generic ask_user panel if only that is wired, then to the headless path.
    if ctx.deps.ui.on_present_plan is not None:
        choice = await ctx.deps.ui.on_present_plan(summary, clean, _PLAN_CHOICES)
    elif ctx.deps.ui.ask_user is not None:
        answers = await ctx.deps.ui.ask_user(
            [Question(question="How should I execute this plan?", header="execution",
                      options=_PLAN_CHOICES)]
        )
        choice = (answers or {}).get("execution", "Keep planning")
    else:
        return (
            f"Plan saved{f' to {path}' if path else ''}. No interactive UI, so "
            "staying in plan mode — share the plan and await direction."
        )
```

Then DELETE the old `if ctx.deps.ui.ask_user is None:` early-return block that preceded the `answers = ...` call (lines 124-134 in the original), since the no-UI case is now the `else` branch above. The remaining `new_mode = _PLAN_EXEC_MODES.get(...)` block and everything after it is unchanged — `choice` feeds it exactly as before.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_present_plan_tool.py tests/test_plan_mode_e2e.py -v`
Expected: PASS (the e2e test still uses `ask_user`, which is now the fallback branch — must stay green)

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/tools/planning_tools.py tests/test_present_plan_tool.py
git commit -m "feat(plan): present_plan records deps.plan and prefers on_present_plan"
```

---

### Task 3: `PlanCard` inline widget

**Files:**
- Create: `src/marim_harness/interfaces/tui/plan_card.py`
- Test: `tests/test_plan_card.py`

**Interfaces:**
- Consumes: `InteractionPanel` + `run_panel` (`interfaces/tui/interaction_panel.py`); `Choice` (`ask_user.py`).
- Produces: `PlanCard(summary: str, steps: list[str], choices: list[Choice])`, an `InteractionPanel` subclass that resolves (via `self.result`) to the selected choice's **label** (a `str`), or `"Keep planning"` on Escape. Task 4 mounts it via `run_panel`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan_card.py
"""PlanCard inline panel: renders summary + steps + choices, resolves to the
chosen label, defaults to 'Keep planning' on Escape."""

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from marim_harness.ask_user import Choice
from marim_harness.interfaces.tui.interaction_panel import run_panel
from marim_harness.interfaces.tui.plan_card import PlanCard

pytestmark = pytest.mark.anyio

_CHOICES = [
    Choice("Execute hands-off (auto)", "Run the whole plan."),
    Choice("Execute step-by-step (ask)", "Approve each change."),
    Choice("Hand off to sub-agent", "Spawn a sub-agent."),
    Choice("Keep planning", "Save as a draft."),
]


class _Harness(App):
    def __init__(self, summary, steps, choices):
        super().__init__()
        self._args = (summary, steps, choices)
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static("line\n" * 100), id="log")
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        self.run_worker(self._run())

    async def _run(self) -> None:
        self.result = await run_panel(self, PlanCard(*self._args))


async def test_selects_highlighted_choice():
    app = _Harness("Refactor parser", ["Extract tokenizer", "Add tests"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # first option highlighted
        await pilot.pause()
    assert app.result == "Execute hands-off (auto)"


async def test_selects_second_choice():
    app = _Harness("Refactor parser", ["Extract tokenizer"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "Execute step-by-step (ask)"


async def test_escape_keeps_planning():
    app = _Harness("Refactor parser", ["Extract tokenizer"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result == "Keep planning"


async def test_renders_summary_and_steps():
    app = _Harness("Refactor the parser", ["Extract tokenizer", "Add tests"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        card = app.query_one(PlanCard)
        text = card.query_one("#plan-summary", Static).renderable
        body = card.query_one("#plan-steps", Static).renderable
        assert "Refactor the parser" in str(text)
        assert "Extract tokenizer" in str(body)
        assert "1." in str(body)  # steps are numbered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_plan_card.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.plan_card'`

- [ ] **Step 3: Implement `plan_card.py`**

```python
# src/marim_harness/interfaces/tui/plan_card.py
"""The inline Plan card behind ``present_plan``: shows the plan's summary and
numbered steps, then the execution choices, and resolves with the chosen
label. Mounted above the status bar like the ask/approval panels (not a modal),
so the transcript stays scrollable while the user decides. The full plan also
persists in the PlanScreen overlay (Ctrl+P); this card is the deliberate
'here's my plan — how should I run it?' moment in the transcript."""

from textual.app import ComposeResult
from textual.content import Content
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ...ask_user import Choice
from .interaction_panel import InteractionPanel

# Returned on Escape / dismissal — must match the "keep planning" label in
# tools/planning_tools._PLAN_CHOICES so present_plan maps it to "stay in plan mode".
_DISMISS_LABEL = "Keep planning"


def _steps_markup(steps: list[str]) -> str:
    return "\n".join(f"[$accent]{i}.[/] {s}" for i, s in enumerate(steps, 1))


class PlanCard(InteractionPanel):
    """Resolves with the chosen execution-choice label (str), or the
    "Keep planning" label if dismissed with Escape."""

    DEFAULT_CSS = """
    #plan-title { text-style: bold; color: $accent; margin-bottom: 1; }
    #plan-summary { margin-bottom: 1; }
    #plan-steps { margin-bottom: 1; }
    #plan-choices { height: auto; max-height: 8; }
    """

    BINDINGS = [("escape", "dismiss_card", "Keep planning")]

    def __init__(self, summary: str, steps: list[str], choices: list[Choice]) -> None:
        super().__init__()
        self._summary = summary
        self._steps = steps
        self._choices = choices

    def compose(self) -> ComposeResult:
        yield Static("Plan", id="plan-title")
        yield Static(self._summary, id="plan-summary")
        yield Static(Content.from_markup(_steps_markup(self._steps)), id="plan-steps")
        options = OptionList(id="plan-choices")
        yield options

    def on_mount(self) -> None:
        options = self.query_one("#plan-choices", OptionList)
        for i, choice in enumerate(self._choices):
            prompt = Content.from_markup(
                f"{choice.label}" + (f"\n  [dim]{choice.description}[/]" if choice.description else "")
            )
            options.add_option(Option(prompt, id=str(i)))
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # A stale event can land after the future already resolved (mirrors the
        # guard in AskUserPanel) — the panel is going away, so ignore it.
        if self.result.done() or event.option.id is None:
            return
        self.resolve(self._choices[int(event.option.id)].label)

    def action_dismiss_card(self) -> None:
        self.resolve(_DISMISS_LABEL)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_plan_card.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/plan_card.py tests/test_plan_card.py
git commit -m "feat(tui): PlanCard inline panel for present_plan handoff"
```

---

### Task 4: Wire `on_present_plan` through `bind_ui` and the app

**Files:**
- Modify: `src/marim_harness/runtime/harness.py:361-395` (`bind_ui` signature + assignment)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`bind_ui` call ~107-121; add `_present_plan` method near `_ask_user` ~830)
- Test: `tests/test_app_present_plan.py`

**Interfaces:**
- Consumes: `PlanCard` (Task 3), `run_panel`, `CurrentPlan`/`on_present_plan` (Task 1), `present_plan` behavior (Task 2).
- Produces: `Harness.bind_ui(..., on_present_plan=...)` assigns `self.deps.ui.on_present_plan`. The app's `_present_plan(summary, steps, choices) -> str` mounts a `PlanCard` and returns the chosen label. Wired in the app's `bind_ui` call.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app_present_plan.py
"""The app wires on_present_plan: a present_plan handoff mounts a PlanCard whose
choice flips the session mode end to end."""

import pytest
from conftest import _make_harness, _make_deps
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.plan_card import PlanCard
from marim_harness.runtime.permissions import Mode

pytestmark = pytest.mark.anyio


def _plan_then_done_model() -> FunctionModel:
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="present_plan",
                args={"summary": "Refactor the parser.",
                      "steps": ["Extract tokenizer", "Add tests"]},
                tool_call_id="call_plan")])
        return ModelResponse(parts=[TextPart(content="executing now")])

    return FunctionModel(fn)


async def test_present_plan_mounts_card_and_flips_mode(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    deps = _make_deps(tmp_path, mode=Mode.plan)
    harness = _make_harness(_plan_then_done_model(), deps)
    app = HarnessApp(harness)
    async with app.run_test() as pilot:
        app.run_worker(app._run_turn("plan the refactor"))
        # Wait for the PlanCard to appear.
        for _ in range(50):
            await pilot.pause()
            if app.query(PlanCard):
                break
        assert app.query(PlanCard), "PlanCard never mounted"
        await pilot.press("enter")  # highlighted = "Execute hands-off (auto)"
        for _ in range(50):
            await pilot.pause()
            if deps.workspace.mode is Mode.auto:
                break
    assert deps.workspace.mode is Mode.auto
    assert deps.plan is not None and deps.plan.summary == "Refactor the parser."
```

(If `_make_harness`/`HarnessApp` construction differs from this in the current suite — check `tests/test_app.py` for the exact constructor and adapt the two setup lines; the assertion body is what matters.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_app_present_plan.py -v`
Expected: FAIL — no `PlanCard` mounts (the app doesn't wire `on_present_plan`, so `present_plan` falls back to the generic `ask_user` panel).

- [ ] **Step 3: Implement**

In `runtime/harness.py`, add the parameter to `bind_ui` (after `on_mode_change`):

```python
        on_present_plan: OnPresentPlanFn | None = None,
```

Add the import at the top of `harness.py` where the other deps callback types are imported (find the existing `from .deps import ...` or `from ..runtime.deps import ...` line that brings in `ApprovalFn`/`AskUserFn` and add `OnPresentPlanFn`). Then in the assignment block add:

```python
        self.deps.ui.on_present_plan = on_present_plan
```

In `interfaces/tui/app.py`, add `_present_plan` next to `_ask_user`:

```python
    async def _present_plan(self, summary, steps, choices):
        """Put the finished plan to the user as an inline card and return their
        chosen execution label. Inline panel, not a modal — the transcript stays
        scrollable; a cancelled turn removes the card via run_panel's finally.
        The plan's summary/steps already live on deps.plan (set by present_plan),
        so the pinned title and Ctrl+P overlay stay in sync regardless of the
        choice made here."""
        from .plan_card import PlanCard

        self._notify("Plan ready", summary, "ask_user")
        self._render_tasks()  # refresh the TaskPanel title now that deps.plan is set
        return await run_panel(self, PlanCard(summary, steps, choices))
```

Wire it into the app's `bind_ui(...)` call (add after `ask_user=self._ask_user,`):

```python
            on_present_plan=self._present_plan,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_app_present_plan.py tests/test_plan_mode_e2e.py -v`
Expected: PASS

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/harness.py src/marim_harness/interfaces/tui/app.py tests/test_app_present_plan.py
git commit -m "feat(tui): wire on_present_plan to mount the PlanCard"
```

---

### Task 5: TaskPanel plan title line

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/panels.py` (`TaskPanel`)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`_render_tasks` passes the plan title)
- Test: `tests/test_task_panel_plan_title.py`

**Interfaces:**
- Consumes: `deps.plan` (Task 1).
- Produces: `TaskPanel.show_tasks(items, plan_title: str | None = None)` — when `plan_title` is set, a `▸ Plan: <title> · ^P for full plan` line renders above the checklist; when `None`, the panel renders exactly as before. The app's `_render_tasks` derives the title from `deps.plan`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_task_panel_plan_title.py
"""TaskPanel shows a compact plan title above the checklist when a plan exists."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.panels import TaskPanel
from marim_harness.tasks import Task

pytestmark = pytest.mark.anyio


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield TaskPanel()


async def test_plan_title_renders_above_checklist():
    app = _Harness()
    async with app.run_test() as pilot:
        panel = app.query_one(TaskPanel)
        panel.show_tasks([Task(text="Extract tokenizer")], plan_title="Refactor the parser")
        await pilot.pause()
        body = str(app.query_one("#task-body", Static).renderable)
        assert "Plan: Refactor the parser" in body
        assert "^P for full plan" in body
        assert "Extract tokenizer" in body


async def test_no_title_when_plan_absent():
    app = _Harness()
    async with app.run_test() as pilot:
        panel = app.query_one(TaskPanel)
        panel.show_tasks([Task(text="Extract tokenizer")])  # no plan_title
        await pilot.pause()
        body = str(app.query_one("#task-body", Static).renderable)
        assert "Plan:" not in body
        assert "Extract tokenizer" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_task_panel_plan_title.py -v`
Expected: FAIL — `TypeError: show_tasks() got an unexpected keyword argument 'plan_title'`

- [ ] **Step 3: Implement**

In `panels.py`, replace `TaskPanel` with:

```python
class TaskPanel(LivePanel):
    """The agent's live checklist, optionally prefixed with a compact one-line
    plan title (``▸ Plan: <summary> · ^P for full plan``) when plan mode has
    produced a plan this session — so the plan's 'why' stays reachable while the
    bare steps scroll by. The full plan lives in the Ctrl+P PlanScreen overlay."""

    def __init__(self) -> None:
        from ....tasks import render_tasks

        # markup stays False on the base panel: this class renders its own body
        # (below) so it can prepend a markup plan-title line while keeping the
        # checklist text markup-escaped.
        super().__init__(name="task", title="Tasks", renderer=render_tasks)
        self._plan_title: str | None = None

    def show_tasks(self, items: list, plan_title: str | None = None) -> None:
        self._plan_title = plan_title
        self._render_items(items)

    def _render_items(self, items: list) -> None:
        # Fully overrides LivePanel._render_items (does not call super): the body
        # is built as markup so the plan-title line can be styled, but the
        # checklist text (which may contain '[' in a task, e.g. "fix [bug]") is
        # escaped so it can never be parsed as markup. Empty/hide handling
        # mirrors the base class.
        from textual.content import Content
        from textual.markup import escape

        if not items:
            self.display = False
            self._header.update("")
            self._body.update("")
            return
        self.display = True
        self._count = len(items)
        self._update_header()
        steps = escape(self._renderer(items))
        body = f"{_plan_title_line(self._plan_title)}\n{steps}" if self._plan_title else steps
        self._body.update(Content.from_markup(body))
```

Add this helper near the top of `panels.py` (after the imports):

```python
def _plan_title_line(summary: str, width: int = 48) -> str:
    """One-line plan title for the TaskPanel: first line of the summary,
    truncated, with the Ctrl+P hint. Markup-escaped so a summary containing
    brackets can't break Content.from_markup."""
    from textual.markup import escape

    first = summary.splitlines()[0].strip() if summary else ""
    if len(first) > width:
        first = first[: width - 1] + "…"
    return f"[$accent]▸ Plan:[/] {escape(first)} [dim]· ^P for full plan[/]"
```

In `app.py`, change `_render_tasks` to pass the title from `deps.plan`:

```python
    def _render_tasks(self) -> None:
        """Repaint the task panel from the harness's current checklist, plus a
        compact plan title when a plan has been presented this session."""
        try:
            panel = self.query_one(TaskPanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        plan = self.harness.deps.plan
        panel.show_tasks(
            self.harness.deps.tasks.items,
            plan_title=plan.summary if plan is not None else None,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_task_panel_plan_title.py tests/test_app.py -v`
Expected: PASS (existing app tests that assert on the task panel must stay green)

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/widgets/panels.py src/marim_harness/interfaces/tui/app.py tests/test_task_panel_plan_title.py
git commit -m "feat(tui): compact plan title on the TaskPanel"
```

---

### Task 6: `PlanScreen` overlay + Ctrl+P

**Files:**
- Create: `src/marim_harness/interfaces/tui/plan_screen.py`
- Modify: `src/marim_harness/interfaces/tui/app.py` (BINDINGS + `action_show_plan`)
- Test: `tests/test_plan_screen.py`

**Interfaces:**
- Consumes: `CurrentPlan` (`deps.plan`) and `deps.tasks.items` (Task 1); `render_tasks` (`tasks.py`).
- Produces: `PlanScreen(summary: str, path: str | None, tasks: list[Task])` — a `Screen[None]` showing the summary, the checklist (with live done/in-progress/pending markers), and the path. Pushed by `HarnessApp.action_show_plan` (bound to Ctrl+P); Esc dismisses. With no plan, Ctrl+P flashes a hint and pushes nothing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan_screen.py
"""PlanScreen overlay: shows the plan summary, path, and the checklist with live
progress markers; Ctrl+P is a no-op hint when no plan exists."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.plan_screen import PlanScreen
from marim_harness.tasks import Task

pytestmark = pytest.mark.anyio


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield Static("base")


async def test_plan_screen_shows_summary_path_and_progress():
    tasks = [Task(text="Extract tokenizer", status="done"),
             Task(text="Add tests", status="in_progress")]
    app = _Harness()
    async with app.run_test() as pilot:
        app.push_screen(PlanScreen("Refactor the parser", "/tmp/plan.md", tasks))
        await pilot.pause()
        text = " ".join(str(w.renderable) for w in app.screen.query(Static))
        assert "Refactor the parser" in text
        assert "/tmp/plan.md" in text
        assert "Extract tokenizer" in text
        assert "✔" in text   # done marker
        assert "▸" in text   # in-progress marker


async def test_escape_dismisses():
    app = _Harness()
    async with app.run_test() as pilot:
        app.push_screen(PlanScreen("s", None, [Task(text="x")]))
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, PlanScreen)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_plan_screen.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.plan_screen'`

- [ ] **Step 3: Implement `plan_screen.py`**

```python
# src/marim_harness/interfaces/tui/plan_screen.py
"""The full-height plan overlay (Ctrl+P): a read-only view of the current plan's
summary, its steps with live progress markers, and the plan-file path. Pushed as
a Screen (like Settings and the sub-agents view), NOT a ModalScreen — you summon
it precisely to *not* need the transcript behind it, so the inline-over-modal
rule doesn't apply. Step progress is read from the live task list, so the view
reflects execution in real time; the summary/path come from deps.plan."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Static

from ...tasks import Task, render_tasks


class PlanScreen(Screen[None]):
    """Read-only plan overlay. Esc closes it."""

    CSS = """
    PlanScreen { background: $surface; }
    #plan-screen-header { height: 1; padding: 0 1; background: $panel; color: $accent;
        text-style: bold; }
    #plan-screen-body { height: 1fr; padding: 1 2; }
    #plan-screen-summary { margin-bottom: 1; }
    #plan-screen-steps { margin-bottom: 1; }
    #plan-screen-path { color: $text-muted; }
    #plan-screen-footer { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    """

    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, summary: str, path: str | None, tasks: list[Task]) -> None:
        super().__init__()
        self._summary = summary
        self._path = path
        self._tasks = tasks

    def compose(self) -> ComposeResult:
        yield Static("Plan", id="plan-screen-header")
        with VerticalScroll(id="plan-screen-body"):
            yield Static(self._summary, id="plan-screen-summary")
            yield Static(Content(render_tasks(self._tasks)), id="plan-screen-steps")
            path_line = f"[dim]file:[/] {escape(self._path)}" if self._path else "[dim]not saved to disk[/]"
            yield Static(Content.from_markup(path_line), id="plan-screen-path")
        yield Static("esc close", id="plan-screen-footer")

    def action_close(self) -> None:
        self.dismiss()
```

In `app.py`, add the binding to `BINDINGS` (after the `ctrl+x` line):

```python
        ("ctrl+p", "show_plan", "Plan"),
```

Add the action method (near `action_toggle_subagents`):

```python
    def action_show_plan(self) -> None:
        """Open the full plan overlay, or flash a hint when no plan exists yet."""
        from .plan_screen import PlanScreen

        plan = self.harness.deps.plan
        if plan is None:
            self.notify("No plan yet — the agent presents one in plan mode.",
                        severity="information")
            return
        self.push_screen(
            PlanScreen(plan.summary, plan.path, self.harness.deps.tasks.items)
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_plan_screen.py -v`
Expected: PASS

- [ ] **Step 5: Run the full CI gauntlet in CI order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all green, coverage ≥90%. Fix anything that surfaces before committing.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/plan_screen.py src/marim_harness/interfaces/tui/app.py tests/test_plan_screen.py
git commit -m "feat(tui): PlanScreen overlay on Ctrl+P"
```

---

## Deferred (explicitly NOT in this plan, per spec)

A right-side panel; editing the plan from the overlay; persisting overlay open/closed state across sessions; any change to plan-mode permission semantics (`_plan_decision`) or the plan file format.
