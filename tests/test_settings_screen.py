"""Tests for the full-bleed SettingsScreen (replaces the centered SettingsModal).

The screen mirrors the sub-agents full-bleed layout: a header breadcrumb, a left
section rail (Session / Theme / MCP servers / Context & Memory / Tools /
Notifications / Advanced), a content pane, and a footer hint bar. Sections are
mounted once and shown/hidden by ``display`` so widget state and ids survive
section switches.
"""

import os

import pytest
from textual.app import App

from marim_harness.config import ModelConfig
from marim_harness.interfaces.tui.settings import SettingsScreen
from marim_harness.interfaces.tui.themes import MARIM_THEMES, THEME_NAMES
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
        deps=SimpleNamespace(workspace=SimpleNamespace(mode=Mode.auto)),
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
