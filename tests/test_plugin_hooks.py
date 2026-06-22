import json
from pathlib import Path

from marim_harness.hooks.config import load_hooks_config
from marim_harness.plugins import InstalledPlugin, save_state


def _install_plugin_with_hooks(plugins_dir: Path, plugin: str, trusted: bool):
    pdir = plugins_dir / plugin
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin}), encoding="utf-8"
    )
    (pdir / "hooks").mkdir(parents=True, exist_ok=True)
    (pdir / "hooks" / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"type": "command", "command": "echo hi"}]}}
        ),
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


def test_trusted_plugin_hooks_merged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_hooks(gdir, "p", trusted=True)
    cfg = load_hooks_config(ws, trust_project=False)
    assert cfg["Stop"][0]["command"] == "echo hi"


def test_untrusted_plugin_hooks_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_hooks(gdir, "p", trusted=False)
    cfg = load_hooks_config(ws, trust_project=False)
    assert cfg == {}
