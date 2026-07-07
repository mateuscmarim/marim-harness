# PlanCard Free-Text Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user reject a presented plan and say what to change in one step, via a free-text feedback field on the `PlanCard`, so the agent revises and re-presents in the same turn.

**Architecture:** `on_present_plan`'s return widens from a bare `str` (choice label) to a small `PlanDecision(choice, feedback)` value object. The `PlanCard` gains an always-visible `Input`; typing feedback resolves `PlanDecision(choice="Keep planning", feedback=<text>)`. `present_plan` threads that feedback into its tool-return string, and since `present_plan` is a normal tool call the model revises in-turn and re-presents automatically.

**Tech Stack:** Python ≥3.10, Textual (optional `tui` extra), pytest + `pytest.mark.anyio` + Textual `run_test()` pilot.

**Spec:** `docs/superpowers/specs/2026-07-07-plancard-feedback-design.md`

## Global Constraints

- `requires-python = ">=3.10"` — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (imports sorted). Use `uv` for everything (`uv run pytest`, `uv run ruff check src tests`, `uv run pyright`). Never bare `python`/`pip`.
- Verify order before claiming done (matches CI): ruff → pyright → pytest. pyright config scans `src/` only (not `tests/`), but run it anyway.
- No live/paid models in any test — `FunctionModel`/direct calls only.
- The four plan-execution choice labels are the single source of truth in `tools/planning_tools._PLAN_CHOICES`; the dismiss/keep-planning label is `"Keep planning"`. Feedback always pairs with the `"Keep planning"` choice (reject-and-revise only).
- Preserve existing long "why" comments when editing near them.
- `present_plan` must stay correct headless (both callbacks `None` → save + stay in plan mode, unchanged).
- Model/user text rendered in the card must never be Textual-markup-parsed — but the feedback text is an `Input` value echoed back to the model, never rendered as markup, so this does not apply to the feedback path.

## File Structure

Modify:
- `src/marim_harness/runtime/deps.py` — add `PlanDecision`; widen `OnPresentPlanFn`.
- `src/marim_harness/interfaces/tui/plan_card.py` — add feedback `Input`; resolve `PlanDecision`.
- `src/marim_harness/tools/planning_tools.py` — consume `PlanDecision`; thread feedback into the keep-planning return (Task 1); normalize the `ask_user` fallback (Task 2).
- Tests: `tests/test_plan_card.py`, `tests/test_present_plan_tool.py` (both tasks), `tests/test_app_present_plan.py` (regression, no change expected).

---

### Task 1: `PlanDecision` + PlanCard feedback field + present_plan feedback threading

This is one atomic task: the `PlanDecision` interface change must be produced (PlanCard) and consumed (present_plan) together, or the real TUI flow breaks between tasks.

**Files:**
- Modify: `src/marim_harness/runtime/deps.py` (add `PlanDecision` near `CurrentPlan` ~line 135; widen `OnPresentPlanFn` line 78)
- Modify: `src/marim_harness/interfaces/tui/plan_card.py` (feedback `Input`, resolve `PlanDecision`)
- Modify: `src/marim_harness/tools/planning_tools.py` (consume `PlanDecision`, thread feedback)
- Test: `tests/test_plan_card.py`, `tests/test_present_plan_tool.py`

**Interfaces:**
- Produces: `PlanDecision` (frozen dataclass: `choice: str`, `feedback: str | None = None`) in `runtime/deps.py`. `OnPresentPlanFn = Callable[[str, list[str], list[Choice]], Awaitable["PlanDecision"]]`. `PlanCard.result` now resolves a `PlanDecision`. `present_plan` returns a revise-instruction string containing the feedback when `PlanDecision.feedback` is set.

- [ ] **Step 1: Write the failing PlanCard tests**

Replace the three existing resolution assertions in `tests/test_plan_card.py` (which currently expect bare strings) and add the feedback tests. First, add the import at the top of the file (next to the existing `from marim_harness.interfaces.tui.plan_card import PlanCard`):

```python
from marim_harness.runtime.deps import PlanDecision
from textual.widgets import Input
```

Change the existing `test_selects_highlighted_choice`, `test_selects_second_choice`, and `test_escape_keeps_planning` assertions from bare-string to `PlanDecision`:

