# Inline interaction panels (ask-user & approval)

**Date:** 2026-07-01
**Status:** Approved

## Problem

`AskUserModal` and `ApprovalModal` are Textual `ModalScreen`s pushed with
`push_screen_wait`. A modal screen sits on its own layer and captures all focus
and input, so while a question or approval is pending the transcript underneath
is frozen and mostly hidden. When the agent asks "How should I execute this
plan?", the user cannot scroll back to see what the question refers to.

## Decision

Replace both modals with **inline panel widgets** mounted in the main screen's
vertical stack, just above the status bar — the same region the Job/Task/Queue
panels occupy. The transcript stays fully visible and scrollable while the
agent waits for input. This mirrors how Claude Code's own terminal UI renders
questions and approvals inline.

Alternatives considered and rejected:

- **Scroll-transparent modal** (keep `ModalScreen`, dock it to the bottom,
  forward scroll events to the transcript underneath): smaller diff but fights
  Textual's modal design; mouse wheel over the box still can't reach the
  transcript, and it accumulates event-forwarding hacks.
- **Hide/show toggle key**: trivial but clunky — the user can't read the
  question and the transcript at the same time, which is the whole point.

## Design

### 1. Modal → panel conversion

- `AskUserModal` → `AskUserPanel`, `ApprovalModal` → `ApprovalPanel`, each in
  its existing file (`interfaces/tui/ask_user.py`, `interfaces/tui/approval.py`).
- Base class changes from `ModalScreen[T]` to a plain `Vertical` container.
- Each panel owns an `asyncio.Future` (its *result future*). Every code path
  that today calls `dismiss(value)` instead resolves the future with the same
  value. Result types are unchanged: `dict | None` for ask-user, `bool` for
  approval.
- Internal logic is preserved as-is: question stepping, single/multi select,
  free-text "Other" input, `format_detail` diff rendering, and the existing
  keybindings (`a`/`d` for approve/deny, Enter to select, Esc to cancel/deny).

### 2. Shared await helper

One method on the app, used by both flows:

```python
async def _run_panel(self, panel):  # mounts, focuses, awaits, removes
```

- Mounts the panel into the main screen above the status bar, focuses it,
  and awaits its result future.
- Removal happens in a `finally`, so a cancelled turn (Esc/Ctrl-C), an
  exception, or app shutdown always tears the panel down.
- `_ask_user` and `_request_approval` become thin wrappers over it; their
  signatures and return values to the runtime layer are unchanged. Both keep
  their `_notify` calls.

### 3. Layout

- Panel height `auto`, `max-height: 50%` of the screen, so the transcript
  always keeps at least half the viewport.
- The panel body (option list / diff preview) keeps its internal
  `max-height` + scroll, as the modals already have for large diffs.
- The transcript (`#log`) remains a normal scrollable widget: mouse wheel
  over it works natively because there is no modal layer intercepting events.

### 4. Keyboard scrolling while the panel has focus

The panel binds PageUp/PageDown and Ctrl+Up/Ctrl+Down to forward to the
transcript's scroll actions, so keyboard-only users can scroll the transcript
without moving focus away. Arrow keys and Enter stay with the panel's own
option list.

### 5. Esc semantics

- Esc while the panel is **focused**: cancels the question (ask-user resolves
  `None`) or denies the tool (approval resolves `False`) — same as the modals
  today.
- Esc while focus is **elsewhere**: the existing app-level binding cancels the
  whole turn; the panel is removed by `_run_panel`'s `finally`.

This preserves the reflexive "Esc backs out of the thing I'm looking at"
behavior in both contexts.

### 6. Prompt input coexistence

The regular `PromptInput` stays visible below the panel. Submitting there
while a question is pending keeps today's busy-turn behavior (the message is
queued). Answers to the pending question go only through the panel's option
list or its free-text input.

### 7. Testing

- Update existing modal tests to the panel API.
- New Pilot tests:
  - Transcript scrolls (offset changes) while an ask-user question is pending.
  - Selecting an option resolves the turn with the expected answer mapping.
  - Esc with panel focus cancels the question only (turn continues; panel
    resolves `None` / `False`).
  - Cancelling the turn removes the panel from the DOM.

## Out of scope

- Other modals (model picker, settings, sudo password) stay modal — they are
  context-free dialogs, not conversation turns.
- No changes to the runtime layer (`ask_user` tool, approval controller);
  the UI callback contract is untouched.
