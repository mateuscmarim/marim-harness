"""Settings modal: edit runtime settings live (mode, model, theme, MCP) and the
env-backed toggles with an explicit save to the global .env.

Runtime widgets apply immediately by calling the same harness mutations the
slash commands use. The env block (LSP, LSP tools, job-tool mode, context
budget, proactive memory) is written to the global .env only when "Save to .env"
is pressed and takes effect on the next launch — those settings are consumed at
Harness construction and cannot be safely re-registered mid-session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Static, Switch

from ...config import ModelConfig, global_config_path, save_env_settings
from ...permissions import Mode
from .model_picker import ModelPickerModal
from .themes import THEME_NAMES

if TYPE_CHECKING:
    from ...agent import Harness

_MODES = ("ask", "auto", "plan")


def _b(value: bool) -> str:
    return "1" if value else "0"


class SettingsModal(ModalScreen[None]):
    """A scrollable settings overlay. Dismisses with None."""

    CSS = """
    SettingsModal {
        align: center middle;
    }
    #settings-box {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #settings-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #settings-scroll {
        height: auto;
        max-height: 30;
    }
    .section {
        text-style: bold;
        color: $accent;
        margin-top: 1;
    }
    .muted {
        color: $text-muted;
    }
    .row {
        height: auto;
        margin-bottom: 1;
    }
    .row Label {
        width: 24;
    }
    #save-env {
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self, *, harness: Harness, current_theme: str, env_cfg: ModelConfig
    ) -> None:
        super().__init__()
        self.harness = harness
        self.current_theme = current_theme
        self.env_cfg = env_cfg
        self._mcp_names: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-box"):
            yield Static("Settings", id="settings-title")
            with VerticalScroll(id="settings-scroll"):
                # --- Runtime (applies immediately) ---
                yield Static("Runtime — applies immediately", classes="section")

                yield Label("Mode")
                with RadioSet(id="mode-set"):
                    for name in _MODES:
                        yield RadioButton(
                            name,
                            value=(name == self.harness.deps.mode.value),
                            id=f"mode-{name}",
                        )

                with Horizontal(classes="row"):
                    yield Static(f"Model: {self.harness.model_label}", id="model-label")
                    yield Button("change", id="model-change", variant="primary")

                yield Label("Theme")
                with RadioSet(id="theme-set"):
                    for i, name in enumerate(THEME_NAMES):
                        yield RadioButton(
                            name, value=(name == self.current_theme), id=f"theme-{i}"
                        )

                yield Static("MCP servers", classes="section")
                self._mcp_names = list(self.harness.mcp.configured_names())
                if not self._mcp_names:
                    yield Static("No MCP servers configured.", classes="muted")
                else:
                    for i, name in enumerate(self._mcp_names):
                        with Horizontal(classes="row"):
                            yield Static(self._mcp_state(name), id=f"mcp-state-{i}")
                            label = (
                                "enable"
                                if name in self.harness.mcp.disabled
                                else "disable"
                            )
                            yield Button(label, id=f"mcp-btn-{i}")

                # --- Config (saved to .env, next launch) ---
                yield Static(
                    "Config — saved to .env (applies on next launch)",
                    classes="section",
                )
                with Horizontal(classes="row"):
                    yield Label("LSP")
                    yield Switch(value=self.env_cfg.lsp_enabled, id="sw-lsp")
                with Horizontal(classes="row"):
                    yield Label("LSP navigation tools")
                    yield Switch(value=self.env_cfg.lsp_tools_enabled, id="sw-lsp-tools")
                with Horizontal(classes="row"):
                    yield Label("Job tool combined")
                    yield Switch(value=self.env_cfg.job_tool_combined, id="sw-job")
                with Horizontal(classes="row"):
                    yield Label("Proactive memory")
                    yield Switch(value=self.env_cfg.proactive_memory, id="sw-mem")
                with Horizontal(classes="row"):
                    yield Label("Context budget (tokens)")
                    yield Input(
                        value=str(self.env_cfg.max_context_tokens),
                        id="ctx-input",
                        type="integer",
                    )
                with Horizontal(classes="row"):
                    yield Label("Desktop notifications")
                    yield Switch(
                        value=self.env_cfg.notifications_enabled, id="sw-notifications"
                    )
                with Horizontal(classes="row"):
                    yield Label("Notification events")
                    yield Input(
                        value=", ".join(sorted(self.env_cfg.notification_events)),
                        id="notif-events-input",
                    )
                yield Button("Save to .env", id="save-env", variant="success")
                yield Static("", id="save-status")
                yield Static(
                    "⚠ Config changes apply on next launch.", classes="muted"
                )

                # --- Read-only ---
                yield Static(
                    "Read-only (edit the .env file directly)", classes="section"
                )
                deny = ", ".join(self.env_cfg.command_denylist) or "(none)"
                allow = ", ".join(self.env_cfg.command_allowlist) or "(none)"
                trust = "on" if self.env_cfg.trust_project_hooks else "off"
                yield Static(f"Command denylist: {deny}", classes="muted")
                yield Static(f"Command allowlist: {allow}", classes="muted")
                yield Static(f"Trust project hooks: {trust}", classes="muted")
                yield Static(f"Config file: {global_config_path()}", classes="muted")

    def _mcp_state(self, name: str) -> str:
        status = self.harness.mcp.mcp_status
        connected = set(status.get("connected", []))
        failed = dict(status.get("failed", []))
        if name in self.harness.mcp.disabled:
            return f"{name} — disabled"
        if name in connected:
            return f"{name} — connected"
        if name in failed:
            return f"{name} — failed: {failed[name]}"
        return f"{name} — not connected"

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.index is None:
            return
        if event.radio_set.id == "mode-set":
            self.harness.deps.mode = Mode(_MODES[event.index])
            self.app.status.refresh_status()  # type: ignore[attr-defined]
        elif event.radio_set.id == "theme-set":
            self.app.theme = THEME_NAMES[event.index]  # type: ignore[attr-defined]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "model-change":
            self._open_model_picker()
        elif bid == "save-env":
            self._save_env()
        elif bid.startswith("mcp-btn-"):
            index = int(bid.removeprefix("mcp-btn-"))
            self.run_worker(self._toggle_mcp(index))

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

    async def _toggle_mcp(self, index: int) -> None:
        name = self._mcp_names[index]
        mcp = self.harness.mcp
        state = self.query_one(f"#mcp-state-{index}", Static)
        if name in mcp.disabled:
            err = await self.harness.enable_server(name)
            if err:
                state.update(f"{name} — enable failed: {err}")
            else:
                state.update(self._mcp_state(name))
        else:
            await self.harness.disable_server(name)
            state.update(self._mcp_state(name))
        btn = self.query_one(f"#mcp-btn-{index}", Button)
        btn.label = "enable" if name in mcp.disabled else "disable"
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
            "MARIM_LSP": _b(self.query_one("#sw-lsp", Switch).value),
            "MARIM_LSP_TOOLS": _b(self.query_one("#sw-lsp-tools", Switch).value),
            "MARIM_JOB_TOOL_COMBINED": _b(self.query_one("#sw-job", Switch).value),
            "MARIM_PROACTIVE_MEMORY": _b(self.query_one("#sw-mem", Switch).value),
            "MARIM_MAX_CONTEXT_TOKENS": str(ctx),
            "MARIM_NOTIFICATIONS": _b(self.query_one("#sw-notifications", Switch).value),
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

    def action_cancel(self) -> None:
        self.dismiss(None)
