# PlanCard Free-Text Feedback — Design

**Date:** 2026-07-07
**Status:** Approved (brainstorm), pending implementation plan

## Goal

Let the user reject a presented plan **and say what to change in one step**,
directly from the `PlanCard` (the `present_plan` handoff panel), instead of
picking "Keep planning" and then typing a separate follow-up message. The
feedback reaches the agent, which revises the plan and presents a fresh card —
all within the same turn.

## Motivation

Today `PlanCard` offers only the four fixed execution choices (and Esc). "Keep
planning" is a bare *not-yet*: it ends the turn in plan mode but carries no
signal about **why** or **what to change**, so the model has nothing to act on
and the user must type a follow-up message. Unlike the generic `ask_user`
panel — which always shows a "type your own answer" free-text field — the
`PlanCard` has no free-text input at all. This closes that gap.

## Key architectural property

`present_plan` is a normal tool call: when it returns a string to the model,
the model **continues the same turn**. So threading feedback into
`present_plan`'s return value makes the agent revise the plan and (typically)
call `present_plan` again in-turn, popping a fresh `PlanCard` automatically.
The "one step" outcome falls out of the existing loop — no new turn, no
user follow-up message.

## Scope decision

Typing feedback means **reject-and-revise only**: it always implies "don't
execute yet — revise with these notes" and keeps the session in plan mode.
Feedback is never attached to an execute choice (rejected as scope creep — it
would muddy the field's meaning and require a richer return shape).

## The interface change

`OnPresentPlanFn` currently returns a bare `str` (the chosen choice label). It
widens to return a small value object so it can carry feedback alongside the
choice.

New frozen dataclass in `runtime/deps.py`, next to `CurrentPlan`:

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

`OnPresentPlanFn` becomes
`Callable[[str, list[str], list[Choice]], Awaitable[PlanDecision]]`.

## Components

### PlanCard (`interfaces/tui/plan_card.py`)

Gains an always-visible `Input` mounted below the choices, with
`placeholder="or type feedback to revise the plan…"`, mirroring how
`AskUserPanel` offers a free-text field alongside its options. The choice
`OptionList` keeps focus by default (arrow + Enter to pick a choice); the user
Tabs or clicks into the input to type feedback.

Resolution (`self.result` now carries a `PlanDecision`):
- Pick a choice → `PlanDecision(choice=<label>, feedback=None)`
- Type non-empty feedback + Enter → `PlanDecision(choice="Keep planning", feedback=<text>)`
- Esc (dismiss) → `PlanDecision(choice="Keep planning", feedback=None)` (unchanged behavior)
- Empty feedback submit (Enter on empty input) → ignored (no resolve), matching
  `AskUserPanel`'s `if text:` guard.

The "Keep planning" label remains the single source of truth in
`tools/planning_tools._PLAN_CHOICES`; the card continues to reference it via the
existing `_DISMISS_LABEL` constant for both the Esc path and the feedback path.

### present_plan (`tools/planning_tools.py`)

Consumes the `PlanDecision`. The mode-flip branch (`_PLAN_EXEC_MODES`) and the
"Hand off to sub-agent" branch are unchanged — feedback only ever accompanies
"Keep planning", so those execute/handoff paths never see feedback. The
keep-planning return branch becomes feedback-aware:

- **with feedback** → return a string that names the rejection and includes the
  feedback, instructing the model to revise and re-present, e.g.:
  `"Plan not approved. The user wants you to revise it. Their feedback: <text>. Revise the plan accordingly and call present_plan again when ready."`
- **without feedback** → the existing "saved as a draft — refine and
  re-present when ready" message.

### ask_user fallback (same file)

The `ask_user` fallback path (reached only when `on_present_plan` is unwired —
i.e. never in the real TUI, but kept for robustness) already renders a
free-text field, but currently discards anything typed there. Normalize it to
the same behavior: an answer that is **not** one of the known `_PLAN_CHOICES`
labels is treated as feedback → `("Keep planning", <free text>)`. This makes
both handoff paths behave identically at no extra cost.

Both paths converge on a single `(choice, feedback)` pair that feeds the
existing downstream logic.

### app (`interfaces/tui/app.py`)

`_present_plan` returns whatever `run_panel(self, PlanCard(...))` resolves — now
a `PlanDecision`. No logic change; the method stays a thin UI adapter.

## Data flow

```
type feedback + Enter
  → PlanCard resolves PlanDecision(choice="Keep planning", feedback=<text>)
  → run_panel returns it
  → app._present_plan passes it through
  → present_plan returns a revise-instruction string containing <text>
  → model revises in the same turn
  → model calls present_plan again
  → fresh PlanCard appears
```

No new turn; no user follow-up message.

## Error handling / edge cases

- Empty feedback submit → ignored (no resolve), user can still pick a choice or Esc.
- The feedback text is the user's own input echoed back to the **model** as a
  plain string; it is never rendered as Textual markup in the card (it lives in
  an `Input` value), so the markup-safety concerns from the prior feature do not
  recur here.
- Stale-event guard on the input mirrors the existing option-selected guard
  (`if self.result.done(): return`).

## Testing

- **PlanCard** (`tests/test_plan_card.py`): feedback + Enter →
  `PlanDecision(choice="Keep planning", feedback=<text>)`; pick a choice →
  `PlanDecision(choice=<label>, feedback=None)`; Esc →
  `PlanDecision(choice="Keep planning", feedback=None)`; empty submit is ignored
  (card stays up). Update the existing choice/Esc tests for the new
  `PlanDecision` return type.
- **present_plan** (`tests/test_present_plan_tool.py`): `PlanDecision` with
  feedback → return string contains the feedback and mode stays plan; with an
  execute choice → mode flips (existing behavior preserved). Update existing
  tests for the widened return type.
- **ask_user fallback**: a free-text answer that isn't a known label → treated
  as feedback (keep planning + feedback in the return).
- **app integration** (`tests/test_app_present_plan.py`): update the existing
  test for the `PlanDecision` return type (the mode-flip assertion is
  unchanged).

## Files touched

- `src/marim_harness/runtime/deps.py` — add `PlanDecision`, widen `OnPresentPlanFn`.
- `src/marim_harness/interfaces/tui/plan_card.py` — add the feedback `Input`,
  resolve with `PlanDecision`, add the input-submitted handler.
- `src/marim_harness/tools/planning_tools.py` — consume `PlanDecision`, thread
  feedback into the keep-planning return; normalize the `ask_user` fallback.
- Tests: `tests/test_plan_card.py`, `tests/test_present_plan_tool.py`,
  `tests/test_app_present_plan.py`.

## Out of scope

- Attaching feedback to an execute choice.
- Any change to the four execution choices, the mode-flip semantics, the
  PlanScreen overlay, or the TaskPanel title.
- Multi-round feedback history / persisting feedback to the plan file.
