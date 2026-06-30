# Settings Screen UX Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the TUI settings screen into focused topic pages with per-field auto-save and clear live-vs-relaunch tagging.

**Architecture:** Two changes to one screen file. First, replace the single "Save to .env" button with per-field auto-save driven by a declarative widget-id→env-key registry, so each checkbox/radio commits on change and each integer/text input commits on Enter/blur with inline rejection of bad values. Second, split the `Config` catch-all rail section into topic pages (Session, Context & Memory, Tools, Notifications, Advanced) and dissolve `Runtime` into `Session`, reusing the same registry and stable widget ids so handlers don't change.

**Tech Stack:** Python 3.10+, Textual, Pydantic, pytest (anyio), uv.

## Global Constraints

- Python `>=3.10`; no 3.11+-only syntax.
- `uv` for everything (`uv run …`); never bare `python`/`pip`/`pytest`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- CI order, run locally before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Env var names are fixed and must not change: `MARIM_DEFAULT_MODE`, `MARIM_TOOL_SEARCH`, `MARIM_TOOL_SEARCH_THRESHOLD`, `MARIM_LSP`, `MARIM_LSP_TOOLS`, `MARIM_JOB_TOOL_COMBINED`, `MARIM_PROACTIVE_MEMORY`, `MARIM_MASK_OBSERVATIONS`, `MARIM_MASK_KEEP_RECENT`, `MARIM_MASK_MIN_CHARS`, `MARIM_MAX_CONTEXT_TOKENS`, `MARIM_SUBAGENT_REQUEST_LIMIT`, `MARIM_NOTIFICATIONS`, `MARIM_NOTIFICATION_EVENTS`.
- `_b(value: bool) -> str` returns `"1"`/`"0"`; boolean env vars use it.
- `save_env_settings(values: dict[str, str])` (already exists in `config/persist.py`) merges keys into the global `.env` in place, preserves other lines, writes atomically, and mirrors each value into `os.environ`. Single-key dicts are safe.

## File Structure

- Modify: `src/marim_harness/interfaces/tui/settings.py` — the whole screen. Both tasks live here.
- Modify: `tests/test_settings_screen.py` — replace save-button tests with auto-save tests (Task 1); add per-page mount tests (Task 2).
- Unchanged: `src/marim_harness/interfaces/tui/app.py:716` — the `SettingsScreen(harness=, current_theme=, env_cfg=)` constructor signature is preserved, so the call site needs no edit. Verify, don't modify.

---

### Task 1: Per-field auto-save (replace the Save button)

Convert env persistence from one batched `_save_env` + `Save to .env` button to per-field auto-save, on the **existing** rail layout (Runtime / Theme / MCP / Config). Introduce the widget-id→env-key registry that Task 2 reuses. Widget ids are unchanged from today.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings.py`
- Test: `tests/test_settings_screen.py`

**Interfaces:**
- Consumes: `save_env_settings` from `...config`; `ModelConfig`, `Mode`; existing widget ids (`sw-lsp`, `sw-lsp-tools`, `sw-job`, `sw-mem`, `sw-mask-obs`, `sw-notifications`, `ctx-input`, `toolsearch-threshold`, `mask-keep-recent`, `mask-min-chars`, `subagent-req-limit`, `notif-events-input`, `default-mode-set`, `toolsearch-set`, `mode-set`, `model-change`).
- Produces: module-level registries `_ENV_CHECKBOXES: dict[str, str]`, `_ENV_INT_INPUTS: dict[str, tuple[str, str]]`, `_ENV_RADIOS: dict[str, tuple[str, tuple[str, ...]]]`, `_ENV_TEXT_INPUTS: dict[str, str]`; instance flag `self._ready: bool`; methods `_commit_env(env_key: str, value: str) -> None`, `_commit_int(widget_id: str) -> None`, `_status(msg: str) -> None`. Footer status widget id `#settings-status`. The `_save_env` method and `#save-env` button are removed.

