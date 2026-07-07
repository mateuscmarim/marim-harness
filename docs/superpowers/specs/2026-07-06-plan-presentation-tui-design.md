# Plan Presentation in the TUI — Design

**Date:** 2026-07-06
**Status:** Approved (brainstorm), pending implementation plan

## Goal

Make the agent's plan (from plan mode's `present_plan` tool) a first-class,
persistent part of the TUI instead of a transient generic tool card. Three
concrete outcomes, weighted equally:

1. **The plan + handoff read as one deliberate moment** — a distinct inline
   "Plan card" with the execution choices built in, not a generic tool card
   followed by a separate `ask_user` box.
2. **The plan's narrative stays reachable during execution** — today the
   summary lives only in the transient tool card and scrolls away; only the
   bare step checklist persists in the `TaskPanel`.
3. **The full plan can be summoned on demand** — a focused, read-only view of
   summary + all steps + live progress, dismissable, without scrolling back
   through the transcript.

## Why not a right-side panel

The TUI is a single-column `VerticalScroll` transcript (`#log`) with a
documented inline-over-modal philosophy (`2026-07-01-inline-interaction-panels`)
and a bottom-docked `TaskPanel` that already mirrors plan steps. A permanent
right panel would (a) fight that single-column architecture, (b) steal
horizontal space from the transcript's wide content (diffs, code, tool output),
and (c) duplicate the persistent step view the `TaskPanel` already provides.
Terminal vertical space is equally scarce, so the persistent piece stays
compact and the full plan lives in an on-demand overlay instead.

## Current behavior (baseline)

`present_plan(summary, steps)` (`tools/planning_tools.py`) today:

- Writes the plan to `.marim/plans/<slug>.md` (`workspace/plans.py`).
- Mirrors steps into the task checklist via `deps.tasks.replace(...)` → shown
  in the pinned `TaskPanel`.
- Calls `deps.ui.ask_user` with "How should I execute this plan?" and four
  choices (Hands-off / Step-by-step / Hand off to sub-agent / Keep planning),
  rendered by the shared inline `ask_user` panel.
- On an execution choice, flips `workspace.mode` and fires `on_mode_change`.
- When `ask_user` is `None` (headless/tests), saves the plan and stays in plan
  mode.

In the TUI, `present_plan` renders as a generic `ToolCallWidget` (only
`update_tasks` is special-cased, as a one-line breadcrumb). The summary is
therefore visible only as that card's transient args.

## Chosen approach

**Approach A — a dedicated `PlanCard` inline panel with the choices built in**,
driven by a new optional UI callback `on_present_plan` on `Deps.ui`, exactly
parallel to `ask_user`. Rejected alternatives:

- **B** — rich-render the `present_plan` tool widget (like the `update_tasks`
  special-case) and keep the separate `ask_user` panel below it, visually
  grouped. Less new code, but still two widgets stitched together cosmetically.
- **C** — fold the plan into the generic `ask_user` panel. One widget, but it
  pollutes a general-purpose component with plan-specific rendering.

Approach A is a first-class inline panel (matching the ask/approval
philosophy), plan-specific, and keeps plan semantics out of the generic
`ask_user` component.

## Components

### 1. `PlanCard` inline widget

New widget under `interfaces/tui/widgets/`. Mounted in the transcript at
`present_plan` time. Renders:

- A distinct header ("Plan").
- The full `summary` paragraph.
- The numbered `steps`.
- The four execution choices as inline selectable options (reusing the inline
  ask/approval interaction pattern — no modal).

It resolves to the chosen option string. After resolution it remains in the
transcript as a static record, collapsing to header + summary (consistent with
how other resolved inline panels persist).

### 2. Persistent compact affordance (extend `TaskPanel`)

The existing `TaskPanel` (`interfaces/tui/widgets/panels.py`) gains a one-line
plan title above its checklist:

```
▸ Plan: <first line of summary, truncated> · ^P for full plan
```

Costs one row; keeps the "why" one keypress away. Seeded whenever
`present_plan` runs. The steps already flow into the panel via
`deps.tasks.replace`; this adds only the title line and the hint. When no plan
exists this session, the title line is absent (the panel renders as today).