```python
async def test_selects_highlighted_choice():
    app = _Harness("Refactor parser", ["Extract tokenizer", "Add tests"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # first option highlighted
        await pilot.pause()
    assert app.result == PlanDecision(choice="Execute hands-off (auto)", feedback=None)


async def test_selects_second_choice():
    app = _Harness("Refactor parser", ["Extract tokenizer"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == PlanDecision(choice="Execute step-by-step (ask)", feedback=None)


async def test_escape_keeps_planning():
    app = _Harness("Refactor parser", ["Extract tokenizer"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result == PlanDecision(choice="Keep planning", feedback=None)
```

Add two new tests at the end of the file:

```python
async def test_feedback_input_resolves_keep_planning_with_text():
    app = _Harness("Refactor parser", ["Extract tokenizer"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        card = app.query_one(PlanCard)
        inp = card.query_one("#plan-feedback", Input)
        inp.focus()
        await pilot.pause()
        inp.value = "please add error handling first"
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == PlanDecision(
        choice="Keep planning", feedback="please add error handling first"
    )


async def test_empty_feedback_submit_is_ignored():
    app = _Harness("Refactor parser", ["Extract tokenizer"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        card = app.query_one(PlanCard)
        inp = card.query_one("#plan-feedback", Input)
        inp.focus()
        await pilot.pause()
        inp.value = ""
        await pilot.press("enter")
        await pilot.pause()
        # Empty submit did not resolve — the card is still mounted and awaiting.
        assert app.query(PlanCard)
        assert app.result == "unset"
        # A real choice still works afterward.
        options = card.query_one("#plan-choices")
        options.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == PlanDecision(choice="Execute hands-off (auto)", feedback=None)
```

- [ ] **Step 2: Run PlanCard tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_plan_card.py -v`
Expected: FAIL — `ImportError: cannot import name 'PlanDecision'` (and the assertions expect a value object the card doesn't produce yet).

- [ ] **Step 3: Add `PlanDecision` + widen `OnPresentPlanFn` in `deps.py`**

Change the `OnPresentPlanFn` alias (currently line 78) to return a forward-referenced `PlanDecision`:

```python
# (summary, steps, choices) -> the user's decision (chosen label + optional
# revise-feedback). Wired by the TUI (mounts a PlanCard inline panel); None when
# headless, where present_plan falls back to ask_user then to "save and stay in
# plan mode". The choices are passed through so the card never hardcodes the
# plan-execution labels (their single source of truth is
# tools/planning_tools._PLAN_CHOICES).
OnPresentPlanFn = Callable[[str, list[str], list[Choice]], Awaitable["PlanDecision"]]
```

Add the `PlanDecision` dataclass just above `CurrentPlan` (around line 134, before `@dataclass(frozen=True) class CurrentPlan`):

```python
@dataclass(frozen=True)
class PlanDecision:
    """The outcome of a present_plan handoff. ``choice`` is one of the
    _PLAN_CHOICES labels (or the "Keep planning" dismiss label). ``feedback`` is
    the user's revise-notes when they typed feedback instead of picking a choice
    — always paired with the "Keep planning" choice (reject-and-revise)."""

    choice: str
    feedback: str | None = None
```

- [ ] **Step 4: Add the feedback `Input` to `PlanCard` and resolve `PlanDecision`**

In `src/marim_harness/interfaces/tui/plan_card.py`:

Update imports:
```python
from textual.widgets import Input, OptionList, Static
```
and add:
```python
from ...runtime.deps import PlanDecision
```

Add an input CSS rule to `DEFAULT_CSS` (inside the existing string, after the `#plan-choices` rule):
```css
    #plan-feedback { margin-top: 1; }
```

Add the input to `compose` (after the `options` yield):
```python
    def compose(self) -> ComposeResult:
        yield Static("Plan", id="plan-title")
        yield Static(self._summary, id="plan-summary", markup=False)
        yield Static(_steps_content(self._steps), id="plan-steps")
        options = OptionList(id="plan-choices")
        yield options
        yield Input(placeholder="or type feedback to revise the plan…", id="plan-feedback")
```

Change `on_option_list_option_selected` to resolve a `PlanDecision`:
```python
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # A stale event can land after the future already resolved (mirrors the
        # guard in AskUserPanel) — the panel is going away, so ignore it.
        if self.result.done() or event.option.id is None:
            return
        self.resolve(PlanDecision(choice=self._choices[int(event.option.id)].label))
```

