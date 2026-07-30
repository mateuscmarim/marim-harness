"""The full-bleed settings screen: topic pages on a left rail (Session,
Providers, Theme, MCP servers, Context & Memory, Tools, Notifications,
Advanced). Live settings (mode, model, theme, MCP, provider credentials,
dynamic workflows) apply immediately; env-backed settings auto-save per field.

This module is the screen's *behaviour*: layout, navigation, and what each
widget's change event does. The two halves it delegates to are
``settings_sections`` (the widget tree of each topic page) and ``settings_env``
(which widget id maps to which env var, and the single save funnel).

Live widgets apply immediately by calling the same harness mutations the slash
commands use. The env block (LSP, LSP tools, job-tool mode, context budget,
proactive memory, ...) is written to the global .env as soon as a field changes
(checkbox/radio on change, text/integer input on Enter or blur) and takes effect on
the next launch — those settings are consumed at Harness construction and cannot be
safely re-registered mid-session. The `_ready` flag suppresses the Changed events
that fire while widgets mount with their initial values."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, RadioSet, Static

from ...config import ModelConfig, MultiModelSource
from ...runtime.permissions import Mode
from ...subagents.cli_backend import resolve_cli_binary
from .model_picker import ModelPickerModal
from .providers import ProvidersPane, current_default_provider
from .settings_env import (
    ENV_CHECKBOXES,
    ENV_INT_INPUTS,
    ENV_RADIOS,
    ENV_TEXT_INPUTS,
    MODES,
    TIER_ENV,
    EnvAutoSave,
    env_flag,
)
from .settings_sections import (
    advanced_widgets,
    advisor_value_text,
    context_widgets,
    mcp_status_word,
    mcp_widgets,
    notifications_widgets,
    session_widgets,
    short_theme,
    theme_widgets,
    thinking_value_text,
    tier_value_text,
    tools_widgets,
)
from .themes import THEME_NAMES
from .thinking_picker import ThinkingPickerModal
from .widgets.box_checkbox import BoxCheckbox

if TYPE_CHECKING:
    from ...runtime.harness import Harness

# The full-bleed settings screen's rail sections, in order: (key, label).
_SECTIONS = (
    ("session", "Session"),
    ("providers", "Providers"),
    ("theme", "Theme"),
    ("mcp", "MCP servers"),
    ("context", "Context & Memory"),
    ("tools", "Tools"),
    ("notifications", "Notifications"),
    ("advanced", "Advanced"),
)
_SETTINGS_HINTS = "↑↓ section · enter edit · esc back/close · changes save automatically"


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
    .tier-row-label { width: 12; }
    .tier-row-value { width: 1fr; color: $text-muted; }
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
        Binding("enter", "edit_section", "Edit section", show=False),
    ]

    # Rail-first keyboard model: the screen opens with NOTHING focused (the
    # empty string suppresses App.AUTO_FOCUS="*", which would silently focus
    # the first Input and swallow the ↑↓ rail navigation). enter hands focus
    # to the active section's first field; escape hands it back to the rail.
    AUTO_FOCUS = ""

    active_section: reactive[str] = reactive("session")

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
        # Every .env write goes through here, so a failed save is reported on the
        # footer status line from one place instead of six.
        self._env = EnvAutoSave(self._status)
        # Gate auto-save until the initial widget tree has mounted: setting widget
        # values during compose fires Changed events we must not persist.
        self._ready = False

    def compose(self) -> ComposeResult:
        # Snapshot the configured servers before building: both the MCP section's
        # toggles and the rail badge index into this list by position, and
        # `_toggle_mcp` resolves a checkbox id back to a name through it.
        self._mcp_names = list(self.harness.mcp.configured_names())
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
                with Vertical(id="section-session"):
                    yield from session_widgets(
                        self.harness,
                        self.env_cfg,
                        autonomous_wake=getattr(
                            self.app, "autonomous_wake", self.harness.autonomous_wake
                        ),
                    )
                yield ProvidersPane(
                    model_source=self.harness.model_source,
                    status=self._status,
                    set_badge=self._set_providers_badge,
                    cli_detected=resolve_cli_binary() is not None,
                    id="section-providers",
                )
                with Vertical(id="section-theme"):
                    yield from theme_widgets()
                with Vertical(id="section-mcp"):
                    yield from mcp_widgets(self.harness.mcp, self._mcp_names)
                with Vertical(id="section-context"):
                    yield from context_widgets(self.env_cfg)
                with Vertical(id="section-tools"):
                    yield from tools_widgets(self.env_cfg)
                with Vertical(id="section-notifications"):
                    yield from notifications_widgets(self.env_cfg)
                with Vertical(id="section-advanced"):
                    yield from advanced_widgets(self.harness, self.env_cfg)
        with Horizontal(id="settings-footer"):
            yield Static(_SETTINGS_HINTS, id="settings-hints")
            yield Static("", id="settings-status")

    def _rail_badge(self, key: str) -> str:
        """The current value shown to the right of a rail row (mode / theme / count)."""
        if key == "session":
            return self.harness.deps.workspace.mode.value
        if key == "providers":
            return current_default_provider()
        if key == "theme":
            return short_theme(self.current_theme)
        if key == "mcp":
            return str(len(self._mcp_names))
        return ""

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
        # ↑↓ navigate the rail only while nothing is focused; a focused field
        # is "edit mode" and stray arrows must not yank the section away
        # mid-edit (widgets that use arrows themselves, like RadioSet, consume
        # them before this binding fires — this guards the ones that don't).
        if self.focused is not None:
            return
        self._move_section(1)

    def action_prev_section(self) -> None:
        if self.focused is not None:
            return
        self._move_section(-1)

    def action_edit_section(self) -> None:
        """enter: move focus from the rail into the active section's first
        focusable field. Buttons are skipped — enter lands on a field to edit,
        not on a button the *same keypress* would immediately press (the
        remove button sits first in a provider card's DOM order)."""
        section = self.query_one(f"#section-{self.active_section}")
        for widget in section.query("*"):
            if widget.focusable and not isinstance(widget, Button):
                widget.focus()
                return

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
        self.query_one("#badge-theme", Static).update(short_theme(name))

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.index is None:
            return
        rid = event.radio_set.id or ""
        if rid == "mode-set":
            self.harness.set_mode(Mode(MODES[event.index]))
            self.query_one("#badge-session", Static).update(
                self.harness.deps.workspace.mode.value
            )
            self.app.status.refresh_status()  # type: ignore[attr-defined]
            return
        if not self._ready:
            return
        spec = ENV_RADIOS.get(rid)
        if spec is not None:
            env_key, choices = spec
            self._env.commit(env_key, choices[event.index])

    def _tier_value_text(self, tier: str) -> str:
        return tier_value_text(self.env_cfg, tier)

    def _advisor_value_text(self) -> str:
        return advisor_value_text(self.env_cfg)

    def _thinking_value_text(self) -> str:
        return thinking_value_text(self.env_cfg)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "model-change":
            self._open_model_picker()
        elif bid.startswith("tier-change-"):
            self._open_tier_picker(bid.removeprefix("tier-change-"))
        elif bid == "advisor-change":
            self._open_advisor_picker()
        elif bid == "thinking-change":
            self._open_thinking_picker()

    async def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        if cid.startswith("mcp-toggle-"):
            await self._toggle_mcp(int(cid.removeprefix("mcp-toggle-")), event.value)
            return
        if cid == "sw-autonomous-wake":
            # Live, session-only toggle — mirrors `/jobs wake on|off`. The App reads
            # this flag per wake decision; nothing is persisted to .env.
            self.app.autonomous_wake = event.value  # type: ignore[attr-defined]
            self.app.status.refresh_status()  # type: ignore[attr-defined]
            return
        if not self._ready:
            return
        if cid == "sw-workflows":
            self._toggle_workflows(event.value)
            return
        if cid == "sw-tiering":
            self._toggle_tiering(event.value)
            return
        env_key = ENV_CHECKBOXES.get(cid)
        if env_key is not None:
            self._env.commit(env_key, env_flag(event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._commit_input(event.input.id or "")

    def on_input_blurred(self, event: Input.Blurred) -> None:
        if self._ready:
            self._commit_input(event.input.id or "")

    def _commit_input(self, widget_id: str) -> None:
        if not self._ready:
            return
        if widget_id in ENV_INT_INPUTS:
            self._env.commit_int(widget_id, self.query_one(f"#{widget_id}", Input).value)
        elif widget_id in ENV_TEXT_INPUTS:
            self._env.commit(
                ENV_TEXT_INPUTS[widget_id],
                self.query_one(f"#{widget_id}", Input).value.strip(),
            )

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
        state.update(mcp_status_word(self.harness.mcp, name))
        self.app.status.refresh_status()  # type: ignore[attr-defined]

    def _status(self, msg: str) -> None:
        self.query_one("#settings-status", Static).update(msg)

    def _set_providers_badge(self, provider: str) -> None:
        self.query_one("#badge-providers", Static).update(provider)

    def _toggle_workflows(self, enabled: bool) -> None:
        """Persist MARIM_WORKFLOWS and flip the harness's live run_workflow seam
        in the same gesture. Disabling always takes effect at once; enabling is
        live only when an engine was built at launch — otherwise (workflows off
        at launch, or pydantic-monty missing) the harness reports False and the
        status line falls back to the usual next-launch promise."""
        if not self._env.save({"MARIM_WORKFLOWS": env_flag(enabled)}):
            return
        applied = self.harness.set_workflows_enabled(enabled)
        suffix = "applied" if applied else "applies next launch"
        self._status(f"✓ saved MARIM_WORKFLOWS · {suffix}")

    def _toggle_tiering(self, enabled: bool) -> None:
        """Persist MARIM_SUBAGENT_TIERING and flip the harness's live tier set in
        the same gesture — new spawns pick up the change without a relaunch. The
        curated per-tier slugs are left untouched, so re-enabling restores routing
        without re-entry."""
        if not self._env.save({"MARIM_SUBAGENT_TIERING": env_flag(enabled)}):
            return
        self.env_cfg.subagent.tiers = replace(self.env_cfg.subagent.tiers, enabled=enabled)
        self.harness.set_subagent_tiering_enabled(enabled)
        state = "on" if enabled else "off"
        self._status(f"✓ saved MARIM_SUBAGENT_TIERING · tiering {state} · applied")

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

    def _open_tier_picker(self, tier: str) -> None:
        """Open the model picker for one sub-agent tier row, current-seeded
        from the in-memory ``env_cfg`` (kept in sync with .env by
        ``_on_tier_chosen`` below, so a second pick without leaving the screen
        still shows the last-saved value as current). Mirrors
        ``_open_model_picker`` exactly, except the choice is persisted to .env
        (like a Providers-pane credential) rather than applied to the live
        session model — a sub-agent tier has no "current session" model to
        swap."""
        source = self.harness.model_source
        if source is None:
            self.query_one(f"#tier-value-{tier}", Static).update(
                "Model switching isn't available here."
            )
            return
        self.app.push_screen(
            ModelPickerModal(
                current=self.env_cfg.subagent.tiers.model_for(tier),
                fetch=source.list_models,
                is_local=source.is_local,
            ),
            lambda chosen, tier=tier: self._on_tier_chosen(tier, chosen),
        )

    def _on_tier_chosen(self, tier: str, chosen: str | None) -> None:
        """Persist the chosen model for one tier and refresh the live model
        catalog, same as a Providers credential save. Note the asymmetry with
        ``_on_model_chosen``: that path calls ``harness.set_model`` because
        there is one running session model to swap; a sub-agent tier has no
        such live seam — ``SubagentRunner._tiers`` was captured once at build
        time, so this save takes effect for sub-agents spawned after the next
        harness rebuild/session relaunch, not those already in flight (a live
        ``harness.set_subagent_tiers()`` setter is a deferred follow-up)."""
        if chosen is None:
            return
        env_key = TIER_ENV[tier]
        if not self._env.save({env_key: chosen}):
            return
        self._refresh_model_catalog()
        self.env_cfg.subagent.tiers = replace(self.env_cfg.subagent.tiers, **{tier: chosen})
        self.query_one(f"#tier-value-{tier}", Static).update(chosen)
        self._status(f"✓ saved {env_key} · applies to new sessions")

    def _open_advisor_picker(self) -> None:
        """Model picker for the global advisor default. Mirrors the tier rows:
        the pick persists to .env (new sessions); the live per-session switch
        is /advisor. Typing ``off`` in the picker clears the default."""
        source = self.harness.model_source
        if source is None:
            self.query_one("#advisor-value", Static).update(
                "Model switching isn't available here."
            )
            return
        self.app.push_screen(
            ModelPickerModal(
                current=self.env_cfg.advisor_model,
                fetch=source.list_models,
                is_local=source.is_local,
            ),
            self._on_advisor_chosen,
        )

    def _on_advisor_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        if chosen.strip().lower() == "off":
            # An explicit off DROPS the var rather than writing a
            # sentinel: unset is the env layer's own "no advisor", and a
            # written "off" would round-trip as a bogus model slug.
            if not self._env.save({}, drop=("MARIM_ADVISOR_MODEL",)):
                return
            self.env_cfg.advisor_model = None
        else:
            if not self._env.save({"MARIM_ADVISOR_MODEL": chosen}):
                return
            self.env_cfg.advisor_model = chosen
        self._refresh_model_catalog()
        self.query_one("#advisor-value", Static).update(self._advisor_value_text())
        self._status("✓ saved MARIM_ADVISOR_MODEL · applies to new sessions")

    def _open_thinking_picker(self) -> None:
        """Fixed-list picker for the global thinking default. The pick persists
        to .env (new sessions); the live per-session switch is /think."""
        self.app.push_screen(
            ThinkingPickerModal(current=self.env_cfg.thinking_level),
            self._on_thinking_chosen,
        )

    def _on_thinking_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        if chosen == "off":
            # off DROPS the var rather than writing a sentinel: unset is the
            # env layer's own "no thinking", and a written "off" round-trips
            # to the same None anyway (parse_thinking_level("off") == "off",
            # but the .env default should read as absent).
            if not self._env.save({}, drop=("MARIM_THINKING",)):
                return
            self.env_cfg.thinking_level = None
        else:
            if not self._env.save({"MARIM_THINKING": chosen}):
                return
            self.env_cfg.thinking_level = chosen
        self.query_one("#thinking-value", Static).update(self._thinking_value_text())
        self._status("✓ saved MARIM_THINKING · applies to new sessions")

    def _refresh_model_catalog(self) -> None:
        """Re-read per-provider credentials into the live catalog after a
        model-ish .env save, so the next picker open lists whatever the new
        value unlocked. Only MultiModelSource has that seam."""
        source = self.harness.model_source
        if isinstance(source, MultiModelSource):
            source.refresh_from_env()

    def action_cancel(self) -> None:
        # Two-stage escape mirroring enter: leave edit mode (back to the
        # rail) first, close the screen only when already at the rail.
        if self.focused is not None:
            # Escape reads as *cancel*, but the unfocus below fires Blurred
            # and password fields commit on blur — which would persist a
            # half-typed API key to the global .env. Discard the secret
            # first (an empty commit is a no-op); non-secret fields keep
            # the screen's save-on-blur model, same as clicking away.
            if isinstance(self.focused, Input) and self.focused.password:
                self.focused.value = ""
            self.set_focus(None)
            return
        self.dismiss(None)