- [ ] **Step 1: Add the registries and the mount-ready guard (failing test first)**

Add to `tests/test_settings_screen.py` (the existing `_fake_harness`, `_env_cfg`, `_Host`, `isolated_env` fixtures stay). Replace the old `_goto_config` helper body to still reach Config (4th section) for now:

```python
@pytest.mark.anyio
async def test_open_does_not_write_env(isolated_env, monkeypatch, tmp_path):
    """Opening the screen must not write .env (mount-time Changed events are ignored)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
    assert not (tmp_path / "marim" / ".env").exists()
```

- [ ] **Step 2: Run it — expect PASS-by-accident or FAIL**

Run: `uv run pytest tests/test_settings_screen.py::test_open_does_not_write_env -v`
Expected: today this PASSES (no auto-save exists yet). That's fine — it's the invariant we must not break. Keep it.

- [ ] **Step 3: Add registries + `_ready` flag + footer status in `settings.py`**

After the `_TOOL_SEARCH_MODES` constant, add:

```python
# Auto-save registries: widget id -> what to persist. The same ids are used in
# both the old single-Config layout and the topic-page layout, so these maps are
# the single source of truth for persistence and survive the page restructure.
_ENV_CHECKBOXES: dict[str, str] = {
    "sw-lsp": "MARIM_LSP",
    "sw-lsp-tools": "MARIM_LSP_TOOLS",
    "sw-job": "MARIM_JOB_TOOL_COMBINED",
    "sw-mem": "MARIM_PROACTIVE_MEMORY",
    "sw-mask-obs": "MARIM_MASK_OBSERVATIONS",
    "sw-notifications": "MARIM_NOTIFICATIONS",
}
# widget id -> (env var, human label for the "must be a positive integer" error)
_ENV_INT_INPUTS: dict[str, tuple[str, str]] = {
    "ctx-input": ("MARIM_MAX_CONTEXT_TOKENS", "Context budget"),
    "toolsearch-threshold": ("MARIM_TOOL_SEARCH_THRESHOLD", "Tool-search threshold"),
    "mask-keep-recent": ("MARIM_MASK_KEEP_RECENT", "Mask: keep recent returns"),
    "mask-min-chars": ("MARIM_MASK_MIN_CHARS", "Mask: min chars to elide"),
    "subagent-req-limit": ("MARIM_SUBAGENT_REQUEST_LIMIT", "Sub-agent request limit"),
}
# radio set id -> (env var, ordered choices)
_ENV_RADIOS: dict[str, tuple[str, tuple[str, ...]]] = {
    "default-mode-set": ("MARIM_DEFAULT_MODE", _MODES),
    "toolsearch-set": ("MARIM_TOOL_SEARCH", _TOOL_SEARCH_MODES),
}
_ENV_TEXT_INPUTS: dict[str, str] = {"notif-events-input": "MARIM_NOTIFICATION_EVENTS"}
```

In `__init__`, after `self._rail_ids = …`, add:

```python
        # Gate auto-save until the initial widget tree has mounted: setting widget
        # values during compose fires Changed events we must not persist.
        self._ready = False
```

At the **end** of `on_mount`, after `self._paint_themes()`, add:

```python
        self._ready = True
```

- [ ] **Step 4: Add commit helpers + footer status in `settings.py`**

Add these methods (place near `_positive_int`, which stays):

```python
    def _status(self, msg: str) -> None:
        self.query_one("#settings-status", Static).update(msg)

    def _commit_env(self, env_key: str, value: str) -> None:
        """Persist a single env var to the global .env, surfacing the result in the
        footer status. Used by every auto-saving widget."""
        try:
            save_env_settings({env_key: value})
        except Exception as exc:  # surface any write failure on the status line
            self._status(f"Save failed: {exc}")
            return
        self._status(f"✓ saved {env_key} · applies next launch")

    def _commit_int(self, widget_id: str) -> None:
        """Validate and persist one integer Input. A blank/invalid/≤0 value is
        rejected with a field-specific message and nothing is written."""
        env_key, label = _ENV_INT_INPUTS[widget_id]
        value = self._positive_int(f"#{widget_id}")
        if value is None:
            self._status(f"{label} must be a positive integer.")
            return
        self._commit_env(env_key, str(value))
```

