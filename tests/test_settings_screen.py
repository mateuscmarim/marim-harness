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
async def test_save_writes_env_file(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())  # lsp_enabled defaults True
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await _goto_config(pilot)
        await pilot.click("#sw-lsp")  # toggle LSP off
        await pilot.pause()
        _scroll_to(app, "#save-env")
        await pilot.pause()
        await pilot.click("#save-env")
        await pilot.pause()
    env_file = tmp_path / "marim" / ".env"
    assert env_file.exists()
    text = env_file.read_text()
    assert "MARIM_LSP=0" in text
    assert "MARIM_MAX_CONTEXT_TOKENS=100000" in text


@pytest.mark.anyio
async def test_invalid_context_budget_blocks_save(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await _goto_config(pilot)
        app.screen.query_one("#ctx-input").value = "0"
        await pilot.pause()
        _scroll_to(app, "#save-env")
        await pilot.pause()
        await pilot.click("#save-env")
        await pilot.pause()
        status = str(app.screen.query_one("#save-status").render())
    assert not (tmp_path / "marim" / ".env").exists()
    assert "positive integer" in status


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


@pytest.mark.anyio
async def test_default_mode_radio_reflects_config_and_saves(isolated_env, monkeypatch, tmp_path):
    """The Config section's default-mode radio shows the configured value and,
    on save, persists MARIM_DEFAULT_MODE to the .env (mirrored to os.environ)."""
    from textual.widgets import RadioButton

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MARIM_DEFAULT_MODE", raising=False)
    app = _Host(_fake_harness(), _env_cfg())  # env_cfg.default_mode == "ask"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        # config default is "ask" -> that radio starts pressed
        assert screen.query_one("#defmode-ask", RadioButton).value is True
        screen.active_section = "config"
        await pilot.pause()
        screen.query_one("#defmode-plan", RadioButton).value = True
        await pilot.pause()
        screen._save_env()
        await pilot.pause()
    assert os.environ.get("MARIM_DEFAULT_MODE") == "plan"


@pytest.mark.anyio
async def test_tool_search_selector_saves(isolated_env, monkeypatch, tmp_path):
    from textual.widgets import Input, RadioButton

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MARIM_TOOL_SEARCH", raising=False)
    app = _Host(_fake_harness(), _env_cfg())  # env_cfg.tool_search == "auto"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#toolsearch-auto", RadioButton).value is True
        screen.active_section = "config"
        await pilot.pause()
        screen.query_one("#toolsearch-on", RadioButton).value = True
        screen.query_one("#toolsearch-threshold", Input).value = "20"
        await pilot.pause()
        screen._save_env()
        await pilot.pause()
    assert os.environ.get("MARIM_TOOL_SEARCH") == "on"
    assert os.environ.get("MARIM_TOOL_SEARCH_THRESHOLD") == "20"
