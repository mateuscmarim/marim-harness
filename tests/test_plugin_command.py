import asyncio
import json
from pathlib import Path

from marim_harness.interfaces.tui.commands import _cmd_plugin
from marim_harness.plugins import InstalledPlugin, load_state, save_state


class _FakeDeps:
    def __init__(self, ws):
        self.workspace_root = ws


class _FakeHarness:
    def __init__(self, ws):
        self.deps = _FakeDeps(ws)


class _FakeApp:
    def __init__(self, ws):
        self.harness = _FakeHarness(ws)
        self.messages = []

    async def post_system(self, text):
        self.messages.append(text)


def _install(plugins_dir: Path, name: str, enabled=True):
    pdir = plugins_dir / name
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "description": "d"}), encoding="utf-8"
    )
    save_state(plugins_dir, {name: InstalledPlugin(
        name=name, version=None, source={"type": "local"}, enabled=enabled)})


def test_plugin_list(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    _install(tmp_path / "cfg" / "marim" / "plugins", "demo")
    app = _FakeApp(ws)
    asyncio.run(_cmd_plugin(app, "list"))
    assert any("demo" in m for m in app.messages)


def test_plugin_disable_then_enable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install(gdir, "demo")
    app = _FakeApp(ws)
    asyncio.run(_cmd_plugin(app, "disable demo"))
    assert load_state(gdir)["demo"].enabled is False
    asyncio.run(_cmd_plugin(app, "enable demo"))
    assert load_state(gdir)["demo"].enabled is True
    assert any("next launch" in m.lower() for m in app.messages)


def test_plugin_unknown_subcommand_posts_usage(tmp_path, monkeypatch):
    """An unknown subcommand must post a usage message, not crash."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    app = _FakeApp(ws)
    asyncio.run(_cmd_plugin(app, "somethingbogus"))
    assert app.messages, "expected at least one message"
    assert any("Usage" in m or "usage" in m for m in app.messages)


def test_plugin_list_no_plugins_installed(tmp_path, monkeypatch):
    """``/plugin list`` with no plugins installed must post the expected message."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    app = _FakeApp(ws)
    asyncio.run(_cmd_plugin(app, "list"))
    assert app.messages, "expected at least one message"
    assert any("No plugins installed" in m for m in app.messages)
