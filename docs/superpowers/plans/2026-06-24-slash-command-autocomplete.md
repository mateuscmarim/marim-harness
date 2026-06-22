# Slash Command Autocomplete — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an as-you-type autocomplete dropdown for slash commands in the marim TUI.

**Architecture:** A `CommandAutocomplete` widget wraps Textual's `OptionList`, positioned as an overlay above `PromptInput`. The prompt posts `SlashChanged`/`SlashDismissed` messages on text changes; the app shows/hides the autocomplete and replaces prompt text on selection.

**Tech Stack:** Python 3.14, Textual ≥0.80, pytest + Textual pilot

## Global Constraints

- No new dependencies — Textual primitives only (`OptionList`, `Static`, CSS layers)
- No changes to `commands.py` — reads existing `COMMANDS` list and `COMMANDS_BY_NAME`
- Command names only (no argument completions)
- Selection inserts `/<canonical-name> ` (trailing space)
- Case-insensitive prefix matching on names and aliases

---

### Task 1: CommandAutocomplete widget

**Files:**
- Create: `src/marim_harness/interfaces/tui/widgets/autocomplete.py`
- Test: `tests/test_autocomplete.py`

**Interfaces:**
- Consumes: `COMMANDS` list and `COMMANDS_BY_NAME` dict from `marim_harness.interfaces.tui.commands`
- Produces: `CommandAutocomplete` widget with `filter(query: str)` method and `CommandSelected(command_name: str)` message

- [ ] **Step 1: Create the widget module with empty scaffold**

```python
# src/marim_harness/interfaces/tui/widgets/autocomplete.py
"""Slash-command autocomplete dropdown for the TUI prompt.

Displays a filtered list of commands above the prompt when the user types
``/``.  Uses Textual's ``OptionList`` for keyboard/mouse navigation.
"""

from __future__ import annotations

from textual import on
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class CommandAutocomplete(Static):
    """A floating dropdown that shows matching slash commands."""

    class CommandSelected(Message):
        """Posted when the user picks a command from the list."""

        def __init__(self, command_name: str) -> None:
            self.command_name = command_name
            super().__init__()

    class Dismissed(Message):
        """Posted when the widget is dismissed (Escape or empty results)."""

        def __init__(self) -> None:
            super().__init__()

    def __init__(self) -> None:
        super().__init__()
        self._options: list[tuple[str, str, str]] = []  # (name, display, canonical)
        self.can_focus = False

    def compose(self):
        yield OptionList(id="cmd-options")

    def on_mount(self) -> None:
        self.visible = False

    def filter(self, query: str) -> None:
        """Update the dropdown to show commands matching *query*.

        ``query`` is the text after the leading ``/``.  An empty query shows
        all commands.  Matching is a case-insensitive prefix check on the
        command name and all its aliases.
        """
        from ..commands import COMMANDS

        query_lower = query.lower()
        self._options = []
        seen: set[str] = set()
        for cmd in COMMANDS:
            names = [cmd.name, *cmd.aliases]
            if not any(n.lower().startswith(query_lower) for n in names):
                continue
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            display = f"/{cmd.name}  — {cmd.summary}"
            self._options.append((cmd.name, display, cmd.name))

        option_list = self.query_one("#cmd-options", OptionList)
        option_list.clear_options()
        if not self._options:
            self.visible = False
            return
        for _, display, _ in self._options:
            option_list.add_option(Option(display))
        self.visible = True
        # Highlight the first item.
        option_list.highlighted = 0

    @on(OptionList.OptionSelected)
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._options):
            name = self._options[idx][0]
            self.visible = False
            self.post_message(self.CommandSelected(name))

    def dismiss(self) -> None:
        """Hide the dropdown and post Dismissed."""
        self.visible = False
        self.post_message(self.Dismissed())

    def _on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.dismiss()
```

- [ ] **Step 2: Write unit tests for the widget**