- [ ] **Step 5: Replace the event handlers in `settings.py`**

Replace `on_radio_set_changed` with a version that handles live mode **and** env radios:

```python
    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.index is None:
            return
        rid = event.radio_set.id or ""
        if rid == "mode-set":
            self.harness.set_mode(Mode(_MODES[event.index]))
            self.query_one("#badge-runtime", Static).update(
                self.harness.deps.workspace.mode.value
            )
            self.app.status.refresh_status()  # type: ignore[attr-defined]
            return
        if not self._ready:
            return
        spec = _ENV_RADIOS.get(rid)
        if spec is not None:
            env_key, choices = spec
            self._commit_env(env_key, choices[event.index])
```

Extend `on_checkbox_changed` to auto-save env checkboxes (keep the MCP branch):

```python
    async def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        if cid.startswith("mcp-toggle-"):
            await self._toggle_mcp(int(cid.removeprefix("mcp-toggle-")), event.value)
            return
        if not self._ready:
            return
        env_key = _ENV_CHECKBOXES.get(cid)
        if env_key is not None:
            self._commit_env(env_key, _b(event.value))
```

Add input commit handlers (Enter and blur both commit; blur covers tab-away):

```python
    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._commit_input(event.input.id or "")

    def on_input_blurred(self, event: Input.Blurred) -> None:
        if self._ready:
            self._commit_input(event.input.id or "")

    def _commit_input(self, widget_id: str) -> None:
        if not self._ready:
            return
        if widget_id in _ENV_INT_INPUTS:
            self._commit_int(widget_id)
        elif widget_id in _ENV_TEXT_INPUTS:
            self._commit_env(
                _ENV_TEXT_INPUTS[widget_id],
                self.query_one(f"#{widget_id}", Input).value.strip(),
            )
```

Update `on_button_pressed` to drop the `save-env` branch (keep `model-change`):

```python
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if (event.button.id or "") == "model-change":
            self._open_model_picker()
```

- [ ] **Step 6: Remove the Save button, old banner, and `_save_env` from `_config_widgets`**

In `_config_widgets`, delete the `Button("Save to .env", id="save-env", …)` line and the `Static("", id="save-status")` line. Change the leading muted line from `"Saved to .env — applies on next launch."` to `"Changes save automatically — apply on next launch."`. Delete the entire `_save_env` method. Add the footer status widget to `compose` (next to the hints): change the hints footer to a horizontal row holding both the hint and the status:

In `compose`, replace `yield Static(_SETTINGS_HINTS, id="settings-hints")` with:

```python
        with Horizontal(id="settings-footer"):
            yield Static(_SETTINGS_HINTS, id="settings-hints")
            yield Static("", id="settings-status")
```

Update `_SETTINGS_HINTS` to: `"↑↓ section · enter edit · changes save automatically · esc close"`. Add CSS for the footer row:

```css
    #settings-footer { height: 1; background: $panel; }
    #settings-hints { padding: 0 1; color: $text-muted; width: auto; }
    #settings-status { width: 1fr; color: $text-muted; content-align: right middle; padding: 0 1; }
```

(Remove the old standalone `#settings-hints { height: 1; … }` rule it replaces.)

- [ ] **Step 7: Rewrite the persistence tests for auto-save**

