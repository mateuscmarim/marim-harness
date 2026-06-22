# Slash Command Autocomplete — Design Spec

**Date:** 2026-06-24
**Status:** Draft — awaiting user review

## Goal

Add an as-you-type autocomplete dropdown for slash commands in the marim TUI. When the user types `/` followed by characters, a filtered list of matching commands appears above the prompt. Selecting a command inserts it with a trailing space, ready for arguments.

**Scope:** Command name completion only. Argument completions (e.g., `/switch <session-name>`, `/model <id>`) are out of scope for this iteration but the design should not preclude adding them later.

## Requirements

| # | Requirement |
|---|-------------|
| R1 | Dropdown appears when the first line of prompt text starts with `/` |
| R2 | Filters commands by prefix match (name + aliases), case-insensitive |
| R3 | Shows all commands when the query is just `/` with no further characters |
| R4 | Selection inserts `/<command-name> ` (trailing space) into the prompt |
| R5 | Dismisses on: Escape, text no longer starting with `/`, prompt submission |
| R6 | No new dependencies — uses Textual primitives (`OptionList`) |
| R7 | No changes to `commands.py` — reads existing `COMMANDS` list |

## Architecture

### New files

- `src/marim_harness/interfaces/tui/widgets/autocomplete.py` — the `CommandAutocomplete` widget

### Modified files

- `src/marim_harness/interfaces/tui/widgets/prompt.py` — add `SlashChanged` / `SlashDismissed` messages
- `src/marim_harness/interfaces/tui/widgets/__init__.py` — re-export `CommandAutocomplete`
- `src/marim_harness/interfaces/tui/app.py` — wire autocomplete show/hide/selection handlers
- `src/marim_harness/interfaces/tui/styles.tcss` — CSS for the autocomplete widget

### Unchanged files

- `src/marim_harness/interfaces/tui/commands.py` — no changes; autocomplete reads `COMMANDS` and `COMMANDS_BY_NAME`

## Component Design

### CommandAutocomplete (`widgets/autocomplete.py`)

A widget wrapping Textual's `OptionList`.

**Messages:**

- `CommandSelected(command_name: str)` — posted when the user selects a command

**Behaviour:**

- `filter(query: str)` — repopulates the OptionList with commands whose name or aliases start with `query` (case-insensitive). Each option displays `/<name>  — <summary>`. If no commands match, hides the widget.
- Keyboard: ↑↓ navigate options (delegated to OptionList), Enter selects, Escape dismisses.
- Mouse: click on an option selects it.

**CSS:**

- Positioned as a screen-level overlay above the prompt area.
- `layer: overlay` for z-ordering above all other content.
- `max-height: 8` to cap the dropdown size.
- `width` matches the prompt width or a fixed reasonable width (e.g. 60 cols).
- Background and border use theme variables (`$panel`, `$accent`).

### PromptInput changes (`widgets/prompt.py`)

**New messages:**

- `SlashChanged(value: str)` — posted when the first line starts with `/`. `value` is the full prompt text.
- `SlashDismissed()` — posted when text transitions from starting-with-slash to not.

**New state:**

- `_slash_active: bool` — tracks whether we're currently in "slash mode" to avoid posting `SlashDismissed` redundantly.

**Modified `on_text_area_changed`:**

After the existing `_resize()` call, check the first line:

```python
first_line = self.text.split("\n", 1)[0]
if first_line.startswith("/"):
    self._slash_active = True
    self.post_message(self.SlashChanged(self.text))
elif self._slash_active:
    self._slash_active = False
    self.post_message(self.SlashDismissed())
```

No changes to `_on_key` — Tab is not used for selection (Enter/click only).

### App wiring (`app.py`)

**New instance variable:**

- `self._autocomplete: CommandAutocomplete | None = None` — lazily created

**New handlers:**

1. `on_prompt_input_slash_changed(self, event)`:
   - Extract query from first line: `event.value.split("\n", 1)[0][1:]`
   - Call `_show_autocomplete(query)`

2. `on_prompt_input_slash_dismissed(self)`:
   - Call `_hide_autocomplete()`

3. `on_command_autocomplete_selected(self, event)`:
   - Set `prompt.text = f"/{event.command_name} "`
   - Move cursor to end
   - Call `_hide_autocomplete()`

**New methods:**

- `_show_autocomplete(query: str)`:
  - Lazily create and mount `CommandAutocomplete` on the screen
  - Call `filter(query)`
  - Set `visible = True`

- `_hide_autocomplete()`:
  - Set `visible = False` (or no-op if not created yet)

**Modified `on_prompt_input_submitted`:**
- Add `_hide_autocomplete()` call at the top (before existing logic)

### CSS (`styles.tcss`)

```css
CommandAutocomplete {
    layer: overlay;
    dock: bottom;
    width: 60;
    max-height: 8;
    offset: 0 -<prompt_height_plus_margin>;  /* positioned above prompt */
    background: $panel;
    border: round $accent;
}
```

The exact positioning mechanism (dock + offset, absolute positioning, or container wrapping) will be refined during implementation. The goal is to anchor the dropdown directly above the PromptInput, aligned to its left edge.

## Edge Cases

1. **Empty query (`/` only):** Show all commands — same as `/help` listing but inline.
2. **Alias match:** Typing `/?` shows `help` (since `?` is an alias). The inserted text is `/help ` (canonical name, not the alias the user typed).
3. **Multi-line prompt:** Slash detection only checks the first line. If the user types `/model\nsome context`, the autocomplete still appears for `/model` and selecting it replaces the first line.
4. **No matches:** Widget hides. User continues typing and the command is handled by the existing unknown-command error path on submit.
5. **Rapid typing:** `on_text_area_changed` fires on every keystroke. `filter()` is O(n) over ~18 commands — no performance concern.
6. **Backspace to empty:** Deleting back to just `/` re-shows the full list. Deleting the `/` itself dismisses.

## Future Extensibility

Argument completions (out of scope now) would extend this design:

- `Command` dataclass gains an optional `completer: Callable[[str], list[str]]` field
- `CommandAutocomplete` checks if the selected command has a completer; if so, after the command is inserted, it enters argument-completion mode
- Each command defines its own completer (e.g., `/switch` queries session names, `/model` queries available models)

This requires no architecture changes — just new data on `Command` and a second mode in the autocomplete widget.

## Testing

### Unit tests (`tests/test_autocomplete.py`)

| Test | Expected |
|------|----------|
| Empty query shows all commands | OptionList contains all COMMANDS entries |
| Prefix `"he"` → `[help]` | Single option |
| Prefix `"mo"` → `[model]` | Single option |
| Alias `"?"` → `[help]` | Matched via alias |
| Alias `"ls"` → `[sessions]` | Matched via alias |
| Alias `"cost"` → `[usage]` | Matched via alias |
| Case `"HELP"` → `[help]` | Case-insensitive |
| No match `"xyz"` → empty | Widget hides or shows empty state |
| Selection posts `CommandSelected("help")` | Message received |

### Integration tests (`tests/test_autocomplete_integration.py`)

| Test | Expected |
|------|----------|
| Type `/he` → autocomplete visible | Widget appears with `help` |
| Select `help` from autocomplete | Prompt text = `/help `, widget hidden |
| Type `/` → autocomplete with all commands | Full list visible |
| Delete `/` → autocomplete dismissed | Widget hidden |
| Type normal text → no autocomplete | Widget never appears |
| Submit `/help` without autocomplete | Command still dispatches normally |

### Regression

- Run existing `tests/test_commands.py` — no changes expected.
- Run existing `tests/test_widgets.py` — confirm no PromptInput regressions.
