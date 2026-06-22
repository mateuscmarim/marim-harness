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


def test_user_and_plugin_agent_same_stem_coexist(tmp_path, monkeypatch):
    """A USER agent and a PLUGIN agent with the same stem must both survive.

    The user's copy is reachable by bare name; the plugin's copy is reachable
    by ``plugin:name``. Neither shadows the other.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    # Install the plugin agent named "analyst"
    _install_plugin_with_agent(gdir, "myplugin", "analyst")
    # Write a user (global) agent also named "analyst"
    user_agents_dir = tmp_path / "cfg" / "marim" / "agents"
    user_agents_dir.mkdir(parents=True, exist_ok=True)
    (user_agents_dir / "analyst.md").write_text(
        "---\nname: analyst\ndescription: user analyst\n---\nUser analyst prompt.",
        encoding="utf-8",
    )
    agents = discover_agents(ws)
    names = [a.qualified_name for a in agents]
    # Both must be present under distinct keys
    assert "analyst" in names
    assert "myplugin:analyst" in names
    # The bare-name entry is the user's copy, not the plugin's
    user_agent = find_agent(ws, "analyst")
    assert user_agent is not None and user_agent.plugin is None
    assert user_agent.description == "user analyst"
    # The namespaced entry is the plugin's copy
    plugin_agent = find_agent(ws, "myplugin:analyst")
    assert plugin_agent is not None and plugin_agent.plugin == "myplugin"


def test_plugin_agent_named_explore_does_not_shadow_builtin(tmp_path, monkeypatch):
    """A plugin agent named ``explore`` surfaces as ``<plugin>:explore``.

    The built-in ``explore`` must still be resolvable by its bare name.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    gdir = tmp_path / "cfg" / "marim" / "plugins"
    _install_plugin_with_agent(gdir, "myplugin", "explore")
    agents = discover_agents(ws)
    names = [a.qualified_name for a in agents]
    # Plugin-namespaced entry is present
    assert "myplugin:explore" in names
    # Built-in bare name is still present
    assert "explore" in names
    # The bare-name ``explore`` must be the built-in, not the plugin's copy
    builtin_explore = find_agent(ws, "explore")
    assert builtin_explore is not None and builtin_explore.plugin is None
    assert builtin_explore.source == "built-in"