```python
# tests/test_autocomplete.py
"""Tests for the slash-command autocomplete dropdown."""

import pytest
from textual.app import App, ComposeResult

from marim_harness.interfaces.tui.widgets.autocomplete import CommandAutocomplete
from marim_harness.interfaces.tui.widgets import PromptInput


class _AcApp(App):
    """Minimal host for testing CommandAutocomplete in isolation."""

    def __init__(self):
        super().__init__()
        self.selected: list[str] = []

    def compose(self) -> ComposeResult:
        yield PromptInput()
        yield CommandAutocomplete()

    def on_command_autocomplete_command_selected(
        self, event: CommandAutocomplete.CommandSelected
    ) -> None:
        self.selected.append(event.command_name)


@pytest.mark.anyio
async def test_filter_empty_query_shows_all_commands():
    """An empty query (bare ``/``) should list every command."""
    from marim_harness.interfaces.tui.commands import COMMANDS

    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("")
        await pilot.pause()
        assert ac.visible is True
        options = ac.query_one("#cmd-options")
        # At least as many options as there are unique command names.
        assert len(options._option_contents) >= len({c.name for c in COMMANDS})


@pytest.mark.anyio
async def test_filter_prefix_matches_name():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("he")
        await pilot.pause()
        assert ac.visible is True
        options = ac.query_one("#cmd-options")
        assert len(options._option_contents) == 1  # only "help"


@pytest.mark.anyio
async def test_filter_prefix_matches_alias():
    """``?`` is an alias for help; ``ls`` for sessions."""
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("?")
        await pilot.pause()
        assert ac.visible is True
        # The canonical name is used, not the alias.
        assert len(ac._options) == 1
        assert ac._options[0][0] == "help"


@pytest.mark.anyio
async def test_filter_is_case_insensitive():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("HELP")
        await pilot.pause()
        assert ac.visible is True
        assert ac._options[0][0] == "help"


@pytest.mark.anyio
async def test_filter_no_match_hides_widget():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("xyz_nonexistent")
        await pilot.pause()
        assert ac.visible is False


@pytest.mark.anyio
async def test_filter_prefix_model():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("mod")
        await pilot.pause()
        assert ac.visible is True
        assert len(ac._options) == 1
        assert ac._options[0][0] == "model"


@pytest.mark.anyio
async def test_filter_alias_ls():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("ls")
        await pilot.pause()
        assert ac.visible is True
        assert len(ac._options) == 1
        assert ac._options[0][0] == "sessions"


@pytest.mark.anyio
async def test_filter_alias_cost():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("cos")
        await pilot.pause()
        assert ac.visible is True
        assert len(ac._options) == 1
        assert ac._options[0][0] == "usage"


@pytest.mark.anyio
async def test_dismiss_hides_and_posts_message():
    dismissed = []

    class DApp(App):
        def compose(self) -> ComposeResult:
            yield CommandAutocomplete()

        def on_command_autocomplete_dismissed(self, _):
            dismissed.append(True)

    app = DApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("help")
        await pilot.pause()
        assert ac.visible is True
        ac.dismiss()
        await pilot.pause()
        assert ac.visible is False
        assert dismissed == [True]


@pytest.mark.anyio
async def test_select_posts_command_selected():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("help")
        await pilot.pause()
        # Simulate selecting the first option.
        ac.query_one("#cmd-options").select(0)
        await pilot.pause()
        assert app.selected == ["help"]
```

- [ ] **Step 3: Run tests to verify they pass**

```bash
cd /home/mateuscmarim/Projects/marim.dev/marim-harness
python -m pytest tests/test_autocomplete.py -v
```

Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/autocomplete.py tests/test_autocomplete.py
git commit -m "feat(tui): CommandAutocomplete widget with unit tests"
```

---

### Task 2: PromptInput slash-detection messages

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/prompt.py`
- Modify: `src/marim_harness/interfaces/tui/widgets/__init__.py`
- Test: `tests/test_autocomplete.py` (append)

**Interfaces:**
- Consumes: existing `PromptInput.on_text_area_changed` method
- Produces: `PromptInput.SlashChanged(value: str)` and `PromptInput.SlashDismissed()` messages