In `tests/test_settings_screen.py`, **delete** `test_save_writes_env_file`, `test_mask_observations_toggle_saves`, `test_invalid_mask_threshold_blocks_save`, `test_invalid_context_budget_blocks_save`, `test_default_mode_radio_reflects_config_and_saves`, `test_tool_search_selector_saves`, `test_subagent_request_limit_reflects_config_and_saves`, `test_invalid_request_limit_blocks_save` (they call the removed `_save_env`/`#save-env`). Replace with auto-save equivalents:

```python
@pytest.mark.anyio
async def test_checkbox_autosaves(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())  # lsp_enabled defaults True
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await _goto_config(pilot)
        _scroll_to(app, "#sw-lsp")
        await pilot.click("#sw-lsp")  # toggle LSP off
        await pilot.pause()
    assert "MARIM_LSP=0" in (tmp_path / "marim" / ".env").read_text()


@pytest.mark.anyio
async def test_int_input_autosaves_on_submit(isolated_env, monkeypatch, tmp_path):
    from textual.widgets import Input
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await _goto_config(pilot)
        inp = app.screen.query_one("#subagent-req-limit", Input)
        inp.value = "120"
        app.screen._commit_input("subagent-req-limit")  # what Enter/blur trigger
        await pilot.pause()
    assert os.environ.get("MARIM_SUBAGENT_REQUEST_LIMIT") == "120"


@pytest.mark.anyio
async def test_invalid_int_rejected_no_write(isolated_env, monkeypatch, tmp_path):
    from textual.widgets import Input
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await _goto_config(pilot)
        app.screen.query_one("#mask-keep-recent", Input).value = "0"
        app.screen._commit_input("mask-keep-recent")
        await pilot.pause()
        status = str(app.screen.query_one("#settings-status").render())
    assert not (tmp_path / "marim" / ".env").exists()
    assert "positive integer" in status


@pytest.mark.anyio
async def test_radio_autosaves(isolated_env, monkeypatch, tmp_path):
    from textual.widgets import RadioButton
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MARIM_DEFAULT_MODE", raising=False)
    app = _Host(_fake_harness(), _env_cfg())  # default_mode == "ask"
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "config"
        await pilot.pause()
        app.screen.query_one("#defmode-plan", RadioButton).value = True
        await pilot.pause()
    assert os.environ.get("MARIM_DEFAULT_MODE") == "plan"
```

Note: `_goto_config` and `_scroll_to` helpers stay as-is for this task (rail is still 4 sections). `test_mode_radio_applies_live` and `test_theme_applies_live` stay unchanged.

- [ ] **Step 8: Run the settings tests**

Run: `uv run pytest tests/test_settings_screen.py -v`
Expected: PASS (all auto-save + retained live/nav/escape tests).

- [ ] **Step 9: Lint, type-check, commit**

```bash
uv run ruff check src tests
uv run pyright
git add src/marim_harness/interfaces/tui/settings.py tests/test_settings_screen.py
git commit -m "feat: per-field auto-save in settings, drop Save button"
```

Expected: ruff clean, pyright clean.

---

### Task 2: Topic pages, live/relaunch tags, polish

Split the rail into 7 topic pages, dissolve `Runtime` into `Session`, rehome every field, add `live` / `next launch` tags, and tighten alignment. Widget ids and the Task 1 registries/handlers are unchanged — this is layout reorganization plus tags.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings.py`
- Test: `tests/test_settings_screen.py`

**Interfaces:**
- Consumes: Task 1 registries/handlers; existing widget ids; `THEME_NAMES`, `MARIM_THEMES`, `ModelPickerModal`.
- Produces: `_SECTIONS = (("session",…),("theme",…),("mcp",…),("context",…),("tools",…),("notifications",…),("advanced",…))`; section builders `_session_widgets`, `_context_widgets`, `_tools_widgets`, `_notifications_widgets`, `_advanced_widgets` (replacing `_runtime_widgets` and `_config_widgets`); `active_section` default `"session"`; helper `_tag(live: bool) -> Static` for the row tag.

- [ ] **Step 1: Write the page-structure tests first**

In `tests/test_settings_screen.py`, update the module docstring's section list to `Session / Theme / MCP servers / Context & Memory / Tools / Notifications / Advanced`. Change `_Host` default `current_theme=THEME_NAMES[1]` (unchanged). Replace `test_opens_on_runtime_section` and `test_down_arrow_switches_section`, and fix `_goto_config`:

```python
@pytest.mark.anyio
async def test_opens_on_session_section():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.active_section == "session"
        assert screen.query_one("#section-session").display is True
        assert screen.query_one("#section-theme").display is False


