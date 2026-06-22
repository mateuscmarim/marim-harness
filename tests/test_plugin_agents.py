import json
from pathlib import Path

from marim_harness.plugins import InstalledPlugin, save_state
from marim_harness.workspace.agents import (
    agents_index_text,
    discover_agents,
    find_agent,
)


def _install_plugin_with_agent(plugins_dir: Path, plugin: str, agent: str):
    pdir = plugins_dir / plugin
    (pdir / ".marim-plugin").mkdir(parents=True, exist_ok=True)
    (pdir / ".marim-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin}), encoding="utf-8"
    )
    adir = pdir / "agents"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{agent}.md").write_text(
        f"---\nname: {agent}\ndescription: plugin agent\n---\nYou are {agent}.",
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
            )
        },
    )


def test_plugin_agent_is_namespaced(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_agent(gdir, "myplugin", "reviewer")
    names = [a.qualified_name for a in discover_agents(ws)]
    assert "myplugin:reviewer" in names
    # built-ins still present with bare names
    assert "explore" in names and "general" in names
    found = find_agent(ws, "myplugin:reviewer")
    assert found is not None and found.plugin == "myplugin"
    assert "- myplugin:reviewer — plugin agent" in agents_index_text(
        discover_agents(ws)
    )