- [ ] **Step 1: Add the new messages and state to PromptInput**

In `src/marim_harness/interfaces/tui/widgets/prompt.py`, add two new message classes inside the `PromptInput` class (after the existing `Submitted` class) and a state flag in `__init__`:

```python
    class SlashChanged(Message):
        """Posted when the first line starts with ``/``."""
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class SlashDismissed(Message):
        """Posted when text stops starting with ``/``."""
        def __init__(self) -> None:
            super().__init__()
```

In `__init__`, add after `self.attachments = []`:

```python
        self._slash_active: bool = False
```

Replace the existing `on_text_area_changed` method body:

```python
    def on_text_area_changed(self, event: "TextArea.Changed") -> None:
        self._resize()
        # Slash-command autocomplete: track when the first line starts with /.
        first_line = self.text.split("\n", 1)[0]
        if first_line.startswith("/"):
            self._slash_active = True
            self.post_message(self.SlashChanged(self.text))
        elif self._slash_active:
            self._slash_active = False
            self.post_message(self.SlashDismissed())
```

- [ ] **Step 2: Add CommandAutocomplete to the widgets __init__ re-exports**

In `src/marim_harness/interfaces/tui/widgets/__init__.py`, add:

After the `from .prompt import PromptInput` line:

```python
from .autocomplete import CommandAutocomplete
```

Add `"CommandAutocomplete"` to the `__all__` list (in the `# input` section):

```python
    # input
    "PromptInput",
    "CommandAutocomplete",
```

- [ ] **Step 3: Write tests for the slash messages**

Append to `tests/test_autocomplete.py`:

```python
# --- PromptInput slash-detection tests ---


@pytest.mark.anyio
async def test_slash_triggers_slash_changed():
    class H(App):
        def __init__(self):
            super().__init__()
            self.events: list[str] = []

        def compose(self) -> ComposeResult:
            yield PromptInput()

        def on_prompt_input_slash_changed(self, event):
            self.events.append(("changed", event.value))

    app = H()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        assert any(e[0] == "changed" for e in app.events)


@pytest.mark.anyio
async def test_deleting_slash_triggers_dismissed():
    class H(App):
        def __init__(self):
            super().__init__()
            self.events: list[str] = []

        def compose(self) -> ComposeResult:
            yield PromptInput()

        def on_prompt_input_slash_changed(self, event):
            self.events.append("changed")

        def on_prompt_input_slash_dismissed(self, _):
            self.events.append("dismissed")

    app = H()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        assert "changed" in app.events
        await pilot.press("backspace")
        await pilot.pause()
        assert "dismissed" in app.events


@pytest.mark.anyio
async def test_normal_text_does_not_trigger_slash():
    class H(App):
        def __init__(self):
            super().__init__()
            self.slash_events: list[str] = []

        def compose(self) -> ComposeResult:
            yield PromptInput()

        def on_prompt_input_slash_changed(self, _):
            self.slash_events.append("changed")

    app = H()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("h", "e", "l", "p")
        await pilot.pause()
        assert app.slash_events == []
```

- [ ] **Step 4: Run all tests**

```bash
cd /home/mateuscmarim/Projects/marim.dev/marim-harness
python -m pytest tests/test_autocomplete.py tests/test_widgets.py tests/test_commands.py -v
```

Expected: All tests PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/prompt.py src/marim_harness/interfaces/tui/widgets/__init__.py tests/test_autocomplete.py
git commit -m "feat(tui): PromptInput posts SlashChanged/SlashDismissed messages"
```

---

### Task 3: App wiring and CSS

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py`
- Modify: `src/marim_harness/interfaces/tui/styles.tcss`

**Interfaces:**
- Consumes: `CommandAutocomplete`, `CommandAutocomplete.CommandSelected`, `PromptInput.SlashChanged`, `PromptInput.SlashDismissed`
- Produces: `_show_autocomplete(query)`, `_hide_autocomplete()` on HarnessApp

- [ ] **Step 1: Add the autocomplete widget to the app's compose and CSS**

