import json
from pathlib import Path

from marim_harness.mcp.config import load_mcp_config
from marim_harness.plugins import InstalledPlugin, save_state


def _install_plugin_with_mcp(plugins_dir: Path, plugin: str, trusted: bool):
    pdir = plugins_dir / plugin
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (
        pdir / ".marim-plugin" / "plugin.json"
    ).write_text(json.dumps({"name": plugin}), encoding="utf-8")
    (pdir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"web": {"url": "https://plugin"}}}),
        encoding="utf-8",
    )
    save_state(
        plugins_dir,
        {
            plugin: InstalledPlugin(
                name=plugin,
                version=None,
                source={"type": "local"},
                enabled=True,
                trusted=trusted,
            )
        },
    )


def test_trusted_plugin_mcp_merged_and_namespaced(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_mcp(gdir, "p", trusted=True)
    specs = load_mcp_config(ws)
    assert "p_web" in specs
    assert specs["p_web"]["url"] == "https://plugin"


def test_untrusted_plugin_mcp_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_mcp(gdir, "p", trusted=False)
    assert load_mcp_config(ws) == {}


def test_disabled_trusted_plugin_mcp_excluded(tmp_path, monkeypatch):
    """A TRUSTED but DISABLED plugin must contribute zero MCP servers.

    Being trusted is not sufficient; the plugin must also be enabled.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    pdir = gdir / "p"
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p"}), encoding="utf-8"
    )
    (pdir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"web": {"url": "https://plugin"}}}),
        encoding="utf-8",
    )
    save_state(
        gdir,
        {
            "p": InstalledPlugin(
                name="p",
                version=None,
                source={"type": "local"},
                enabled=False,
                trusted=True,
            )
        },
    )
    assert load_mcp_config(ws) == {}
