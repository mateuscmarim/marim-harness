"""The full-bleed settings screen: edit runtime settings live (mode, model, theme,
MCP) and the env-backed toggles, which auto-save per field.

Runtime widgets apply immediately by calling the same harness mutations the slash
commands use. The env block (LSP, LSP tools, job-tool mode, context budget,
proactive memory, ...) is written to the global .env as soon as a field changes
(checkbox/radio on change, text/integer input on Enter or blur) and takes effect on
the next launch — those settings are consumed at Harness construction and cannot be
safely re-registered mid-session. The `_ready` flag suppresses the Changed events
that fire while widgets mount with their initial values."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
)

from ...config import ModelConfig, global_config_path, save_env_settings
from ...runtime.permissions import Mode
from .model_picker import ModelPickerModal
from .themes import MARIM_THEMES, THEME_NAMES

if TYPE_CHECKING:
    from ...runtime.harness import Harness

_MODES = ("ask", "auto", "plan")
_TOOL_SEARCH_MODES = ("off", "auto", "on")

# The full-bleed settings screen's rail sections, in order: (key, label).
_SECTIONS = (
    ("runtime", "Runtime"),
    ("theme", "Theme"),
    ("mcp", "MCP servers"),
    ("config", "Config"),
)
_SETTINGS_HINTS = "↑↓ section · enter edit · changes save automatically · esc close"

# Each theme's accent hex, for the colored dot in the Theme section + the rail badge.
_ACCENTS = {t.name: str(t.primary) for t in MARIM_THEMES}

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


def _short_theme(name: str) -> str:
    """``marim-teal`` -> ``teal`` for the compact rail badge."""
    return name.split("-")[-1]


class BoxCheckbox(Checkbox):
    """A terminal-native ``[x]`` / ``[ ]`` checkbox. Textual's Checkbox draws a
    ``▐X▌`` block and signals on/off by colour alone; we render literal brackets with
    a blank inner glyph when off so it reads like a TUI checkbox, not an iOS slider.
    Brackets take the muted colour and the check takes the success colour."""

    @property
    def _button(self) -> Content:
        tv = self.app.theme_variables
        bracket = tv.get("text-muted", "#7c828d")
        inner = "x" if self.value else " "
        icol = tv.get("success", "#5fae7e") if self.value else bracket
        return Content.assemble(("[", bracket), (inner, icol), ("]", bracket))


def _b(value: bool) -> str:
    return "1" if value else "0"


class SettingsScreen(Screen[None]):
    """The full-bleed settings screen. Mirrors the sub-agents view layout: a header
    breadcrumb, a left section rail divided by a full-height border, a content pane,
    and a docked footer hint bar. Sections are mounted once and shown/hidden by
    ``display`` so widget state and ids survive a section switch. The rail shows the
    active section's current value as a badge and a ``›`` caret on the active row;
    ``↑/↓`` or a click switch sections."""

    CSS = """
    SettingsScreen { background: $surface; }
    #settings-header { height: 1; padding: 0 1; background: $panel; }
    #settings-body { height: 1fr; }
    #settings-rail { width: 26; height: 1fr; border-right: solid $panel; }
    .rail-row { height: 1; layout: horizontal; }
    .rail-caret { width: 2; color: $accent; }
    .rail-label { width: 1fr; color: $text-muted; }
    .rail-badge { width: auto; color: $text-muted; }
    .rail-row.-active { background: $boost; }
    .rail-row.-active .rail-label { color: $accent; text-style: bold; }
    #settings-content { width: 1fr; height: 1fr; padding: 1 2; }
    BoxCheckbox { border: none; padding: 0; height: 1; background: transparent; }
    BoxCheckbox:focus { background: $boost; }
    .theme-row { height: 1; layout: horizontal; }
    .theme-name { width: 1fr; }
    .theme-active { width: auto; color: $success; }
    .theme-row.-active .theme-name { text-style: bold; }
    .mcp-head { height: 1; color: $text-muted; }
    .mcp-row { height: 1; }
    .mcp-name { width: 28; }
    .mcp-status { width: 16; }
    .mcp-on { width: 5; }
    .srow { width: 1fr; height: 1; }
    .srow Static { width: auto; }
    .srow Button { width: auto; height: 1; border: none; padding: 0 1; margin-left: 2; }
    .frow { width: 1fr; height: 3; }
    .frow Label { width: 24; height: 3; content-align: left middle; }
    .frow Input { width: 1fr; }
    #settings-footer { height: 1; background: $panel; }
    #settings-hints { padding: 0 1; color: $text-muted; width: auto; }
    #settings-status { width: 1fr; color: $text-muted; content-align: right middle; padding: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("down", "next_section", "Next section", show=False),
        Binding("up", "prev_section", "Prev section", show=False),
    ]

    active_section: reactive[str] = reactive("runtime")

    def __init__(
        self, *, harness: Harness, current_theme: str, env_cfg: ModelConfig
    ) -> None:
        super().__init__()
        self.harness = harness
        self.current_theme = current_theme
        self.env_cfg = env_cfg
        self._mcp_names: list[str] = []
        # Rail row id -> section key, for click-to-select.
        self._rail_ids = {f"rail-{k}": k for k, _ in _SECTIONS}
        # Gate auto-save until the initial widget tree has mounted: setting widget
        # values during compose fires Changed events we must not persist.
        self._ready = False

    def compose(self) -> ComposeResult:
        yield Static(id="settings-header")
        with Horizontal(id="settings-body"):
            with Vertical(id="settings-rail"):
                for key, label in _SECTIONS:
                    with Horizontal(id=f"rail-{key}", classes="rail-row"):
                        yield Static("", id=f"caret-{key}", classes="rail-caret")
                        yield Static(label, classes="rail-label")
                        yield Static(
                            self._rail_badge(key),
                            id=f"badge-{key}",
                            classes="rail-badge",
                        )
            with VerticalScroll(id="settings-content"):
                with Vertical(id="section-runtime"):
                    yield from self._runtime_widgets()
                with Vertical(id="section-theme"):
                    yield from self._theme_widgets()
                with Vertical(id="section-mcp"):
                    yield from self._mcp_widgets()
                with Vertical(id="section-config"):
                    yield from self._config_widgets()
        with Horizontal(id="settings-footer"):
            yield Static(_SETTINGS_HINTS, id="settings-hints")
            yield Static("", id="settings-status")

    def _rail_badge(self, key: str) -> str:
        """The current value shown to the right of a rail row (mode / theme / count)."""
        if key == "runtime":
            return self.harness.deps.workspace.mode.value
        if key == "theme":
            return _short_theme(self.current_theme)
        if key == "mcp":
            return str(len(list(self.harness.mcp.configured_names())))
        return ""

    def _runtime_widgets(self) -> ComposeResult:
        yield Static("Changes apply immediately to the active session.", classes="muted")
        yield Label("Mode")
        with RadioSet(id="mode-set"):
            for name in _MODES:
                yield RadioButton(
                    name,
                    value=(name == self.harness.deps.workspace.mode.value),
                    id=f"mode-{name}",
                )
        with Horizontal(classes="srow"):
            yield Static(f"Model: {self.harness.model_label}", id="model-label")
            yield Button("change", id="model-change", variant="primary")
        yield Static(
            f"Context budget: {self.env_cfg.max_context_tokens:,} tokens",
            classes="muted",
        )

    def _theme_widgets(self) -> ComposeResult:
        yield Static("Accent palette for the harness UI.", classes="muted")
        for i, name in enumerate(THEME_NAMES):
            with Horizontal(id=f"theme-{i}", classes="theme-row"):
                yield Static(
                    Content.assemble(("● ", _ACCENTS[name]), name),
                    classes="theme-name",
                )
                yield Static("", id=f"theme-active-{i}", classes="theme-active")

    def _mcp_widgets(self) -> ComposeResult:
        self._mcp_names = list(self.harness.mcp.configured_names())
        if not self._mcp_names:
            yield Static("No MCP servers configured.", classes="muted")
            return
        with Horizontal(classes="mcp-head"):
            yield Static("SERVER", classes="mcp-name")
            yield Static("STATUS", classes="mcp-status")
            yield Static("ON", classes="mcp-on")
        for i, name in enumerate(self._mcp_names):
            with Horizontal(classes="mcp-row"):
                yield Static(name, classes="mcp-name")
                yield Static(
                    self._mcp_status_word(name),
                    id=f"mcp-state-{i}",
                    classes="mcp-status",
                )
                yield BoxCheckbox(
                    value=(name not in self.harness.mcp.disabled),
                    id=f"mcp-toggle-{i}",
                    classes="mcp-on",
                )

    def _config_widgets(self) -> ComposeResult:
        yield Static("Changes save automatically — apply on next launch.", classes="muted")
        yield BoxCheckbox("LSP", value=self.env_cfg.lsp_enabled, id="sw-lsp")
        yield BoxCheckbox(
            "LSP navigation tools",
            value=self.env_cfg.lsp_tools_enabled,
            id="sw-lsp-tools",
        )
        yield BoxCheckbox(
            "Job tool combined", value=self.env_cfg.job_tool_combined, id="sw-job"
        )
        yield BoxCheckbox(
            "Proactive memory", value=self.env_cfg.proactive_memory, id="sw-mem"
        )
        yield BoxCheckbox(
            "Mask stale observations at compaction",
            value=self.env_cfg.mask_observations,
            id="sw-mask-obs",
        )
        yield BoxCheckbox(
            "Desktop notifications",
            value=self.env_cfg.notifications.enabled,
            id="sw-notifications",
        )
        yield Label("Default mode (new sessions)")
        with RadioSet(id="default-mode-set"):
            for name in _MODES:
                yield RadioButton(
                    name,
                    value=(name == self.env_cfg.default_mode),
                    id=f"defmode-{name}",
                )
        yield Label("Tool search (MCP/plugin tools)")
        with RadioSet(id="toolsearch-set"):
            for name in _TOOL_SEARCH_MODES:
                yield RadioButton(
                    name,
                    value=(name == self.env_cfg.tool_search),
                    id=f"toolsearch-{name}",
                )
        with Horizontal(classes="frow"):
            yield Label("Tool-search threshold")
            yield Input(
                value=str(self.env_cfg.tool_search_threshold),
                id="toolsearch-threshold",
                type="integer",
            )
        with Horizontal(classes="frow"):
            yield Label("Context budget (tokens)")
            yield Input(
                value=str(self.env_cfg.max_context_tokens),
                id="ctx-input",
                type="integer",
            )
        with Horizontal(classes="frow"):
            yield Label("Mask: keep recent returns")
            yield Input(
                value=str(self.env_cfg.mask_keep_recent),
                id="mask-keep-recent",
                type="integer",
            )
        with Horizontal(classes="frow"):
            yield Label("Mask: min chars to elide")
            yield Input(
                value=str(self.env_cfg.mask_min_chars),
                id="mask-min-chars",
                type="integer",
            )
        with Horizontal(classes="frow"):
            yield Label("Sub-agent request limit")
            yield Input(
                value=str(self.env_cfg.subagent.request_limit),
                id="subagent-req-limit",
                type="integer",
            )
        with Horizontal(classes="frow"):
            yield Label("Notification events")
            yield Input(
                value=", ".join(sorted(self.env_cfg.notifications.events)),
                id="notif-events-input",
            )
        deny = ", ".join(self.env_cfg.command_denylist) or "(none)"
        allow = ", ".join(self.env_cfg.command_allowlist) or "(none)"
        trust = "on" if self.env_cfg.trust_project_hooks else "off"
        yield Static(f"Command denylist: {deny}", classes="muted")
        yield Static(f"Command allowlist: {allow}", classes="muted")
        yield Static(f"Trust project hooks: {trust}", classes="muted")
        yield Static(f"Config file: {global_config_path()}", classes="muted")

    def on_mount(self) -> None:
        self._apply_section()
        self._paint_themes()
        self._ready = True

    def watch_active_section(self) -> None:
        if self.is_mounted:
            self._apply_section()

    def _apply_section(self) -> None:
        """Show the active section, hide the rest, and reflect it in the rail caret
        + breadcrumb. The rail row and section container share the section key."""
        for key, _label in _SECTIONS:
            active = key == self.active_section
            self.query_one(f"#section-{key}").display = active
            self.query_one(f"#rail-{key}").set_class(active, "-active")
            self.query_one(f"#caret-{key}", Static).update("›" if active else "")
        label = dict(_SECTIONS)[self.active_section]
        self.query_one("#settings-header", Static).update(f"settings  ›  {label}")

    def _paint_themes(self) -> None:
        """Mark the current theme's row with an ``active`` badge + bold name."""
        for i, name in enumerate(THEME_NAMES):
            is_current = name == self.current_theme
            self.query_one(f"#theme-active-{i}", Static).update(
                "active" if is_current else ""
            )
            self.query_one(f"#theme-{i}").set_class(is_current, "-active")

    def _move_section(self, delta: int) -> None:
        keys = [k for k, _ in _SECTIONS]
        i = keys.index(self.active_section)
        self.active_section = keys[max(0, min(i + delta, len(keys) - 1))]

    def action_next_section(self) -> None:
        self._move_section(1)

    def action_prev_section(self) -> None:
        self._move_section(-1)

    def on_click(self, event: events.Click) -> None:
        """Click a rail row to switch section, or a theme row to apply that theme."""
        node = event.widget
        while node is not None:
            wid = getattr(node, "id", None) or ""
            if wid in self._rail_ids:
                self.active_section = self._rail_ids[wid]
                return
            if wid.startswith("theme-") and wid.removeprefix("theme-").isdigit():
                self._apply_theme(int(wid.removeprefix("theme-")))
                return
            node = node.parent

    def _apply_theme(self, index: int) -> None:
        name = THEME_NAMES[index]
        self.current_theme = name
        self.app.theme = name  # type: ignore[attr-defined]
        self._paint_themes()
        self.query_one("#badge-theme", Static).update(_short_theme(name))

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if (event.button.id or "") == "model-change":
            self._open_model_picker()

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

    def _mcp_status_word(self, name: str) -> str:
        mcp = self.harness.mcp
        if name in mcp.disabled:
            return "disabled"
        if name in set(mcp.mcp_status.connected):
            return "connected"
        if name in dict(mcp.mcp_status.failed):
            return "failed"
        return "—"

    async def _toggle_mcp(self, index: int, want_on: bool) -> None:
        """Enable/disable a server to match the toggle. Reverts the checkbox (without
        re-firing Changed) if an enable fails. A no-op when already in the wanted
        state — guards the Changed that fires while the checkboxes mount."""
        name = self._mcp_names[index]
        if want_on == (name not in self.harness.mcp.disabled):
            return
        state = self.query_one(f"#mcp-state-{index}", Static)
        if want_on:
            err = await self.harness.enable_server(name)
            if err:
                with self.prevent(BoxCheckbox.Changed):
                    self.query_one(f"#mcp-toggle-{index}", BoxCheckbox).value = False
                state.update("failed")
                return
        else:
            await self.harness.disable_server(name)
        state.update(self._mcp_status_word(name))
        self.app.status.refresh_status()  # type: ignore[attr-defined]

    def _positive_int(self, selector: str) -> int | None:
        """Parse a positive integer from an Input, or None if blank/invalid/≤0.
        The caller turns None into a field-specific error on the status line."""
        raw = self.query_one(selector, Input).value.strip()
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value > 0 else None

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

    def _open_model_picker(self) -> None:
        source = self.harness.model_source
        if source is None:
            self.query_one("#model-label", Static).update(
                "Model switching isn't available here."
            )
            return
        self.app.push_screen(
            ModelPickerModal(
                current=self.harness.model_id,
                fetch=source.list_models,
                is_local=source.is_local,
            ),
            self._on_model_chosen,
        )

    def _on_model_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        self.harness.set_model(chosen)
        self.app.status.refresh_status()  # type: ignore[attr-defined]
        self.query_one("#model-label", Static).update(
            f"Model: {self.harness.model_label}"
        )

    def action_cancel(self) -> None:
        self.dismiss(None)