In `src/marim_harness/interfaces/tui/app.py`, add to imports at the top (near the existing widget imports):

```python
from .widgets import CommandAutocomplete
```

In the `__init__` method, add after the existing instance variables:

```python
        self._autocomplete: CommandAutocomplete | None = None
```

In `compose()`, add the autocomplete widget before `PromptInput`:

```python
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="log")
        yield JobPanel()
        yield TaskPanel()
        yield Static(self.status.status_text(), id="status-bar")
        yield CommandAutocomplete(id="cmd-autocomplete")
        yield PromptInput(history=self._history)
        yield Footer()
```

- [ ] **Step 2: Add the CSS for the autocomplete widget**

Append to `src/marim_harness/interfaces/tui/styles.tcss`:

```css
/* Slash-command autocomplete dropdown.  Docked to the screen bottom (same
   edge as the Footer), offset upward so it sits directly above the prompt.
   The offset value: -(footer=1 + prompt-border-top=1 + prompt-min-height=3
   + prompt-border-bottom=1) = -6.  Uses layer:overlay so it floats above
   sibling content without pushing the layout. */
#cmd-autocomplete {
    display: none;
    layer: overlay;
    dock: bottom;
    width: 60;
    max-height: 8;
    offset: 0 -6;
    background: $panel;
    border: round $accent;
}
#cmd-autocomplete.visible {
    display: block;
}
#cmd-options {
    height: auto;
    max-height: 6;
    background: $panel;
}
```

- [ ] **Step 3: Wire the event handlers in HarnessApp**

Add these methods to the `HarnessApp` class (after the existing `on_prompt_input_submitted` method):

```python
    # --- Slash-command autocomplete ---

    def _show_autocomplete(self, query: str) -> None:
        if self._autocomplete is None:
            self._autocomplete = self.query_one("#cmd-autocomplete", CommandAutocomplete)
        self._autocomplete.filter(query)

    def _hide_autocomplete(self) -> None:
        if self._autocomplete is not None:
            self._autocomplete.visible = False

    def on_prompt_input_slash_changed(
        self, event: PromptInput.SlashChanged
    ) -> None:
        first_line = event.value.split("\n", 1)[0]
        query = first_line[1:]  # strip the leading /
        self._show_autocomplete(query)

    def on_prompt_input_slash_dismissed(
        self, _event: PromptInput.SlashDismissed
    ) -> None:
        self._hide_autocomplete()

    def on_command_autocomplete_command_selected(
        self, event: CommandAutocomplete.CommandSelected
    ) -> None:
        prompt = self.query_one(PromptInput)
        prompt.text = f"/{event.command_name} "
        prompt.move_cursor(prompt.document.end)
        self._hide_autocomplete()
        prompt.focus()
```

In the existing `on_prompt_input_submitted` method, add `_hide_autocomplete()` as the first line inside the method (before the `text = event.value.strip()` line):

```python
    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self._hide_autocomplete()
        text = event.value.strip()
        # ... rest unchanged
```

- [ ] **Step 4: Run existing app tests to check for regressions**

```bash
cd /home/mateuscmarim/Projects/marim.dev/marim-harness
python -m pytest tests/test_app.py tests/test_widgets.py tests/test_commands.py -v
```

Expected: All existing tests PASS. No regressions.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py src/marim_harness/interfaces/tui/styles.tcss
git commit -m "feat(tui): wire slash-command autocomplete into HarnessApp"
```

---

### Task 4: Integration tests

**Files:**
- Test: `tests/test_autocomplete_integration.py`

**Interfaces:**
- Consumes: `HarnessApp`, `PromptInput`, `CommandAutocomplete`, all wiring from Task 3

- [ ] **Step 1: Write integration tests**

```python
# tests/test_autocomplete_integration.py
"""Integration tests for the slash-command autocomplete in the TUI.

Uses Textual's pilot to simulate real typing and verify the end-to-end flow
from keystroke → autocomplete visible → selection → prompt replacement.
"""

import pytest
from textual.app import App, ComposeResult

