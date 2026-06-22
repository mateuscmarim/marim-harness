"""End-to-end integration test for the plugin system.

Exercises the full lifecycle: install (untrusted) → inert content surfaces
namespaced → executable content gated off → trust granted → hooks + MCP load
with ${MARIM_PLUGIN_ROOT} resolved.
"""

from pathlib import Path

from marim_harness.hooks.config import load_hooks_config
from marim_harness.mcp.config import load_mcp_config
from marim_harness.plugins import install_plugin, set_trusted
from marim_harness.workspace.agents import discover_agents
from marim_harness.workspace.skills import discover_skills

FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "demo-plugin"


def test_full_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ws = tmp_path / "ws"
    ws.mkdir()

    # Install untrusted (it has hooks + MCP -> stays untrusted without --trust).
    rec = install_plugin(str(FIXTURE), scope="global", workspace_root=ws, trust=False, now="T")
    assert rec.trusted is False

    # Inert content surfaces immediately, namespaced.
    skills = {s.qualified_name for s in discover_skills(ws)}
    agents = {a.qualified_name for a in discover_agents(ws)}
    assert "demo-plugin:greet" in skills
    assert "demo-plugin:reviewer" in agents

    # Executable content is gated off while untrusted.
    assert load_hooks_config(ws, trust_project=False) == {}
    assert load_mcp_config(ws) == {}

    # Trusting turns on hooks + MCP, with ${MARIM_PLUGIN_ROOT} resolved and
    # the MCP server namespaced.
    set_trusted("demo-plugin", scope="global", workspace_root=ws, trusted=True)
    hooks = load_hooks_config(ws, trust_project=False)
    assert hooks["Stop"][0]["command"].endswith("/bin/notify.sh")
    assert "${MARIM_PLUGIN_ROOT}" not in hooks["Stop"][0]["command"]
    assert "demo-plugin_docs" in load_mcp_config(ws)
