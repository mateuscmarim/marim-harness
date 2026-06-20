import os

import pytest
from textual.app import App

from marim_harness.config import ModelConfig
from marim_harness.interfaces.tui.settings import SettingsModal
from marim_harness.permissions import Mode


@pytest.fixture
def isolated_env():
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _fake_harness():
    from types import SimpleNamespace

    return SimpleNamespace(
        deps=SimpleNamespace(mode=Mode.auto),
        model_label="openrouter/x",
        model_id="x",
        model_source=None,  # disables the model-change picker path
        set_model=lambda mid: None,
        mcp=SimpleNamespace(
            configured_names=lambda: [],
            disabled=set(),
            mcp_status={"connected": [], "failed": []},
        ),
    )


def _env_cfg():
    return ModelConfig(provider="openrouter", model="x")


class _Host(App):
    def __init__(self, harness, env_cfg):
        from types import SimpleNamespace

        super().__init__()
        self._harness = harness
        self._env_cfg = env_cfg
        # The modal refreshes the status bar via app.status.refresh_status().
        self.status = SimpleNamespace(refresh_status=lambda: None)

    def on_mount(self) -> None:
        self.push_screen(
            SettingsModal(harness=self._harness, current_theme="t", env_cfg=self._env_cfg)
        )


def _scroll_to(app: App, widget_id: str) -> None:
    """Scroll the settings VerticalScroll to make a widget visible before clicking.

    Textual 8.x pilot.click resolves the widget's position within its scroll
    container; clicking fails with OutOfBounds when the widget is scrolled out of
    the physical screen region. Scrolling the container into position first makes
    the click land correctly.
    """
    scroll = app.screen.query_one("#settings-scroll")
    widget = app.screen.query_one(widget_id)
    scroll.scroll_to_widget(widget, animate=False)


@pytest.mark.anyio
async def test_mode_radio_applies_live():
    harness = _fake_harness()
    app = _Host(harness, _env_cfg())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#mode-plan")
        await pilot.pause()
    assert harness.deps.mode == Mode.plan


@pytest.mark.anyio
async def test_save_writes_env_file(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    harness = _fake_harness()
    app = _Host(harness, _env_cfg())  # lsp_enabled defaults True -> switch on
    async with app.run_test(size=(80, 80)) as pilot:
        await pilot.pause()
        _scroll_to(app, "#sw-lsp")
        await pilot.pause()
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
    async with app.run_test(size=(80, 80)) as pilot:
        await pilot.pause()
        ctx = app.screen.query_one("#ctx-input")
        ctx.value = "0"
        await pilot.pause()
        _scroll_to(app, "#save-env")
        await pilot.pause()
        await pilot.click("#save-env")
        await pilot.pause()
        # In Textual 8.x, Static stores content as _Static__content (str(render())
        # is the public equivalent of the removed .renderable attribute).
        status = str(app.screen.query_one("#save-status").render())
    assert not (tmp_path / "marim" / ".env").exists()
    assert "positive integer" in str(status)


@pytest.mark.anyio
async def test_escape_dismisses():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SettingsModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SettingsModal)