@pytest.mark.anyio
async def test_every_page_mounts_its_fields():
    """Each topic page owns its expected widgets; no field appears twice."""
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        s = app.screen
        # Session: live mode + relaunch default-mode are distinct widgets.
        assert s.query_one("#section-session #mode-set") is not None
        assert s.query_one("#section-session #default-mode-set") is not None
        # Context & Memory owns the single context-budget input (de-duplicated).
        assert s.query_one("#section-context #ctx-input") is not None
        assert len(s.query("#ctx-input")) == 1
        # Tools owns LSP + tool-search.
        assert s.query_one("#section-tools #sw-lsp") is not None
        assert s.query_one("#section-tools #toolsearch-set") is not None
        # Notifications owns the events input.
        assert s.query_one("#section-notifications #notif-events-input") is not None


@pytest.mark.anyio
async def test_down_arrow_switches_section():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        screen = app.screen
        assert screen.active_section == "theme"
        assert screen.query_one("#section-theme").display is True
        assert screen.query_one("#section-session").display is False
```

Update the `_goto_config` helper (Task 1 tests reuse it) to navigate to the new home of the field each uses, or simpler — set the section directly. Replace its body:

```python
async def _goto_config(pilot):
    """Reach a relaunch page. Context & Memory is the 4th rail section."""
    pilot.app.screen.active_section = "context"
    await pilot.pause()
```

Then in the Task 1 tests, point `_goto_config` users at the right page by setting `active_section` explicitly where a specific widget is needed (e.g. `#sw-lsp` lives on `tools`, `#subagent-req-limit` on `tools`, `#mask-keep-recent` on `context`). Update those three tests to set `active_section` to `"tools"` / `"context"` accordingly instead of calling `_goto_config`.

- [ ] **Step 2: Run the new tests — expect FAIL**

Run: `uv run pytest tests/test_settings_screen.py::test_opens_on_session_section -v`
Expected: FAIL — `active_section` is still `"runtime"`, `#section-session` does not exist.

- [ ] **Step 3: Rewrite `_SECTIONS`, `active_section`, and rail badges**

```python
_SECTIONS = (
    ("session", "Session"),
    ("theme", "Theme"),
    ("mcp", "MCP servers"),
    ("context", "Context & Memory"),
    ("tools", "Tools"),
    ("notifications", "Notifications"),
    ("advanced", "Advanced"),
)
```

Change the reactive default: `active_section: reactive[str] = reactive("session")`.

In `_rail_badge`, replace the `"runtime"` key with `"session"` (same body — current mode), keep `"theme"`/`"mcp"`, and `return ""` for the rest. In `on_radio_set_changed`'s live-mode branch, the badge id is now `#badge-session` (rename from `#badge-runtime`).

- [ ] **Step 4: Add the `_tag` helper and tag CSS**

```python
    def _tag(self, live: bool) -> Static:
        return Static("live" if live else "next launch", classes="field-tag")
```

CSS additions:

```css
    .field-tag { width: auto; color: $text-muted; }
    .field-row { layout: horizontal; height: 1; }
    .field-row .field-main { width: 1fr; }
```

- [ ] **Step 5: Replace `_runtime_widgets`/`_config_widgets` with the five page builders**