Add an input-submitted handler (mirroring `AskUserPanel.on_input_submitted`):
```python
    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Free-text feedback = reject-and-revise: resolve "Keep planning" carrying
        # the notes. Empty submit is ignored so the user can still pick a choice.
        # Guard against a stale event after the future already resolved.
        if self.result.done():
            return
        text = event.value.strip()
        if text:
            self.resolve(PlanDecision(choice=_DISMISS_LABEL, feedback=text))
```

Change `action_dismiss_card` to resolve a `PlanDecision`:
```python
    def action_dismiss_card(self) -> None:
        self.resolve(PlanDecision(choice=_DISMISS_LABEL))
```

Update the class docstring line to reflect the new return:
```python
    """Resolves with a PlanDecision: the chosen execution-choice label, or
    ("Keep planning", feedback) when the user types revise-feedback, or
    ("Keep planning", None) when dismissed with Escape."""
```

- [ ] **Step 5: Run PlanCard tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_plan_card.py -v`
Expected: PASS (all, including the two new feedback tests)

- [ ] **Step 6: Write the failing present_plan tests**

In `tests/test_present_plan_tool.py`, add the import:
```python
from marim_harness.runtime.deps import PlanDecision
```

Change `test_on_present_plan_preferred_and_sets_plan`'s fake to return a `PlanDecision`:
```python
    async def fake_present(summary, steps, choices):
        seen["summary"] = summary
        seen["steps"] = steps
        seen["choices"] = [c.label for c in choices]
        return PlanDecision(choice="Execute hands-off (auto)")
```

Change `test_dismissed_card_keeps_planning`'s fake:
```python
    async def fake_present(summary, steps, choices):
        return PlanDecision(choice="Keep planning")
```

Add a new test:
```python
async def test_feedback_keeps_planning_and_returns_feedback(tmp_path):
    async def fake_present(summary, steps, choices):
        return PlanDecision(choice="Keep planning", feedback="use a dataclass not a dict")

    deps = _make_deps(tmp_path, mode=Mode.plan, on_present_plan=fake_present)
    ctx = SimpleNamespace(deps=deps)

    result = await present_plan(ctx, "s", ["one"])
    assert deps.workspace.mode is Mode.plan            # not approved → no flip
    assert "use a dataclass not a dict" in result      # feedback threaded to the model
    assert "revise" in result.lower()                  # instructed to revise
```

- [ ] **Step 7: Run present_plan tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_present_plan_tool.py -v`
Expected: FAIL — the `on_present_plan` fakes now return a `PlanDecision` but `present_plan` still treats the result as a bare string (`AttributeError` / feedback not in the return).

- [ ] **Step 8: Consume `PlanDecision` and thread feedback in `present_plan`**

In `src/marim_harness/tools/planning_tools.py`, change the handoff block. The `on_present_plan` branch unpacks the decision; the `ask_user` branch sets `feedback = None` (Task 2 normalizes it):

```python
    if ctx.deps.ui.on_present_plan is not None:
        decision = await ctx.deps.ui.on_present_plan(summary, clean, _PLAN_CHOICES)
        choice, feedback = decision.choice, decision.feedback
    elif ctx.deps.ui.ask_user is not None:
        answers = await ctx.deps.ui.ask_user(
            [Question(question="How should I execute this plan?", header="execution",
                      options=_PLAN_CHOICES)]
        )
        choice = (answers or {}).get("execution", "Keep planning")
        feedback = None
    else:
        return (
            f"Plan saved{f' to {path}' if path else ''}. No interactive UI, so "
            "staying in plan mode — share the plan and await direction."
        )
```

