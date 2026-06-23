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


def test_global_and_plugin_hooks_concatenated(tmp_path, monkeypatch):
    """A global Stop hook and a trusted plugin Stop hook must BOTH appear.

    They are concatenated into one list; the plugin entry must not overwrite
    the global entry.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    # Write a global hooks.json with its own Stop command
    global_hooks_dir = tmp_path / "cfg" / "marim"
    global_hooks_dir.mkdir(parents=True, exist_ok=True)
    (global_hooks_dir / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"type": "command", "command": "echo global"}]}}
        ),
        encoding="utf-8",
    )
    # Install a trusted plugin that also contributes a Stop hook
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_hooks(gdir, "p", trusted=True)
    cfg = load_hooks_config(ws, trust_project=False)
    stop_commands = [entry["command"] for entry in cfg.get("Stop", [])]
    assert "echo global" in stop_commands
    assert "echo hi" in stop_commands


def test_plugin_trust_flip_includes_hooks(tmp_path, monkeypatch):
    """A plugin installed without trust must NOT have its hooks merged. After
    the user grants trust (the on-disk state file is updated), the next config
    load must include the hooks. Guards against a future refactor that caches
    the trust flag at install time and forgets to re-read it on every load."""
    from marim_harness.plugins.state import load_state

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_hooks(gdir, "p", trusted=False)

    # Before trust — hooks excluded.
    cfg = load_hooks_config(ws, trust_project=False)
    assert "Stop" not in cfg or all(
        e.get("command") != "echo hi" for e in cfg["Stop"]
    )

    # Grant trust on disk, re-read, hooks must now appear.
    state_path = gdir / "state.json"
    state = load_state(gdir)
    state["p"] = state["p"].__class__(**{**state["p"].__dict__, "trusted": True})
    save_state(gdir, state)
    cfg = load_hooks_config(ws, trust_project=False)
    assert any(e.get("command") == "echo hi" for e in cfg.get("Stop", []))