```python
    def _session_widgets(self) -> ComposeResult:
        yield Static("Mode & model apply live; default mode applies next launch.",
                     classes="muted")
        yield Label("Mode (this session)")
        with RadioSet(id="mode-set"):
            for name in _MODES:
                yield RadioButton(
                    name, value=(name == self.harness.deps.workspace.mode.value),
                    id=f"mode-{name}",
                )
        with Horizontal(classes="srow"):
            yield Static(f"Model: {self.harness.model_label}", id="model-label")
            yield Button("change", id="model-change", variant="primary")
        yield Label("Default mode (new sessions)")
        with RadioSet(id="default-mode-set"):
            for name in _MODES:
                yield RadioButton(
                    name, value=(name == self.env_cfg.default_mode),
                    id=f"defmode-{name}",
                )

    def _context_widgets(self) -> ComposeResult:
        yield Static("Saved to .env — applies on next launch.", classes="muted")
        with Horizontal(classes="frow"):
            yield Label("Context budget (tokens)")
            yield Input(value=str(self.env_cfg.max_context_tokens),
                        id="ctx-input", type="integer")
        yield BoxCheckbox("Mask stale observations at compaction",
                          value=self.env_cfg.mask_observations, id="sw-mask-obs")
        with Horizontal(classes="frow"):
            yield Label("Mask: keep recent returns")
            yield Input(value=str(self.env_cfg.mask_keep_recent),
                        id="mask-keep-recent", type="integer")
        with Horizontal(classes="frow"):
            yield Label("Mask: min chars to elide")
            yield Input(value=str(self.env_cfg.mask_min_chars),
                        id="mask-min-chars", type="integer")
        yield BoxCheckbox("Proactive memory",
                          value=self.env_cfg.proactive_memory, id="sw-mem")

    def _tools_widgets(self) -> ComposeResult:
        yield Static("Saved to .env — applies on next launch.", classes="muted")
        yield BoxCheckbox("LSP", value=self.env_cfg.lsp_enabled, id="sw-lsp")
        yield BoxCheckbox("LSP navigation tools",
                          value=self.env_cfg.lsp_tools_enabled, id="sw-lsp-tools")
        yield BoxCheckbox("Job tool combined",
                          value=self.env_cfg.job_tool_combined, id="sw-job")
        yield Label("Tool search (MCP/plugin tools)")
        with RadioSet(id="toolsearch-set"):
            for name in _TOOL_SEARCH_MODES:
                yield RadioButton(name, value=(name == self.env_cfg.tool_search),
                                  id=f"toolsearch-{name}")
        with Horizontal(classes="frow"):
            yield Label("Tool-search threshold")
            yield Input(value=str(self.env_cfg.tool_search_threshold),
                        id="toolsearch-threshold", type="integer")
        with Horizontal(classes="frow"):
            yield Label("Sub-agent request limit")
            yield Input(value=str(self.env_cfg.subagent.request_limit),
                        id="subagent-req-limit", type="integer")

    def _notifications_widgets(self) -> ComposeResult:
        yield Static("Saved to .env — applies on next launch.", classes="muted")
        yield BoxCheckbox("Desktop notifications",
                          value=self.env_cfg.notifications.enabled, id="sw-notifications")
        with Horizontal(classes="frow"):
            yield Label("Notification events")
            yield Input(value=", ".join(sorted(self.env_cfg.notifications.events)),
                        id="notif-events-input")

    def _advanced_widgets(self) -> ComposeResult:
        deny = ", ".join(self.env_cfg.command_denylist) or "(none)"
        allow = ", ".join(self.env_cfg.command_allowlist) or "(none)"
        trust = "on" if self.env_cfg.trust_project_hooks else "off"
        yield Static("Read-only — managed in config or project settings.", classes="muted")
        yield Static(f"Command denylist: {deny}", classes="muted")
        yield Static(f"Command allowlist: {allow}", classes="muted")
        yield Static(f"Trust project hooks: {trust}", classes="muted")
        yield Static(f"Config file: {global_config_path()}", classes="muted")
```

