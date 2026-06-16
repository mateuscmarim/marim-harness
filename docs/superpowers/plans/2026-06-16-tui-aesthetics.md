# TUI Aesthetics Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the harness TUI a distinct "refined minimal" identity — four custom Textual themes on a shared dark base, a variable-driven stylesheet, a unified glyph language, and a restyled banner — with zero behavior change.

**Architecture:** Themes live in a new `tui/themes.py` (four `textual.theme.Theme` objects + a name list). The startup theme is persisted in `~/.config/marim/prefs.json` via a new `prefs.py` (mirroring the existing `mcp.py` JSON-in-config_dir pattern). `HarnessApp` registers all four themes, loads the saved one in `on_mount`, and re-saves on change via `watch_theme`. The inline `CSS` string moves to `tui/styles.tcss` (`CSS_PATH`) rewritten against theme variables. A new `/theme` slash command lists/sets themes; the built-in command palette (`Ctrl+P`) also works for free. Glyphs are unified in `widgets.py`.

**Tech Stack:** Python 3, Textual 8.2.7 (`textual.theme.Theme`, `App.register_theme`, reactive `App.theme`, `CSS_PATH`), pytest + anyio, `uv` for running.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/marim_harness/tui/themes.py` (new) | The four `Theme` objects, `MARIM_THEMES` list, `THEME_NAMES` tuple, `DEFAULT_THEME`. |
| `src/marim_harness/prefs.py` (new) | `prefs_path()`, `load_theme()`, `save_theme()` — JSON prefs in `config_dir()`. |
| `src/marim_harness/tui/styles.tcss` (new) | The extracted, variable-driven stylesheet. |
| `src/marim_harness/tui/app.py` (modify) | Register themes, load/persist startup theme, `CSS_PATH`, muted banner. |
| `src/marim_harness/tui/commands.py` (modify) | `/theme [name]` command. |
| `src/marim_harness/tui/widgets.py` (modify) | Unified glyph set; lighter status text. |
| `tests/test_themes.py` (new) | Theme registry invariants. |
| `tests/test_prefs.py` (new) | Prefs round-trip + fallback. |
| `tests/test_commands.py` (modify) | `/theme` list/set/invalid. |
| `tests/test_widgets.py` (modify) | Update glyph assertions to the new set. |

**Conventions to follow (verified in the repo):**
- Run everything with `uv run` (e.g. `uv run pytest ...`, `uv run ruff check .`).
- Async tests use `@pytest.mark.anyio` (see `tests/test_widgets.py`).
- `config_dir()` lives in `marim_harness.config` and returns `$XDG_CONFIG_HOME/marim` (else `~/.config/marim`).
- The command test harness is `_FakeApp` in `tests/test_commands.py`, which records `post_system` calls in `app.posted`.

---

## Task 1: Theme definitions (`themes.py`)

**Files:**
- Create: `src/marim_harness/tui/themes.py`
- Test: `tests/test_themes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_themes.py
from marim_harness.tui.themes import (
    DEFAULT_THEME,
    MARIM_THEMES,
    THEME_NAMES,
)
from textual.theme import Theme


def test_four_themes_defined():
    assert len(MARIM_THEMES) == 4
    assert all(isinstance(t, Theme) for t in MARIM_THEMES)


def test_names_match_themes():
    assert THEME_NAMES == tuple(t.name for t in MARIM_THEMES)
    assert set(THEME_NAMES) == {
        "marim-teal",
        "marim-amber",
        "marim-violet",
        "marim-green",
    }


def test_default_is_teal_and_registered():
    assert DEFAULT_THEME == "marim-teal"
    assert DEFAULT_THEME in THEME_NAMES


def test_all_themes_are_dark_and_share_base():
    backgrounds = {t.background for t in MARIM_THEMES}
    surfaces = {t.surface for t in MARIM_THEMES}
    assert all(t.dark for t in MARIM_THEMES)
    # Shared neutral base: one background, one surface across all four.
    assert len(backgrounds) == 1
    assert len(surfaces) == 1


def test_accents_are_distinct():
    primaries = {t.primary for t in MARIM_THEMES}
    assert len(primaries) == 4  # each theme has its own accent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_themes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.tui.themes'`

