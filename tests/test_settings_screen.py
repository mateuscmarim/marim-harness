"""Tests for the full-bleed SettingsScreen (replaces the centered SettingsModal).

The screen mirrors the sub-agents full-bleed layout: a header breadcrumb, a left
section rail (Session / Providers / Theme / MCP servers / Context & Memory /
Tools / Notifications / Advanced), a content pane, and a footer hint bar. Sections are
mounted once and shown/hidden by ``display`` so widget state and ids survive
section switches.
"""

import os

import pytest
from textual.app import App

from marim_harness.config import ModelConfig
from marim_harness.interfaces.tui.settings import SettingsScreen
from marim_harness.interfaces.tui.themes import MARIM_THEMES, THEME_NAMES
from marim_harness.runtime.deps import TrustState
from marim_harness.runtime.permissions import Mode


@pytest.fixture
def isolated_env():
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _fake_harness():
    from types import SimpleNamespace

    h = SimpleNamespace(
        deps=SimpleNamespace(
            workspace=SimpleNamespace(mode=Mode.auto), trust=TrustState()
        ),
        model_label="openrouter/x",
        model_id="x",
        model_source=None,  # disables the model-change picker path
        set_model=lambda mid: None,
        mcp=SimpleNamespace(
            configured_names=lambda: [],
            disabled=set(),
            mcp_status=SimpleNamespace(connected=[], failed={}),
        ),
        autonomous_wake=True,
    )
    h.set_mode = lambda mode: setattr(h.deps.workspace, "mode", mode)
    return h


def _mcp_harness():
    from types import SimpleNamespace

    h = _fake_harness()
    h.mcp.configured_names = lambda: ["agentmemory"]
    h.mcp.disabled = set()
    h.mcp.mcp_status = SimpleNamespace(connected=["agentmemory"], failed={})

    async def _disable(name):
        h.mcp.disabled.add(name)

    async def _enable(name):
        h.mcp.disabled.discard(name)
        return None

    h.disable_server = _disable
    h.enable_server = _enable
    return h


def _env_cfg():
    return ModelConfig(provider="openrouter", model="x")


class _Host(App):
    def __init__(self, harness, env_cfg):
        from types import SimpleNamespace

        super().__init__()
        self._harness = harness
        self._env_cfg = env_cfg
        self.status = SimpleNamespace(refresh_status=lambda: None)
        self.autonomous_wake = harness.autonomous_wake

    def on_mount(self) -> None:
        for theme in MARIM_THEMES:
            self.register_theme(theme)
        self.push_screen(
            SettingsScreen(
                harness=self._harness,
                current_theme=THEME_NAMES[1],
                env_cfg=self._env_cfg,
            )
        )


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
        # Providers: the pane mounts as its own section with all four cards.
        assert s.query_one("#section-providers #prov-card-openrouter") is not None
        assert s.query_one("#section-providers #prov-default-set") is not None