(Keep `_theme_widgets` and `_mcp_widgets` as-is. Delete `_runtime_widgets` and `_config_widgets`.)

- [ ] **Step 6: Rebuild `compose`'s content pane to mount the 7 sections**

Replace the `VerticalScroll(id="settings-content")` block's children with one `Vertical(id=f"section-{key}")` per section, each yielding from its builder:

```python
            with VerticalScroll(id="settings-content"):
                with Vertical(id="section-session"):
                    yield from self._session_widgets()
                with Vertical(id="section-theme"):
                    yield from self._theme_widgets()
                with Vertical(id="section-mcp"):
                    yield from self._mcp_widgets()
                with Vertical(id="section-context"):
                    yield from self._context_widgets()
                with Vertical(id="section-tools"):
                    yield from self._tools_widgets()
                with Vertical(id="section-notifications"):
                    yield from self._notifications_widgets()
                with Vertical(id="section-advanced"):
                    yield from self._advanced_widgets()
```

- [ ] **Step 7: Run the full settings suite**

Run: `uv run pytest tests/test_settings_screen.py -v`
Expected: PASS. If a Task 1 test still navigates to a field not on `context`, fix it to set the correct `active_section` (`tools` for `#sw-lsp`/`#subagent-req-limit`, `context` for `#mask-keep-recent`).

- [ ] **Step 8: Lint, type-check, full suite, commit**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
git add src/marim_harness/interfaces/tui/settings.py tests/test_settings_screen.py
git commit -m "feat: split settings into topic pages with live/relaunch tags"
```

Expected: all green.

---

## Self-Review

**Spec coverage:**
- Rail → 7 topic pages: Task 2 Step 3/6. ✓
- Field→page mapping (every setting rehomed, none dropped): Task 2 Step 5. ✓
- Duplicate Context budget collapsed; Mode vs Default mode on one page: Task 2 Step 5 (`_session_widgets`, single `#ctx-input`) + `test_every_page_mounts_its_fields`. ✓
- Auto-save: checkboxes/radios on change, ints on Enter/blur, invalid rejected: Task 1 Steps 4–5, tests Step 7. ✓
- No Save button; footer status; banner replaced: Task 1 Step 6. ✓
- Live vs relaunch tagging: `_tag` helper Task 2 Step 4 (applied via `.muted` page intros + tag class; tags are static labels). ✓
- Polish (alignment, rhythm, badges, footer hint): Task 1 Step 6 (footer hint) + Task 2 Step 4 CSS. ✓
- `save_env_settings` single-key safety: Global Constraints (verified in spec). ✓
- Advanced read-only: Task 2 `_advanced_widgets`. ✓

**Note on the `_tag` helper:** the design calls for a per-row `live`/`next launch` tag. To keep rows simple and avoid restructuring every `Horizontal` into a 3-column grid, the page-intro muted line states the apply-semantics per page (every field on a page shares one semantic except Session), and Session's two radios are explicitly labeled "(this session)" vs "(new sessions)". The `_tag` helper + `.field-tag` CSS are available if per-row tags are wanted during review; Session is the only mixed page and its labels already disambiguate. This is a deliberate YAGNI simplification — flag at review if per-row tags are required.

**Placeholder scan:** no TBD/TODO; every code step shows real code. ✓

**Type consistency:** registry names (`_ENV_CHECKBOXES`, `_ENV_INT_INPUTS`, `_ENV_RADIOS`, `_ENV_TEXT_INPUTS`), methods (`_commit_env`, `_commit_int`, `_commit_input`, `_status`, `_tag`), ids (`#settings-status`, `#badge-session`, `#section-*`) are used consistently across both tasks. Live-mode badge renamed `runtime`→`session` in both `_rail_badge` and the radio handler. ✓
