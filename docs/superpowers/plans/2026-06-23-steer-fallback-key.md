# Steer Fallback Key Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `Ctrl+G` as a universally-encoded steer trigger alongside `Alt+Enter`, so mid-turn steering works in every terminal (not only those negotiating the Kitty keyboard protocol).

**Architecture:** Broaden the existing steer branch in `PromptInput._on_key` to match `ctrl+g` as well as `alt+enter`; both post the same `PromptInput.Steer` message. Add one help-text line. No harness change.

**Tech Stack:** Textual (TUI, `Pilot` test harness), pytest (`pytest-anyio`), `uv`.

## Global Constraints

- Run tests with `uv run pytest`.
- Keep `Alt+Enter` working — `Ctrl+G` is an additional trigger, not a replacement.
- The broadened branch must post `PromptInput.Steer(self.text, atts)` exactly as the current `alt+enter` branch does (read attachment bytes, clear attachments, `_reset_nav()`).
- No changes to `on_prompt_input_steer`, `Harness.steer`, the buffer, or the stranded-steer fallback.
- No Kitty-protocol detection / startup warning (explicitly out of scope).

---

### Task 1: Add Ctrl+G as a steer trigger + help text

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/prompt.py` (the `alt+enter` branch in `_on_key`)
- Modify: `src/marim_harness/interfaces/tui/app.py` (the `_WELCOME` block)
- Test: `tests/test_steering.py`

**Interfaces:**
- Consumes: `PromptInput.Steer(value, attachments)` (already defined), `HarnessApp.on_prompt_input_steer` (already routes it).
- Produces: no new symbols — only widens the key set that triggers `Steer`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_steering.py` (mirror of the existing `test_alt_enter_posts_steer_message`, pressing `ctrl+g` instead):

```python
@pytest.mark.anyio
async def test_ctrl_g_posts_steer_message(tmp_path):
    from textual.app import App, ComposeResult

    posted = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield PromptInput()

        def on_prompt_input_steer(self, event: PromptInput.Steer) -> None:
            posted.append(event.value)

    app = _App()
    async with app.run_test() as pilot:
        await pilot.pause()
        pi = app.query_one(PromptInput)
        pi.focus()
        pi.text = "steer via ctrl-g"
        await pilot.press("ctrl+g")
        await pilot.pause()
    assert posted == ["steer via ctrl-g"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_steering.py::test_ctrl_g_posts_steer_message -v`
Expected: FAIL — `ctrl+g` is not yet handled, so no `Steer` is posted and `posted` stays empty.

(If this fails instead because `pilot.press("ctrl+g")` doesn't deliver `event.key == "ctrl+g"` to `_on_key`, STOP and report the observed key — but `ctrl+g` is a standard control byte and Textual delivers it as `"ctrl+g"`, so this is not expected.)

- [ ] **Step 3: Broaden the steer branch in `_on_key`**

In `src/marim_harness/interfaces/tui/widgets/prompt.py`, change the steer branch condition:

```python
        if event.key in ("alt+enter", "ctrl+g"):
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Steer(self.text, atts))
            self.attachments = []
            self._reset_nav()
            return
```

(Only the first line — `if event.key == "alt+enter":` → `if event.key in ("alt+enter", "ctrl+g"):` — changes; the body is identical.)

- [ ] **Step 4: Run both steer-key tests to verify they pass**

Run: `uv run pytest tests/test_steering.py::test_ctrl_g_posts_steer_message tests/test_steering.py::test_alt_enter_posts_steer_message -v`
Expected: PASS (both — the broadened condition still matches `alt+enter`).

- [ ] **Step 5: Add the help-text line**

In `src/marim_harness/interfaces/tui/app.py`, add a steering line to `_WELCOME` after the `esc cancels` line:

```python
_WELCOME = (
    "Type a message below to start, or `/help` for commands.\n\n"
    "- `enter` sends · `shift+enter` (or `ctrl+j`) inserts a newline\n"
    "- `ctrl+t` cycles the approval mode (ask → auto → plan)\n"
    "- `esc` cancels the running turn\n"
    "- `ctrl+g` (or `alt+enter`) steers the running turn\n"
    "- `/exit` (or `/quit`, `ctrl+c`) quits"
)
```

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `uv run pytest --no-header -q -o addopts=""`
Expected: PASS — prior count + the one new test, no failures. Any `test_app.py` welcome/intro test that asserts on `_WELCOME` content should still pass (the change only appends a line); if one asserts an exact full string, update it to include the new line.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/prompt.py src/marim_harness/interfaces/tui/app.py tests/test_steering.py
git commit -m "feat(tui): add Ctrl+G as a universal steer trigger alongside Alt+Enter"
```

---

## Self-Review

**Spec coverage:**
- "Broaden `_on_key` to `event.key in ('alt+enter', 'ctrl+g')`, same body" → Step 3. ✔
- "Keep `Alt+Enter`" → Step 4 asserts the alt+enter test still passes. ✔
- "Add one help-text line" → Step 5. ✔
- "Pilot test that `Ctrl+G` posts a `Steer`" → Step 1. ✔
- "No harness / `on_prompt_input_steer` change; no Kitty-protocol warning" → Global Constraints; only `prompt.py`/`app.py`/test touched. ✔

**Placeholder scan:** No TBD/"handle edge cases"/"similar to" — the test and the exact branch/`_WELCOME` code are shown in full. The one STOP-and-report note (Step 2) is a guard, not a placeholder.

**Type consistency:** `PromptInput.Steer(value, attachments)` and `on_prompt_input_steer` match their existing definitions; the new test mirrors the existing `test_alt_enter_posts_steer_message` exactly except the key and assertion text.
