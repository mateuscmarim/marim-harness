# Steer Fallback Key — Design

**Date:** 2026-06-23
**Status:** Approved (pending user spec review)
**Scope:** Add a reliable, universally-encoded keyboard trigger for message **steering**, alongside the existing `Alt+Enter`. Builds on the shipped steering feature.

## Goal

Make mid-turn steering reachable in every terminal. `Ctrl+G` becomes a second steer trigger next to `Alt+Enter`; both inject the typed message (and attachments) into the running turn.

## Background

Steering is triggered in `PromptInput._on_key` (`src/marim_harness/interfaces/tui/widgets/prompt.py`): an `alt+enter` branch posts `PromptInput.Steer(text, attachments)`, which `HarnessApp.on_prompt_input_steer` routes to `Harness.steer`.

A live run revealed that `Alt+Enter` only reaches Textual as `event.key == "alt+enter"` under the **Kitty keyboard protocol** (CSI-u `ESC [ 13 ; 3 u`). With legacy encoding — the default inside tmux and in terminals without the protocol — `Alt+Enter` is byte-for-byte identical to `Enter`, so the app receives `enter` and the message is *queued* instead of steered. (The Pilot test passed because it injects a synthetic `alt+enter`, bypassing terminal encoding.) Verified with a key-logger: `M-Enter`/`ESC+CR` → `enter`; only the CSI-u sequence → `alt+enter`.

`Ctrl+G` encodes as a single control byte (BEL, `0x07`) that every terminal delivers reliably. It is not claimed by the app's `BINDINGS` (`ctrl+t/o/r/c`, `escape`), by Textual's `TextArea` editing bindings (`ctrl+a/c/d/e/k/u/v/w/x/y/z`, `ctrl+left/right`), or by `PromptInput._on_key`, and it is not tmux's prefix (`ctrl+b`) nor a flow-control key (`ctrl+s/q`). So it reaches `_on_key` cleanly everywhere.

## Architecture / Change

1. **`PromptInput._on_key`:** broaden the existing steer branch condition from `event.key == "alt+enter"` to `event.key in ("alt+enter", "ctrl+g")`. The branch body is unchanged — it posts `PromptInput.Steer(self.text, attachments)`, clears attachments, and resets nav. Both keys are equivalent triggers.
2. **Help text:** the welcome/help block (`_WELCOME` in `app.py`) does not currently mention steering. Add one line: `ctrl+g (or alt+enter) steers the running turn`.

No change to `on_prompt_input_steer`, `Harness.steer`, the buffer, the stranded-steer fallback, or any harness code.

## Decisions

- **Keep `Alt+Enter`.** It stays as an intuitive trigger where the Kitty protocol is negotiated (kitty, ghostty, WezTerm, recent Konsole/foot). `Ctrl+G` is the universal fallback, not a replacement.
- **No Kitty-protocol detection / startup warning.** Once `Ctrl+G` works everywhere, a warning that `Alt+Enter` may not register is redundant noise. Dropped (YAGNI).

## Testing

**TUI Pilot (`tests/test_steering.py`):**
- `Ctrl+G` posts a `PromptInput.Steer` carrying the box's text (mirror of the existing `test_alt_enter_posts_steer_message`), confirming the key reaches `_on_key` and routes through the same path.
- The existing `alt+enter` test stays green (the broadened condition still matches it).

Regression: the full suite stays green; `on_prompt_input_steer` and the steering tests are unaffected (only the trigger key set widened).

## Out of Scope / Future

- Kitty-protocol detection/warning (dropped above).
- Making the steer key user-configurable — not needed; `Ctrl+G` + `Alt+Enter` cover the cases.