from marim_harness.interfaces.tui.widgets.autocomplete import CommandAutocomplete
from marim_harness.interfaces.tui.widgets import PromptInput


class _AcIntegrationApp(App):
    """Minimal app with the autocomplete wired up (mirrors HarnessApp wiring)."""

    def __init__(self):
        super().__init__()
        self._autocomplete = None

    def compose(self) -> ComposeResult:
        yield CommandAutocomplete(id="cmd-autocomplete")
        yield PromptInput()

    def _show_autocomplete(self, query: str) -> None:
        if self._autocomplete is None:
            self._autocomplete = self.query_one("#cmd-autocomplete", CommandAutocomplete)
        self._autocomplete.filter(query)

    def _hide_autocomplete(self) -> None:
        if self._autocomplete is not None:
            self._autocomplete.visible = False

    def on_prompt_input_slash_changed(self, event: PromptInput.SlashChanged) -> None:
        first_line = event.value.split("\n", 1)[0]
        self._show_autocomplete(first_line[1:])

    def on_prompt_input_slash_dismissed(self, _) -> None:
        self._hide_autocomplete()

    def on_command_autocomplete_command_selected(
        self, event: CommandAutocomplete.CommandSelected
    ) -> None:
        prompt = self.query_one(PromptInput)
        prompt.text = f"/{event.command_name} "
        prompt.move_cursor(prompt.document.end)
        self._hide_autocomplete()
        prompt.focus()


@pytest.mark.anyio
async def test_typing_slash_shows_autocomplete():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash")  # types "/"
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True


@pytest.mark.anyio
async def test_typing_slash_he_filters_to_help():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash", "h", "e")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True
        assert len(ac._options) == 1
        assert ac._options[0][0] == "help"


@pytest.mark.anyio
async def test_deleting_slash_dismisses_autocomplete():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True
        await pilot.press("backspace")
        await pilot.pause()
        assert ac.visible is False


@pytest.mark.anyio
async def test_normal_text_no_autocomplete():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("h", "e", "l", "p")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is False


@pytest.mark.anyio
async def test_select_replaces_prompt_text():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash", "h", "e")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True
        # Simulate pressing Enter on the highlighted option.
        ac.query_one("#cmd-options").select(0)
        await pilot.pause()
        assert pi.text == "/help "
        assert ac.visible is False


@pytest.mark.anyio
async def test_bare_slash_shows_all_commands():
    from marim_harness.interfaces.tui.commands import COMMANDS

    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True
        unique_names = {c.name for c in COMMANDS}
        assert len(ac._options) == len(unique_names)
```

- [ ] **Step 2: Run all tests**

```bash
cd /home/mateuscmarim/Projects/marim.dev/marim-harness
python -m pytest tests/test_autocomplete.py tests/test_autocomplete_integration.py tests/test_widgets.py tests/test_commands.py -v
```

Expected: All tests PASS (new + existing).

- [ ] **Step 3: Commit**

```bash
git add tests/test_autocomplete_integration.py
git commit -m "test(tui): integration tests for slash-command autocomplete"
```

---

## Self-Review

**1. Spec coverage:**
- R1 (dropdown on `/`): Task 2 (SlashChanged) + Task 3 (wiring) ✓
- R2 (prefix match, case-insensitive): Task 1 (`filter` method) ✓
- R3 (bare `/` shows all): Task 1 (`filter("")`) + Task 4 test ✓
- R4 (insert + trailing space): Task 3 (`on_command_autocomplete_command_selected`) ✓
- R5 (dismiss on Escape/text-change/submit): Task 1 (dismiss method) + Task 2 (SlashDismissed) + Task 3 (submit handler) ✓
- R6 (no new deps): No imports outside Textual ✓
- R7 (no changes to commands.py): Confirmed — only reads `COMMANDS` ✓

**2. Placeholder scan:** No TBD/TODO/"similar to" patterns. Every code step shows complete code.

**3. Type consistency:** `CommandSelected.command_name: str` flows consistently from widget → app handler → prompt replacement. `filter(query: str)` signature consistent across widget definition and app call site.