@pytest.mark.anyio
async def test_advanced_shows_live_trust_state():
    """The Advanced page's trust row reads the live deps.trust (Task 6),
    not the env/config knob — must reflect a store-sourced grant."""
    harness = _fake_harness()
    harness.deps.trust = TrustState(project=True, source="store")
    app = _Host(harness, _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.screen.active_section = "advanced"
        await pilot.pause()
        text = str(app.screen.query_one("#trust-status").render())
    assert "on" in text
    assert "store" in text


@pytest.mark.anyio
async def test_providers_rail_badge_shows_default(isolated_env, monkeypatch):
    monkeypatch.setenv("MARIM_PROVIDER", "google")
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        badge = str(app.screen.query_one("#badge-providers").render())
    assert badge == "google"


@pytest.mark.anyio
async def test_escape_dismisses():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.anyio
async def test_mode_radio_applies_live():
    """Selecting a mode in the Session section applies it to the harness at once."""
    harness = _fake_harness()
    app = _Host(harness, _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#mode-plan")
        await pilot.pause()
    assert harness.deps.workspace.mode == Mode.plan


@pytest.mark.anyio
async def test_autonomous_wake_toggle_applies_live():
    """The Session page's autonomous-wake checkbox reflects the app's live state
    and flips it immediately (session-only, like /jobs wake), without writing .env."""
    harness = _fake_harness()  # autonomous_wake defaults True
    app = _Host(harness, _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.screen.query_one("#sw-autonomous-wake").value is True
        assert app.autonomous_wake is True
        await pilot.click("#sw-autonomous-wake")  # toggle off
        await pilot.pause()
    assert app.autonomous_wake is False


@pytest.mark.anyio
async def test_mcp_toggle_disables_server():
    """Toggling a connected server in the MCP section disables it on the harness."""
    h = _mcp_harness()
    app = _Host(h, _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down", "down", "down")  # Session -> Providers -> Theme -> MCP
        await pilot.pause()
        await pilot.click("#mcp-toggle-0")  # turn the [x] toggle off
        await pilot.pause()
        await pilot.pause()
    assert "agentmemory" in h.mcp.disabled


@pytest.mark.anyio
async def test_theme_applies_live():
    """Selecting a theme in the Theme section applies it to the app immediately."""
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down")  # Session -> Theme
        await pilot.pause()
        await pilot.click("#theme-0")  # first theme
        await pilot.pause()
        assert app.theme == THEME_NAMES[0]


async def _goto_config(pilot):
    """Reach a relaunch page. Context & Memory is the 4th rail section."""
    pilot.app.screen.active_section = "context"
    await pilot.pause()


def _scroll_to(app, widget_id):
    """Bring a config widget into view before clicking (the section scrolls)."""
    scroll = app.screen.query_one("#settings-content")
    scroll.scroll_to_widget(app.screen.query_one(widget_id), animate=False)


@pytest.mark.anyio
async def test_open_does_not_write_env(isolated_env, monkeypatch, tmp_path):
    """Opening the screen must not write .env (mount-time Changed events are ignored)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
    assert not (tmp_path / "marim" / ".env").exists()


@pytest.mark.anyio
async def test_checkbox_autosaves(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())  # lsp_enabled defaults True
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        _scroll_to(app, "#sw-lsp")
        await pilot.click("#sw-lsp")  # toggle LSP off
        await pilot.pause()
    assert "MARIM_LSP=0" in (tmp_path / "marim" / ".env").read_text()


@pytest.mark.anyio
async def test_workflows_toggle_saves_env_and_applies_live(isolated_env, monkeypatch, tmp_path):
    """The Tools page's Dynamic-workflows checkbox persists MARIM_WORKFLOWS and
    flips the harness's live run_workflow seam in the same gesture (the tool
    checks the seam per call, so no relaunch is needed)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    harness = _fake_harness()
    calls = []

    def _set(enabled):
        calls.append(enabled)
        return True  # the engine exists — the flip took effect live

    harness.set_workflows_enabled = _set
    app = _Host(harness, _env_cfg())  # workflows_enabled defaults True
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        _scroll_to(app, "#sw-workflows")
        await pilot.click("#sw-workflows")  # toggle workflows off
        await pilot.pause()
        status = str(app.screen.query_one("#settings-status").render())
    assert "MARIM_WORKFLOWS=0" in (tmp_path / "marim" / ".env").read_text()
    assert calls == [False]
    assert "applied" in status
    assert "next launch" not in status


@pytest.mark.anyio
async def test_workflows_toggle_reports_next_launch_without_engine(
    isolated_env, monkeypatch, tmp_path
):
    """Enabling when the harness was built without an engine (workflows off at
    launch, or pydantic-monty missing) cannot take effect live — the status
    line must say so instead of pretending."""
    from marim_harness.config import ModelConfig

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    harness = _fake_harness()
    harness.set_workflows_enabled = lambda enabled: False  # no engine to restore
    env_cfg = ModelConfig(provider="openrouter", model="x", workflows_enabled=False)
    app = _Host(harness, env_cfg)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        # The checkbox mirrors the configured state, not a hardcoded default.
        assert app.screen.query_one("#sw-workflows").value is False
        _scroll_to(app, "#sw-workflows")
        await pilot.click("#sw-workflows")  # toggle workflows on
        await pilot.pause()
        status = str(app.screen.query_one("#settings-status").render())
    assert "MARIM_WORKFLOWS=1" in (tmp_path / "marim" / ".env").read_text()
    assert "next launch" in status


@pytest.mark.anyio
async def test_int_input_autosaves_on_submit(isolated_env, monkeypatch, tmp_path):
    from textual.widgets import Input

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        inp = app.screen.query_one("#subagent-req-limit", Input)
        inp.value = "120"
        app.screen._commit_input("subagent-req-limit")  # what Enter/blur trigger
        await pilot.pause()
    assert os.environ.get("MARIM_SUBAGENT_REQUEST_LIMIT") == "120"


@pytest.mark.anyio
async def test_int_input_real_submit_event_autosaves(isolated_env, monkeypatch, tmp_path):
    """End-to-end through the real Textual event: focus + type + Enter fires
    Input.Submitted -> on_input_submitted, not a direct _commit_input call."""
    from textual.widgets import Input

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        inp = app.screen.query_one("#subagent-req-limit", Input)
        inp.focus()
        await pilot.pause()
        inp.value = "150"
        await pilot.press("enter")
        await pilot.pause()
    assert os.environ.get("MARIM_SUBAGENT_REQUEST_LIMIT") == "150"


@pytest.mark.anyio
async def test_wake_depth_cap_reflects_config_and_saves(isolated_env, monkeypatch, tmp_path):
    """The Tools page's autonomous-wake-turns input shows the configured value
    (default 8) and persists MARIM_WAKE_DEPTH_CAP on commit."""
    from textual.widgets import Input

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MARIM_WAKE_DEPTH_CAP", raising=False)
    app = _Host(_fake_harness(), _env_cfg())  # subagent.wake_depth_cap defaults 8
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        inp = app.screen.query_one("#wake-depth-cap", Input)
        assert inp.value == "8"
        inp.value = "3"
        app.screen._commit_input("wake-depth-cap")
        await pilot.pause()
    assert os.environ.get("MARIM_WAKE_DEPTH_CAP") == "3"
    assert "MARIM_WAKE_DEPTH_CAP=3" in (tmp_path / "marim" / ".env").read_text()


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
async def test_ctx_budget_zero_accepted_and_saved_under_new_key(
    isolated_env, monkeypatch, tmp_path
):
    """The label says "0 = unbudgeted" — 0 must be accepted and persisted as
    MARIM_CONTEXT_BUDGET (never the deprecated MARIM_MAX_CONTEXT_TOKENS)."""
    from textual.widgets import Input

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await _goto_config(pilot)
        app.screen.query_one("#ctx-input", Input).value = "0"
        app.screen._commit_input("ctx-input")
        await pilot.pause()
    text = (tmp_path / "marim" / ".env").read_text()
    assert "MARIM_CONTEXT_BUDGET=0" in text
    assert "MARIM_MAX_CONTEXT_TOKENS" not in text
    assert os.environ.get("MARIM_CONTEXT_BUDGET") == "0"


@pytest.mark.anyio
async def test_ctx_budget_save_retires_deprecated_key(isolated_env, monkeypatch, tmp_path):
    """A save must remove any stale MARIM_MAX_CONTEXT_TOKENS line (and its
    os.environ mirror) so the deprecation nag can't fire against a line the
    app wrote itself — and so the old var can't shadow the new one."""
    from textual.widgets import Input

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    env_file = tmp_path / "marim" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("MARIM_MAX_CONTEXT_TOKENS=120000\nMARIM_MODEL=keep-me\n")
    monkeypatch.setenv("MARIM_MAX_CONTEXT_TOKENS", "120000")
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await _goto_config(pilot)
        app.screen.query_one("#ctx-input", Input).value = "90000"
        app.screen._commit_input("ctx-input")
        await pilot.pause()
    text = env_file.read_text()
    assert "MARIM_CONTEXT_BUDGET=90000" in text
    assert "MARIM_MAX_CONTEXT_TOKENS" not in text
    assert "MARIM_MODEL=keep-me" in text  # unrelated lines survive
    assert "MARIM_MAX_CONTEXT_TOKENS" not in os.environ


@pytest.mark.anyio
async def test_ctx_budget_negative_rejected_no_write(isolated_env, monkeypatch, tmp_path):
    """0 is meaningful for the budget but a negative is still garbage."""
    from textual.widgets import Input

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await _goto_config(pilot)
        app.screen.query_one("#ctx-input", Input).value = "-5"
        app.screen._commit_input("ctx-input")
        await pilot.pause()
    assert not (tmp_path / "marim" / ".env").exists()


@pytest.mark.anyio
async def test_radio_autosaves(isolated_env, monkeypatch, tmp_path):
    from textual.widgets import RadioButton

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MARIM_DEFAULT_MODE", raising=False)
    app = _Host(_fake_harness(), _env_cfg())  # default_mode == "ask"
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "session"
        await pilot.pause()
        app.screen.query_one("#defmode-plan", RadioButton).value = True
        await pilot.pause()
    assert os.environ.get("MARIM_DEFAULT_MODE") == "plan"


@pytest.mark.anyio
async def test_down_arrow_switches_section():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        screen = app.screen
        assert screen.active_section == "providers"
        assert screen.query_one("#section-providers").display is True
        assert screen.query_one("#section-session").display is False


# -- keyboard focus: rail mode vs edit mode ----------------------------------
#
# The screen opens in "rail mode" (nothing focused; ↑↓ switch sections).
# Enter dives into the active section (focusing its first editable field),
# escape climbs back out to rail mode, and a second escape closes the screen.
# While a field has focus, ↑↓ must NOT switch sections under the editor.


@pytest.mark.anyio
async def test_screen_opens_with_no_focus():
    """Rail mode: auto-focus is disabled so ↑↓ always reach the screen
    bindings on open (nothing swallows them)."""
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.screen.focused is None


@pytest.mark.anyio
async def test_enter_focuses_first_field_of_active_section():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        focused = app.screen.focused
        assert focused is not None and focused.id == "mode-set"


@pytest.mark.anyio
async def test_enter_on_providers_focuses_key_input():
    """Buttons are skipped (enter-enter must not accidentally press e.g. the
    remove button): the first *editable* field gets focus."""
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down")  # Session -> Providers
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        focused = app.screen.focused
        assert focused is not None and focused.id == "prov-key-openrouter"


@pytest.mark.anyio
async def test_escape_returns_to_rail_before_closing():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.focused is not None
        await pilot.press("escape")  # back to rail mode, screen stays
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen.focused is None
        await pilot.press("escape")  # now close
        await pilot.pause()
        assert not isinstance(app.screen, SettingsScreen)


@pytest.mark.anyio
async def test_arrows_do_not_switch_section_while_editing():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down")  # Session -> Providers
        await pilot.pause()
        await pilot.press("enter")  # focus the key input
        await pilot.pause()
        assert app.screen.focused is not None
        await pilot.press("down")  # must not move to Theme under the editor
        await pilot.pause()
        assert app.screen.active_section == "providers"


@pytest.mark.anyio
async def test_settings_open_does_not_verify_until_providers_shown(
    isolated_env, monkeypatch, tmp_path
):
    """Opening settings (on Session) must not fire catalog fetches; switching
    to Providers triggers the one-time verification."""
    from unittest.mock import AsyncMock

    from marim_harness.config import model as _m
    from marim_harness.config.model import MultiModelSource

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(_m, "_claude_cli_available", lambda: False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-verify-1234")
    multi = MultiModelSource.from_env()
    stub = AsyncMock(return_value=[])
    monkeypatch.setattr(multi.sources["openrouter"], "list_models", stub)
    harness = _fake_harness()
    harness.model_source = multi
    app = _Host(harness, _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert stub.await_count == 0
        await pilot.press("down")  # Session -> Providers
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert stub.await_count == 1


@pytest.mark.anyio
async def test_settings_has_three_tier_rows():
    """The Tools page owns three sub-agent model-tier rows, one per tier."""
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        labels = {str(w.render()) for w in app.screen.query(".row-label")}
    assert {"Cheap tier", "Med tier", "High tier"} <= labels


@pytest.mark.anyio
async def test_tools_page_has_group_headers_and_dep_rows():
    """Tools page mounts headed groups and every dependency row wrapper."""
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        s = app.screen
        headers = {str(w.render()) for w in s.query("#section-tools .group-head")}
        assert {
            "Language server",
            "Tool search",
            "Agent tools",
            "Sub-agents",
            "Advisor",
            "Thinking",
        } <= headers
        for row_id in (
            "row-lsp-tools",
            "row-toolsearch-threshold",
            "row-tier-cheap",
            "row-tier-med",
            "row-tier-high",
            "row-advisor-tokens",
            "row-advisor-uses",
        ):
            assert s.query_one(f"#section-tools #{row_id}") is not None
        # Control ids stay stable inside the wrappers.
        assert s.query_one("#row-lsp-tools #sw-lsp-tools") is not None
        assert s.query_one("#row-toolsearch-threshold #toolsearch-threshold") is not None
        assert s.query_one("#row-tier-cheap #tier-change-cheap") is not None
        assert s.query_one("#row-advisor-tokens #advisor-max-tokens") is not None
        # Banner / prose walls removed.
        body = " ".join(str(w.render()) for w in s.query("#section-tools Static"))
        assert "Saved to .env" not in body
        assert "Master switch" not in body
        assert "Advisor — a model" not in body
        assert "Thinking — reasoning" not in body
        # Session model row still exists with model-label class.
        assert "model-label" in s.query_one("#model-label").classes


@pytest.mark.anyio
async def test_tiering_toggle_saves_env_and_applies_live(isolated_env, monkeypatch, tmp_path):
    """The Tools page's Model-tiering checkbox persists MARIM_SUBAGENT_TIERING and
    flips the harness's live tier set in the same gesture — new spawns pick it up
    without a relaunch, and the curated per-tier slugs are left untouched."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    harness = _fake_harness()
    calls = []
    harness.set_subagent_tiering_enabled = lambda enabled: calls.append(enabled)
    app = _Host(harness, _env_cfg())  # tiers.enabled defaults True
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        assert app.screen.query_one("#sw-tiering").value is True
        _scroll_to(app, "#sw-tiering")
        await pilot.click("#sw-tiering")  # toggle tiering off
        await pilot.pause()
        status = str(app.screen.query_one("#settings-status").render())
        # The live config mirror flips too, so a re-render shows the new state.
        assert app.screen.env_cfg.subagent.tiers.enabled is False
    assert "MARIM_SUBAGENT_TIERING=0" in (tmp_path / "marim" / ".env").read_text()
    assert calls == [False]
    assert "applied" in status


@pytest.mark.anyio
async def test_tiering_toggle_reflects_disabled_config():
    """The checkbox mirrors the configured state, not a hardcoded default."""
    from dataclasses import replace

    env_cfg = _env_cfg()
    env_cfg.subagent.tiers = replace(env_cfg.subagent.tiers, enabled=False)
    app = _Host(_fake_harness(), env_cfg)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        assert app.screen.query_one("#sw-tiering").value is False


@pytest.mark.anyio
async def test_tier_rows_show_inherit_main_by_default():
    """An unset tier reads 'inherit main', not a blank or None."""
    app = _Host(_fake_harness(), _env_cfg())  # tiers all unset
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        value = str(app.screen.query_one("#tier-value-cheap").render())
    assert value == "inherit main"


@pytest.mark.anyio
async def test_tier_choice_saves_env_and_refreshes_catalog(isolated_env, monkeypatch, tmp_path):
    """Choosing a model for the cheap tier persists MARIM_SUBAGENT_TIER_CHEAP to
    .env (mirrored into os.environ), refreshes the live MultiModelSource catalog
    (same as a Providers credential save), and updates the row's displayed value —
    mirroring the exact save + refresh_from_env pattern the main-model picker and
    ProvidersPane already use."""
    from marim_harness.config.model import MultiModelSource

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-tier-test")
    multi = MultiModelSource.from_env()
    refresh_calls = []
    monkeypatch.setattr(multi, "refresh_from_env", lambda: refresh_calls.append(True))
    harness = _fake_harness()
    harness.model_source = multi
    app = _Host(harness, _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen._on_tier_chosen("cheap", "openrouter/cheap-model")
        await pilot.pause()
        value = str(app.screen.query_one("#tier-value-cheap").render())
    env_text = (tmp_path / "marim" / ".env").read_text()
    assert "MARIM_SUBAGENT_TIER_CHEAP=openrouter/cheap-model" in env_text
    assert os.environ.get("MARIM_SUBAGENT_TIER_CHEAP") == "openrouter/cheap-model"
    assert refresh_calls == [True]
    assert value == "openrouter/cheap-model"


@pytest.mark.anyio
async def test_tier_change_button_opens_model_picker_with_current_value():
    """The 'change' button on a tier row opens ModelPickerModal seeded with
    that tier's current configured value (the med tier here)."""
    from dataclasses import replace
    from types import SimpleNamespace

    from marim_harness.interfaces.tui.model_picker import ModelPickerModal

    async def _empty_catalog():
        return []

    harness = _fake_harness()
    harness.model_source = SimpleNamespace(list_models=_empty_catalog, is_local=False)
    env_cfg = _env_cfg()
    env_cfg.subagent.tiers = replace(env_cfg.subagent.tiers, med="openrouter/med-model")
    app = _Host(harness, env_cfg)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        _scroll_to(app, "#tier-change-med")
        await pilot.click("#tier-change-med")
        await pilot.pause()
        modal = app.screen_stack[-1]
    assert isinstance(modal, ModelPickerModal)
    assert modal.current == "openrouter/med-model"


@pytest.mark.anyio
async def test_tier_picker_unavailable_without_model_source():
    """When the harness has no model_source (embedding/tests), the tier row
    reports unavailability on the row's own value line instead of opening a
    picker it can't populate — mirroring the main model row's guard."""
    harness = _fake_harness()  # model_source is None
    app = _Host(harness, _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        _scroll_to(app, "#tier-change-high")
        await pilot.click("#tier-change-high")
        await pilot.pause()
        value = str(app.screen.query_one("#tier-value-high").render())
        assert isinstance(app.screen, SettingsScreen)
    assert "isn't available" in value


@pytest.mark.anyio
async def test_escape_discards_half_typed_secret(isolated_env, monkeypatch, tmp_path):
    """Escape reads as cancel: leaving edit mode must not blur-commit a
    half-typed API key. Non-secret fields keep the screen's save-on-blur
    model — only password inputs are discarded."""
    from textual.widgets import Input

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down")  # rail -> Providers
        await pilot.press("enter")  # focus the openrouter key input
        await pilot.pause()
        s = app.screen
        inp = s.query_one("#prov-key-openrouter", Input)
        assert s.focused is inp
        inp.value = "sk-half-typed"
        await pilot.press("escape")  # back to rail — discard, don't save
        await pilot.pause()
        assert s.focused is None
        assert inp.value == ""
        assert os.environ.get("OPENROUTER_API_KEY") is None
    assert not (tmp_path / "marim" / ".env").exists()


@pytest.mark.anyio
async def test_settings_has_advisor_row_defaulting_off():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        value = str(app.screen.query_one("#advisor-value").render())
    assert value == "off"


@pytest.mark.anyio
async def test_advisor_choice_saves_env_and_refreshes_catalog(
    isolated_env, monkeypatch, tmp_path
):
    from marim_harness.config.model import MultiModelSource

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-advisor-test")
    multi = MultiModelSource.from_env()
    refresh_calls = []
    monkeypatch.setattr(multi, "refresh_from_env", lambda: refresh_calls.append(True))
    harness = _fake_harness()
    harness.model_source = multi
    app = _Host(harness, _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen._on_advisor_chosen("openrouter:advisor-model")
        await pilot.pause()
        value = str(app.screen.query_one("#advisor-value").render())
    env_text = (tmp_path / "marim" / ".env").read_text()
    assert "MARIM_ADVISOR_MODEL=openrouter:advisor-model" in env_text
    assert os.environ.get("MARIM_ADVISOR_MODEL") == "openrouter:advisor-model"
    assert refresh_calls == [True]
    assert value == "openrouter:advisor-model"


@pytest.mark.anyio
async def test_advisor_off_choice_drops_the_env_var(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_ADVISOR_MODEL", "openrouter:old-advisor")
    env_cfg = _env_cfg()
    env_cfg.advisor_model = "openrouter:old-advisor"
    app = _Host(_fake_harness(), env_cfg)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen._on_advisor_chosen("off")
        await pilot.pause()
        value = str(app.screen.query_one("#advisor-value").render())
    assert os.environ.get("MARIM_ADVISOR_MODEL") is None
    assert value == "off"


@pytest.mark.anyio
async def test_advisor_numeric_knobs_are_registered():
    from marim_harness.interfaces.tui.settings_env import ENV_INT_INPUTS, ZERO_OK_INPUTS

    assert ENV_INT_INPUTS["advisor-max-tokens"][0] == "MARIM_ADVISOR_MAX_TOKENS"
    assert ENV_INT_INPUTS["advisor-max-uses"][0] == "MARIM_ADVISOR_MAX_USES"
    # 0 = unlimited must be commit-able, like the context budget's 0.
    assert "advisor-max-uses" in ZERO_OK_INPUTS


@pytest.mark.anyio
async def test_settings_has_thinking_row_defaulting_off():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        value = str(app.screen.query_one("#thinking-value").render())
    assert value == "off"


@pytest.mark.anyio
async def test_thinking_choice_saves_env(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen._on_thinking_chosen("high")
        await pilot.pause()
        value = str(app.screen.query_one("#thinking-value").render())
    env_text = (tmp_path / "marim" / ".env").read_text()
    assert "MARIM_THINKING=high" in env_text
    assert os.environ.get("MARIM_THINKING") == "high"
    assert value == "high"


@pytest.mark.anyio
async def test_thinking_off_choice_drops_the_env_var(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_THINKING", "high")
    env_cfg = _env_cfg()
    env_cfg.thinking_level = "high"
    app = _Host(_fake_harness(), env_cfg)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen._on_thinking_chosen("off")
        await pilot.pause()
        value = str(app.screen.query_one("#thinking-value").render())
    assert os.environ.get("MARIM_THINKING") is None
    assert value == "off"


@pytest.mark.anyio
async def test_tools_section_shows_section_help_on_rail():
    from marim_harness.interfaces.tui.settings_env import SECTION_HELP

    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        # Rail: nothing focused.
        app.screen.set_focus(None)
        await pilot.pause()
        help_w = app.screen.query_one("#settings-help")
        assert str(help_w.render()) == SECTION_HELP["tools"]
        # Session has no SECTION_HELP → empty.
        app.screen.active_section = "session"
        await pilot.pause()
        assert str(app.screen.query_one("#settings-help").render()) == ""


@pytest.mark.anyio
async def test_focusing_tools_field_shows_field_help():
    from marim_harness.interfaces.tui.settings_env import FIELD_HELP

    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen.query_one("#sw-lsp").focus()
        await pilot.pause()
        assert str(app.screen.query_one("#settings-help").render()) == FIELD_HELP["sw-lsp"]
        # Escape returns to rail → section help again.
        await pilot.press("escape")
        await pilot.pause()
        from marim_harness.interfaces.tui.settings_env import SECTION_HELP

        assert str(app.screen.query_one("#settings-help").render()) == SECTION_HELP["tools"]