Add the import at the top (extend the existing `from ..runtime.deps import CurrentPlan, Deps`):
```python
from ..runtime.deps import CurrentPlan, Deps
```
(`PlanDecision` itself isn't referenced by name in this file — `decision` is duck-typed from the callback — so no new import is needed here. Leave the existing import line as is.)

Then, immediately before the final "draft" return (the `return (f"Plan saved{...} as a draft. ...")` block), add the feedback-aware branch:

```python
    if feedback:
        return (
            "Plan not approved. The user wants you to revise it. Their feedback: "
            f"{feedback}\n\nRevise the plan accordingly and call present_plan again "
            "when ready."
        )
    return (
        f"Plan saved{f' to {path}' if path else ''} as a draft. Still in plan mode "
        "— refine it and call present_plan again when ready."
    )
```

(The `new_mode = _PLAN_EXEC_MODES.get(choice if isinstance(choice, str) else "")` line, the mode-flip block, and the "Hand off to sub-agent" block are all unchanged — feedback only accompanies "Keep planning", so those branches never see it.)

- [ ] **Step 9: Run present_plan tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_present_plan_tool.py -v`
Expected: PASS (all, including the new feedback test and the unchanged headless/ask_user-fallback tests)

- [ ] **Step 10: Run the app integration test as a regression check**

The end-to-end test drives the real `PlanCard` through `present_plan`; the mode-flip on an execute choice must still work with the new `PlanDecision` flow (no code change expected in that test).

Run: `uv run pytest --no-cov tests/test_app_present_plan.py -v`
Expected: PASS (unchanged). If it fails, the `PlanCard`→`present_plan` `PlanDecision` handoff is wired wrong — fix before committing.

- [ ] **Step 11: Lint, typecheck, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/deps.py src/marim_harness/interfaces/tui/plan_card.py src/marim_harness/tools/planning_tools.py tests/test_plan_card.py tests/test_present_plan_tool.py
git commit -m "feat(tui): PlanCard free-text feedback to revise a plan in one step"
```

---

### Task 2: Normalize the `ask_user` fallback to treat free text as feedback

The `ask_user` fallback (reached only when `on_present_plan` is unwired — never in the real TUI, but kept for robustness) already renders a free-text field but currently discards anything typed there. Make it behave like the `PlanCard`: an answer that isn't a known choice label becomes feedback.

**Files:**
- Modify: `src/marim_harness/tools/planning_tools.py` (the `ask_user` branch)
- Test: `tests/test_present_plan_tool.py`

**Interfaces:**
- Consumes: the feedback-aware return branch from Task 1.
- Produces: no new symbols — `present_plan`'s `ask_user` branch now yields `feedback` when the answer is free text.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_present_plan_tool.py`:

```python
async def test_ask_user_freetext_answer_becomes_feedback(tmp_path):
    async def fake_ask(questions):
        # A typed free-text answer (not one of the four known choice labels).
        return {questions[0].header: "please use pytest fixtures instead"}

    deps = _make_deps(tmp_path, mode=Mode.plan, ask_user=fake_ask)  # on_present_plan unset
    ctx = SimpleNamespace(deps=deps)

    result = await present_plan(ctx, "s", ["one"])
    assert deps.workspace.mode is Mode.plan                     # free text → no execute
    assert "please use pytest fixtures instead" in result       # threaded as feedback
    assert "revise" in result.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_present_plan_tool.py::test_ask_user_freetext_answer_becomes_feedback -v`
Expected: FAIL — the free text currently falls through to the plain "draft" return (does not appear in the result / no "revise").

- [ ] **Step 3: Normalize the `ask_user` branch**

In `src/marim_harness/tools/planning_tools.py`, replace the `ask_user` branch's `choice = ...; feedback = None` with free-text detection:

```python
    elif ctx.deps.ui.ask_user is not None:
        answers = await ctx.deps.ui.ask_user(
            [Question(question="How should I execute this plan?", header="execution",
                      options=_PLAN_CHOICES)]
        )
        choice = (answers or {}).get("execution", "Keep planning")
        # A free-text answer (not one of the known choice labels) is revise-feedback,
        # mirroring the PlanCard's feedback field.
        known = {c.label for c in _PLAN_CHOICES}
        if isinstance(choice, str) and choice not in known:
            feedback = choice
            choice = "Keep planning"
        else:
            feedback = None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest --no-cov tests/test_present_plan_tool.py -v`
Expected: PASS (all, including the pre-existing `ask_user`-fallback test where a *known* label still flips the mode)

- [ ] **Step 5: Run the full CI gauntlet in CI order**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all green, coverage ≥90%. Fix anything that surfaces before committing.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tools/planning_tools.py tests/test_present_plan_tool.py
git commit -m "feat(plan): treat ask_user free-text plan answer as revise-feedback"
```

---

## Deferred (explicitly NOT in this plan, per spec)

Attaching feedback to an execute choice; any change to the four execution choices, the mode-flip semantics, the PlanScreen overlay, or the TaskPanel title; multi-round feedback history; persisting feedback to the plan file.