### 3. `PlanScreen` overlay

New `Screen[None]` (`interfaces/tui/`), pushed on **Ctrl+P**, dismissed on Esc
— the same push pattern as `SettingsScreen` and the sub-agents screen (NOT a
`ModalScreen`; a summoned full-height screen does not need the transcript
behind it, so the inline-over-modal rule does not apply). Full-height,
read-only, showing:

- The `summary`.
- All steps with live progress markers: done (✓) / current (▸) / pending.
- The plan file path (`.marim/plans/<slug>.md`).

It reads from `deps.tasks` (for step progress) plus the stored plan summary, so
it reflects execution progress in real time. When no plan exists this session,
Ctrl+P is a no-op that flashes a hint ("No plan yet — the agent presents one in
plan mode").

## Data flow

`present_plan` (`tools/planning_tools.py`):

1. Write plan file + `deps.tasks.replace(steps)` — unchanged.
2. Store `summary` on a new field so the pinned title and overlay can read it
   (see Domain state below).
3. If `deps.ui.on_present_plan` is wired: `await` it with `(summary, steps)` and
   use the returned choice for the handoff. Else: fall back to the existing
   `ask_user` path — and if that is also `None`, the current
   "saved, stay in plan mode" behavior is unchanged.
4. Mode flip + `on_mode_change` refresh — unchanged.

`bind_ui` (`runtime/harness.py`) wires `on_present_plan` and the app registers
the Ctrl+P action / `PlanScreen`.

### Domain state

The plan summary must outlive the transient `PlanCard` so the pinned title and
`PlanScreen` can read it. Add a minimal holder reachable from `deps` — a
`current_plan` value object carrying `summary`, `steps`, and the plan file
`path` (or `None` when no plan this session). `present_plan` sets it; the
`TaskPanel` title and `PlanScreen` read it. This keeps plan state in one place
rather than scattering `summary` across widgets. Step *progress* continues to
live in `deps.tasks` (the single source of truth for done/current/pending) — the
holder stores the narrative, not the checklist state, to avoid two sources of
truth for progress.

## Interaction / callback contract

`OnPresentPlanFn = Callable[[str, list[str]], Awaitable[str]]` on `UIHooks`,
`None` when headless. Returns the chosen execution-option label (one of the
four `_PLAN_CHOICES`), or the "keep planning" label if the user dismisses the
card without choosing. `present_plan` maps that label through the existing
`_PLAN_EXEC_MODES` table exactly as it maps the `ask_user` answer today, so the
mode-flip and handoff-return logic is shared, not duplicated.

## Error handling

- Plan-file write failure (`OSError`) — already handled: `path` becomes `None`
  and the flow continues. The `PlanCard` and overlay show the plan without a
  path line.
- `on_present_plan` wired but the card is dismissed — resolves to "Keep
  planning" (stay in plan mode), matching a dismissed `ask_user`.
- Ctrl+P with no plan — no-op hint, never an error.
- Re-presenting (the refine loop) overwrites `current_plan`, the plan file
  (stable slug per session), the pinned title, and re-mounts a fresh `PlanCard`.

## Testing

- **Unit:** `PlanCard` renders summary/steps/choices and resolves each of the
  four options (and a dismiss → "Keep planning"); `PlanScreen` reflects
  `deps.tasks` progress (done/current/pending markers) and shows the path;
  the `TaskPanel` title truncates a long summary and updates on re-present;
  `current_plan` holder set/read/overwrite.
- **Integration:** a `FunctionModel` turn that calls `present_plan` →
  `PlanCard` mounts → a choice resolves → `workspace.mode` flips →
  `on_mode_change` fires → `PlanScreen` shows the steps with progress.
- **Headless / no-UI:** `on_present_plan` is `None` → the existing
  save-and-stay-in-plan-mode path is unchanged (regression guard).

## Out of scope

- A right-side panel (explicitly rejected above).
- Editing the plan from the overlay (read-only for now).
- Persisting plan-overlay open/closed state across sessions.
- Any change to plan-mode permission semantics (`_plan_decision`) or the plan
  file format.
