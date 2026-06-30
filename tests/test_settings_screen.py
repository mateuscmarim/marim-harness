"""Tests for the full-bleed SettingsScreen (replaces the centered SettingsModal).

The screen mirrors the sub-agents full-bleed layout: a header breadcrumb, a left
section rail (Runtime / Theme / MCP servers / Config), a content pane, and a footer
hint bar. Sections are mounted once and shown/hidden by ``display`` so widget state
and ids survive section switches.
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
async def test_opens_on_runtime_section():
    """The screen opens with Runtime active: its section is shown, others hidden."""
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.active_section == "runtime"
        assert screen.query_one("#section-runtime").display is True
        assert screen.query_one("#section-theme").display is False


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
    """Selecting a mode in the Runtime section applies it to the harness at once."""
    harness = _fake_harness()
    app = _Host(harness, _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#mode-plan")
        await pilot.pause()
    assert harness.deps.workspace.mode == Mode.plan


@pytest.mark.anyio
async def test_mcp_toggle_disables_server():
    """Toggling a connected server in the MCP section disables it on the harness."""
    h = _mcp_harness()
    app = _Host(h, _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down", "down")  # Runtime -> Theme -> MCP
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
        await pilot.press("down")  # Runtime -> Theme
        await pilot.pause()
        await pilot.click("#theme-0")  # first theme
        await pilot.pause()
        assert app.theme == THEME_NAMES[0]


async def _goto_config(pilot):
    """Navigate Runtime -> Config (the 4th rail section)."""
    await pilot.press("down", "down", "down")
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


@pytest.mark.anyio
async def test_down_arrow_switches_section():
    """Pressing down moves the rail selection Runtime -> Theme and swaps content."""
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        screen = app.screen
        assert screen.active_section == "theme"
        assert screen.query_one("#section-theme").display is True
        assert screen.query_one("#section-runtime").display is False