- [ ] **Step 3: Write the implementation**

```python
# src/marim_harness/tui/themes.py
"""The harness's custom Textual themes.

Four dark themes sharing one neutral base (background / surface / panel /
foreground), differing only in the accent (``primary``). A shared base keeps the
app feeling like one product with a swappable accent rather than four apps.
``$text-muted`` is provided through each theme's ``variables`` so the stylesheet
can lean on it uniformly.
"""

from textual.theme import Theme

# Shared neutral dark base — identical across every theme.
_BACKGROUND = "#16181d"
_SURFACE = "#1c1f26"
_PANEL = "#232730"
_FOREGROUND = "#d7dae0"
_TEXT_MUTED = "#7c828d"

_BASE = {
    "background": _BACKGROUND,
    "surface": _SURFACE,
    "panel": _PANEL,
    "foreground": _FOREGROUND,
    "dark": True,
    "variables": {"text-muted": _TEXT_MUTED},
}


def _theme(name: str, accent: str) -> Theme:
    """A marim theme: the shared neutral base plus one accent hue."""
    return Theme(
        name=name,
        primary=accent,
        accent=accent,
        # A muted error that still reads on the dark base.
        error="#d9544f",
        warning="#d9a14f",
        success="#5fae7e",
        **_BASE,
    )


MARIM_THEMES = (
    _theme("marim-teal", "#4cb6a8"),
    _theme("marim-amber", "#d6a45c"),
    _theme("marim-violet", "#9a86d4"),
    _theme("marim-green", "#7fae6b"),
)

THEME_NAMES = tuple(t.name for t in MARIM_THEMES)
DEFAULT_THEME = "marim-teal"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_themes.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/marim_harness/tui/themes.py tests/test_themes.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tui/themes.py tests/test_themes.py
git commit -m "feat(tui): define four marim themes on a shared dark base"
```

---

## Task 2: Theme persistence (`prefs.py`)

**Files:**
- Create: `src/marim_harness/prefs.py`
- Test: `tests/test_prefs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prefs.py
import pytest

from marim_harness import prefs


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point config_dir() at a temp dir so tests never touch the real prefs."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_load_theme_defaults_when_missing(cfg_home):
    assert prefs.load_theme() == "marim-teal"


def test_save_then_load_round_trips(cfg_home):
    assert prefs.save_theme("marim-amber") is True
    assert prefs.load_theme() == "marim-amber"


def test_load_theme_rejects_unknown_name(cfg_home):
    # A prefs file naming a theme we no longer ship falls back to the default.
    prefs.prefs_path().parent.mkdir(parents=True, exist_ok=True)
    prefs.prefs_path().write_text('{"theme": "bogus"}', encoding="utf-8")
    assert prefs.load_theme() == "marim-teal"


def test_load_theme_survives_malformed_file(cfg_home):
    prefs.prefs_path().parent.mkdir(parents=True, exist_ok=True)
    prefs.prefs_path().write_text("not json {", encoding="utf-8")
    assert prefs.load_theme() == "marim-teal"


def test_save_rejects_unknown_name(cfg_home):
    assert prefs.save_theme("bogus") is False
    assert prefs.load_theme() == "marim-teal"  # nothing was written
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prefs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.prefs'`

- [ ] **Step 3: Write the implementation**

```python
# src/marim_harness/prefs.py
"""Small persisted user preferences (currently just the chosen theme).

A single JSON file in the per-user config dir, mirroring the JSON-in-config_dir
pattern used by ``mcp.py``. Best-effort throughout: a missing or malformed file
never raises — it falls back to the default theme."""

import json
from pathlib import Path

from .config import config_dir
from .tui.themes import DEFAULT_THEME, THEME_NAMES


def prefs_path() -> Path:
    """The prefs file: ``$XDG_CONFIG_HOME/marim/prefs.json`` (else under
    ``~/.config``)."""
    return config_dir() / "prefs.json"


def _read() -> dict:
    try:
        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_theme() -> str:
    """The saved theme name, or ``DEFAULT_THEME`` when absent/invalid/unknown."""
    name = _read().get("theme")
    return name if name in THEME_NAMES else DEFAULT_THEME


def save_theme(name: str) -> bool:
    """Persist ``name`` as the startup theme. Rejects unknown names (returns
    False, writes nothing). Best-effort: a write failure returns False rather
    than raising."""
    if name not in THEME_NAMES:
        return False
    data = _read()
    data["theme"] = name
    try:
        path = prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_prefs.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/marim_harness/prefs.py tests/test_prefs.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/prefs.py tests/test_prefs.py
git commit -m "feat(prefs): persist chosen theme to prefs.json"
```

