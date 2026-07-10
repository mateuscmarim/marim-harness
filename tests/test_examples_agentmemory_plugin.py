"""Regression guard for the bundled ``examples/agentmemory`` plugin: keep its
manifest and its hooks/MCP wiring parseable by marim's own loaders so the
wiring-only example can't silently rot as the plugin format evolves."""

from pathlib import Path

from marim_harness.plugins.discovery import plugin_bundle_summary
from marim_harness.plugins.manifest import load_manifest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "agentmemory"


def test_manifest_loads():
    m = load_manifest(PLUGIN_ROOT)
    assert m.name == "agentmemory"
    assert m.version == "0.9.27"


def test_bundle_wires_nine_hooks_and_one_mcp_server():
    m = load_manifest(PLUGIN_ROOT)
    summary = plugin_bundle_summary(m)
    # Wiring-only: no vendored skills/agents, all value is the hooks + MCP server.
    assert summary == {"skills": 0, "agents": 0, "hooks": 9, "mcpServers": 1}


def test_hooks_reference_external_agentmemory_scripts():
    # The scripts live in agentmemory's own install, reached via ${CLAUDE_PLUGIN_ROOT}
    # (an env var expanded by the shell at fire time) — never ${MARIM_PLUGIN_ROOT},
    # which would point inside this plugin dir where the scripts do not exist.
    m = load_manifest(PLUGIN_ROOT)
    hooks = m.hooks_source()
    assert isinstance(hooks, dict)
    start_cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "${CLAUDE_PLUGIN_ROOT}" in start_cmd
    assert "${MARIM_PLUGIN_ROOT}" not in start_cmd
    assert "session-start.mjs" in start_cmd


def test_mcp_env_carries_no_secret_and_no_unexpanded_placeholder():
    # marim passes an MCP server's env verbatim (no ${VAR} expansion), so the
    # committed spec must hold literal values and never a secret.
    m = load_manifest(PLUGIN_ROOT)
    servers = m.mcp_source()
    assert isinstance(servers, dict)
    env = servers["agentmemory"]["env"]
    assert "AGENTMEMORY_SECRET" not in env
    assert "${" not in "".join(env.values())
