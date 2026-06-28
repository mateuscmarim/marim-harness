"""The full-bleed settings screen: edit runtime settings live (mode, model, theme,
MCP) and the env-backed toggles with an explicit save to the global .env.

Runtime widgets apply immediately by calling the same harness mutations the slash
commands use. The env block (LSP, LSP tools, job-tool mode, context budget,
proactive memory) is written to the global .env only when "Save to .env" is pressed
and takes effect on the next launch — those settings are consumed at Harness
construction and cannot be safely re-registered mid-session."""

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

# The full-bleed settings screen's rail sections, in order: (key, label).
_SECTIONS = (
    ("runtime", "Runtime"),
    ("theme", "Theme"),
    ("mcp", "MCP servers"),
    ("config", "Config"),
)
_SETTINGS_HINTS = "↑↓ section · enter edit · esc close"

# Each theme's accent hex, for the colored dot in the Theme section + the rail badge.
_ACCENTS = {t.name: str(t.primary) for t in MARIM_THEMES}


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
    #settings-hints { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
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
        yield Static(_SETTINGS_HINTS, id="settings-hints")

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
        yield Static("Saved to .env — applies on next launch.", classes="muted")
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
            "Desktop notifications",
            value=self.env_cfg.notifications.enabled,
            id="sw-notifications",
        )
        with Horizontal(classes="frow"):
            yield Label("Context budget (tokens)")
            yield Input(
                value=str(self.env_cfg.max_context_tokens),
                id="ctx-input",
                type="integer",
            )
        with Horizontal(classes="frow"):
            yield Label("Notification events")
            yield Input(
                value=", ".join(sorted(self.env_cfg.notifications.events)),
                id="notif-events-input",
            )
        yield Button("Save to .env", id="save-env", variant="success")
        yield Static("", id="save-status")
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
        if event.index is None or event.radio_set.id != "mode-set":
            return
        self.harness.set_mode(Mode(_MODES[event.index]))
        self.query_one("#badge-runtime", Static).update(self.harness.deps.workspace.mode.value)
        self.app.status.refresh_status()  # type: ignore[attr-defined]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "model-change":
            self._open_model_picker()
        elif bid == "save-env":
            self._save_env()

    async def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        if cid.startswith("mcp-toggle-"):
            await self._toggle_mcp(int(cid.removeprefix("mcp-toggle-")), event.value)

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

    def _save_env(self) -> None:
        status = self.query_one("#save-status", Static)
        raw = self.query_one("#ctx-input", Input).value.strip()
        try:
            ctx = int(raw)
        except ValueError:
            status.update("Context budget must be a positive integer.")
            return
        if ctx <= 0:
            status.update("Context budget must be a positive integer.")
            return
        values = {
            "MARIM_LSP": _b(self.query_one("#sw-lsp", BoxCheckbox).value),
            "MARIM_LSP_TOOLS": _b(self.query_one("#sw-lsp-tools", BoxCheckbox).value),
            "MARIM_JOB_TOOL_COMBINED": _b(self.query_one("#sw-job", BoxCheckbox).value),
            "MARIM_PROACTIVE_MEMORY": _b(self.query_one("#sw-mem", BoxCheckbox).value),
            "MARIM_MAX_CONTEXT_TOKENS": str(ctx),
            "MARIM_NOTIFICATIONS": _b(
                self.query_one("#sw-notifications", BoxCheckbox).value
            ),
            "MARIM_NOTIFICATION_EVENTS": self.query_one(
                "#notif-events-input", Input
            ).value.strip(),
        }
        try:
            path = save_env_settings(values)
        except Exception as exc:  # surface any write failure on the status line
            status.update(f"Save failed: {exc}")
            return
        status.update(f"Saved to {path} — applies on next launch.")

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