---

## Task 3: Extract stylesheet + wire themes into the app

This task is a **pure refactor of styling plus theme registration** — no visual
change yet beyond the accent palette (the tcss rules are byte-equivalent to
today's inline CSS). Visual refinements come in Task 6.

**Files:**
- Create: `src/marim_harness/tui/styles.tcss`
- Modify: `src/marim_harness/tui/app.py` (the `CSS` block at lines 60-76; `on_mount` at line 111)
- Test: `tests/test_app.py` (add a theme-startup test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app.py` (follow the existing `@pytest.mark.anyio` + `run_test()` style already used in that file):

```python
@pytest.mark.anyio
async def test_app_starts_on_saved_marim_theme(tmp_path, monkeypatch):
    """The app registers the marim themes and starts on the persisted one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from marim_harness import prefs
    prefs.save_theme("marim-violet")

    app = _app(tmp_path)  # the existing helper at the top of tests/test_app.py
    async with app.run_test():
        assert app.theme == "marim-violet"
        # All four are registered and selectable.
        assert "marim-teal" in app.available_themes
        assert "marim-green" in app.available_themes
```

> Note for the implementer: `tests/test_app.py` already has an `_app(tmp_path)`
> helper (top of the file) that builds a `HarnessApp` with a `TestModel`. Reuse
> it. `App.available_themes` is a dict keyed by theme name (Textual built-in).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py::test_app_starts_on_saved_marim_theme -v`
Expected: FAIL — `app.theme` is Textual's default (`"textual-dark"`), not `"marim-violet"`.

- [ ] **Step 3a: Create the stylesheet**

```css
/* src/marim_harness/tui/styles.tcss
   The harness's layout + chrome styling. Colors come exclusively from theme
   variables ($primary, $surface, $panel, $text-muted, $error) so switching the
   theme recolors everything. Keep it free of hard-coded colors. */

#log { height: 1fr; padding: 0 1; }

PromptInput { height: 3; max-height: 10; border: none; padding: 0 1; }

#status-bar { height: 1; background: $panel; color: $text-muted; padding: 0 1; }

#task-panel {
    height: auto; max-height: 8; background: $panel; color: $text;
    padding: 0 1; border-top: tall $background;
}

#job-panel {
    height: auto; max-height: 8; background: $panel; color: $text;
    padding: 0 1; border-top: tall $background;
}

.user-msg { color: $accent; text-style: bold; margin-top: 1; }
.error-msg { color: $error; text-style: bold; margin: 1 0; }
.notice-msg { color: $text-muted; text-style: italic; margin: 1 0; }

#banner { color: $accent; text-style: bold; height: auto; margin: 1 0 1 0; }

AssistantMessage { margin: 0 0 1 0; }
ToolCallWidget { margin: 0 0 1 0; }
SubAgentWidget { margin: 0 0 1 0; }
.subagent-body { height: auto; padding: 0 0 0 2; border-left: tall $panel; }
```

- [ ] **Step 3b: Replace the inline CSS with `CSS_PATH` and register themes**

In `src/marim_harness/tui/app.py`, delete the entire `CSS = """ ... """` class
attribute (lines 60-76) and replace it with:

```python
    CSS_PATH = "styles.tcss"
```

Add the import near the other `.` imports at the top of the file:

```python
from .themes import MARIM_THEMES
```

At the **start** of `on_mount` (currently line 111, before `log = self.query_one(...)`), register the themes and select the saved one:

```python
    async def on_mount(self) -> None:
        from ..prefs import load_theme

        for theme in MARIM_THEMES:
            self.register_theme(theme)
        self.theme = load_theme()
        self.title = "marim-harness"
        # ... existing body continues unchanged ...
```

> Keep the rest of `on_mount` exactly as it was; only the three theme lines and
> the existing `self.title = ...` are at the top now.

- [ ] **Step 4: Run the new test + the full app suite**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS, including `test_app_starts_on_saved_marim_theme`.

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/marim_harness/tui/app.py tests/test_app.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tui/styles.tcss src/marim_harness/tui/app.py tests/test_app.py
git commit -m "refactor(tui): move CSS to styles.tcss and register marim themes"
```

---

## Task 4: `/theme` command + persistence on change

**Files:**
- Modify: `src/marim_harness/tui/commands.py` (add `_cmd_theme`; register in `COMMANDS` near line 269)
- Modify: `src/marim_harness/tui/app.py` (add `watch_theme` to persist)
- Test: `tests/test_commands.py`

- [ ] **Step 1: Write the failing command tests**

Add to `tests/test_commands.py`. Extend `_FakeApp` so it carries a settable
`theme` and a known theme list (the real app exposes `available_themes`; the
command only needs to validate against `THEME_NAMES` and set `app.theme`):

```python
from marim_harness.tui.themes import THEME_NAMES


def _theme_app() -> "_FakeApp":
    app = _FakeApp()
    app.theme = "marim-teal"
    return app


@pytest.mark.anyio
async def test_theme_no_arg_lists_themes_and_current():
    app = _theme_app()
    await dispatch(app, "/theme")
    out = "\n".join(app.posted)
    for name in THEME_NAMES:
        assert name in out
    assert "marim-teal" in out  # current is shown


@pytest.mark.anyio
async def test_theme_sets_a_valid_theme():
    app = _theme_app()
    await dispatch(app, "/theme marim-amber")
    assert app.theme == "marim-amber"


@pytest.mark.anyio
async def test_theme_rejects_unknown_name():
    app = _theme_app()
    await dispatch(app, "/theme bogus")
    assert app.theme == "marim-teal"  # unchanged
    assert any("bogus" in m for m in app.posted)


def test_theme_is_a_registered_command():
    assert "theme" in COMMANDS_BY_NAME
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_commands.py -k theme -v`
Expected: FAIL — `theme` not in `COMMANDS_BY_NAME`; no `_cmd_theme`.

- [ ] **Step 3a: Implement the command handler**

Add to `src/marim_harness/tui/commands.py` (near the other `_cmd_*` handlers),
and import the theme names at the top of the file (next to the existing
`from ..permissions import Mode`):

```python
from .themes import THEME_NAMES
```

```python
async def _cmd_theme(app: HarnessApp, arg: str) -> None:
    """List the available themes, or switch to one: ``/theme [name]``."""
    name = arg.strip()
    if not name:
        current = getattr(app, "theme", None)
        lines = ["**Themes**", ""]
        for t in THEME_NAMES:
            marker = " ← active" if t == current else ""
            lines.append(f"- `{t}`{marker}")
        lines += ["", "Switch with `/theme <name>` (or `Ctrl+P` → Change theme)."]
        await app.post_system("\n".join(lines))
        return
    if name not in THEME_NAMES:
        await app.post_system(
            f"Unknown theme: `{name}`. Available: {', '.join(THEME_NAMES)}."
        )
        return
    app.theme = name  # the app's watch_theme persists the choice
```

Register it in the `COMMANDS` list (after the `mode`/`model` entries, near line 270):

```python
    Command("theme", "list or set the color theme: /theme [name]", _cmd_theme),
```

- [ ] **Step 3b: Persist on change in the app**

Add a `watch_theme` method to `HarnessApp` in `src/marim_harness/tui/app.py`
(anywhere among the action/handler methods). Textual calls it automatically
whenever `self.theme` changes:

```python
    def watch_theme(self, theme: str) -> None:
        """Persist the active theme so it's the startup theme next run. Only the
        marim themes are saved; Textual may set built-in defaults during init,
        which save_theme ignores."""
        from ..prefs import save_theme

        save_theme(theme)
```

- [ ] **Step 4: Run the command tests + persistence check**

Run: `uv run pytest tests/test_commands.py -k theme -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/marim_harness/tui/commands.py src/marim_harness/tui/app.py tests/test_commands.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tui/commands.py src/marim_harness/tui/app.py tests/test_commands.py
git commit -m "feat(tui): add /theme command and persist theme on change"
```

---

## Task 5: Unified glyph language (`widgets.py`)

Replace the bracketed glyphs with a single cohesive set:
`·` pending · `✓` done · `✕` denied/error · `▸` sub-agent running · `·` notice.

**Files:**
- Modify: `src/marim_harness/tui/widgets.py` (`ToolCallWidget._summary` line 59-62; `SubAgentWidget._summary` line 176-186; `ErrorMessage` line 100-104; `NoticeMessage` line 107-111)
- Test: `tests/test_widgets.py` (update glyph assertions at lines 53 and 66; subagent at line 401)

- [ ] **Step 1: Update the failing tests first (they assert the OLD glyphs)**

In `tests/test_widgets.py`:
- Line ~53: change `assert "?" in str(w.title)` → `assert "·" in str(w.title)`
- Line ~66: change `assert "+" in str(w.title)  # done glyph` → `assert "✓" in str(w.title)  # done glyph`
- Line ~401 (`test_subagent_finish_clears_activity_from_title`): change `assert "+" in str(w.title)` → `assert "✓" in str(w.title)`

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_widgets.py -v`
Expected: FAIL — titles still contain `[?]` / `[+]`, not `·` / `✓`.

- [ ] **Step 3: Update the glyphs in `widgets.py`**

`ToolCallWidget._summary` (replace the `glyph` dict and bracket wrapping):

```python
    def _summary(self) -> str:
        glyph = {"pending": "·", "done": "✓", "denied": "✕"}.get(self.status, "·")
        arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(self.args.items())[:2])
        return f"{glyph} {self.tool_name}({arg_preview})"
```

`SubAgentWidget._summary` (status glyph + drop the brackets):

```python
    def _summary(self) -> str:
        glyph = {"pending": "▸", "done": "✓", "denied": "✕"}.get(self.status, "▸")
        task = self.agent_task if len(self.agent_task) <= 40 else self.agent_task[:39] + "…"
        parts = [f"{glyph} spawn_agent({self.agent_type}: {task!r})"]
        if self.status == "pending" and self.activity:
            parts.append(self.activity)
        if self.tokens:
            parts.append(f"{human_tokens(self.tokens)} tok")
        return " · ".join(parts)
```

`ErrorMessage` (swap `⚠` for `✕`):

```python
class ErrorMessage(Static):
    """A turn that failed: shown in the log so the session survives the error."""

    def __init__(self, text: str) -> None:
        super().__init__(f"✕ {text}", classes="error-msg")
```

`NoticeMessage` keeps its `•`? No — unify to `·`:

```python
class NoticeMessage(Static):
    """A low-key system note in the log (e.g. history was compacted)."""

    def __init__(self, text: str) -> None:
        super().__init__(f"· {text}", classes="notice-msg")
```

> `UserMessage` already uses `›` (line 97) — leave it; it's the chosen prompt glyph.

- [ ] **Step 4: Run the widget suite**

Run: `uv run pytest tests/test_widgets.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/marim_harness/tui/widgets.py tests/test_widgets.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tui/widgets.py tests/test_widgets.py
git commit -m "feat(tui): unify status/message glyphs across widgets"
```

---

## Task 6: Visual refinements — tool left-rule, muted banner, status weighting

Now the actual minimal-look polish, all in the stylesheet plus one banner color
change and a lighter status line.

**Files:**
- Modify: `src/marim_harness/tui/styles.tcss`
- Modify: `src/marim_harness/tui/widgets.py` (`_status_text` lives in `app.py`, not widgets — see below)
- Modify: `src/marim_harness/tui/app.py` (`_status_text` at lines 195-212)

- [ ] **Step 1: Tool-call left-rule + muted banner in `styles.tcss`**

Edit `src/marim_harness/tui/styles.tcss`:

Change the banner rule from accent-bold to muted (no bold):

```css
#banner { color: $text-muted; height: auto; margin: 1 0 1 0; }
```

Give tool calls and sub-agents an accent left-rule, and align the subagent body
border to the same accent token:

```css
ToolCallWidget { margin: 0 0 1 0; border-left: tall $primary 40%; padding-left: 1; }
SubAgentWidget { margin: 0 0 1 0; }
.subagent-body { height: auto; padding: 0 0 0 2; border-left: tall $primary 40%; }
```

> `border-left: tall $primary 40%` is a muted accent rule — the `│` look from the
> approved mockup. The `40%` is a tint of the theme accent, so it recolors with
> the theme and stays subtle.

- [ ] **Step 2: Verify the app still launches and renders (manual smoke)**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS — the stylesheet still parses and mounts (a tcss syntax error
would fail every `run_test()` here).

- [ ] **Step 3: Lighten the status bar text**

In `src/marim_harness/tui/app.py`, update `_status_text` (lines 195-212) so the
accent is reserved for the session name and the existing context-gauge
thresholds, and the separators read muted. Replace the method body's `base`
assembly with:

```python
        name = getattr(self.harness, "session_name", None)
        prefix = f"[b]{name}[/] · " if name else ""
        sep = " [dim]·[/] "
        base = sep.join(
            [
                f"{prefix}{self.harness.deps.mode.value}",
                cfg,
                ctx,
                f"{_human_tokens(spent)} tokens",
            ]
        )
        return f"{base}{sep}working…" if self._busy else base
```

> The `ctx` string already self-colors at the 75%/90% thresholds (lines 202-205)
> — keep that logic above unchanged. This change only adjusts weighting:
> bold session name, dim separators. `prefix` now wraps the name in `[b]...[/]`,
> so drop the old `name` re-read further down if duplicated.

- [ ] **Step 4: Run the app + status suite**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS. If any test asserts on the exact `_status_text` separator string,
update that assertion to match (search: `uv run pytest tests/ -k status -v`).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/marim_harness/tui/app.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tui/styles.tcss src/marim_harness/tui/app.py
git commit -m "feat(tui): tool left-rule, muted banner, lighter status bar"
```

---

## Task 7: Full suite + manual visual pass

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `uv run pytest -q`
Expected: all pass. Investigate and fix any failure before continuing — do not
claim done with a red suite.

- [ ] **Step 2: Lint the whole tree**

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 3: Manual visual smoke (human-in-the-loop)**

Launch the TUI (`uv run marim` or the project's documented entry point — check
`pyproject.toml [project.scripts]` / `install.sh`). Verify by eye:
- Banner renders muted, seated above the conversation.
- A tool call shows the `· name(...)` summary and an accent left-rule; expanding
  still shows args + result.
- `/theme` with no arg lists all four with the active one marked.
- `/theme marim-amber`, `/theme marim-violet`, `/theme marim-green`, and `Ctrl+P
  → Change theme` each recolor the whole UI (banner rule, tool rule, status
  gauge) — confirming no hard-coded colors survive.
- Restart: the last-selected theme is the startup theme (persistence works).

- [ ] **Step 4: Final commit (if any manual fixes were needed)**

```bash
git add -A
git commit -m "chore(tui): polish pass after manual visual review"
```

---

## Self-Review Notes (for the implementer)

- **Spec coverage:** §3.1 themes → Tasks 1, 3, 4; §3.2 stylesheet extraction →
  Task 3; §3.3 glyphs/left-rule/status → Tasks 5, 6; §3.4 banner → Task 6;
  persistence (§3.1) → Tasks 2, 4. Error handling (§6): unknown `/theme` → Task 4
  test; invalid persisted theme → Task 2 test; config write failure tolerated →
  `save_theme` returns False, Task 2.
- **No new behavior:** every task is presentation-only; behavioral tests must stay
  green (Task 7).
- **Type/name consistency:** `THEME_NAMES`, `MARIM_THEMES`, `DEFAULT_THEME`,
  `load_theme()`, `save_theme()`, `prefs_path()`, `_cmd_theme`, `watch_theme`,
  `CSS_PATH` are used identically wherever they appear across tasks.
- **Known watch-outs:** (1) `register_theme` must run before `self.theme = ...`
  (Task 3 order). (2) Textual sets a default theme during init, firing
  `watch_theme` with a non-marim name — `save_theme` ignores it, so no spurious
  persistence. (3) If `tests/test_app.py`'s app-construction helper differs from
  the assumed `_make_app()`, reuse whatever that file already uses.
