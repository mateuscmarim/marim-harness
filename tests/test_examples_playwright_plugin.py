"""Regression guard for the bundled ``examples/playwright`` plugin: keep its
manifest, agents, and MCP server parseable by marim's own loaders so the
example can't silently rot as the plugin format evolves."""

from pathlib import Path

from marim_harness.plugins.discovery import _resolve_mcp_servers
from marim_harness.plugins.manifest import load_manifest
from marim_harness.workspace.agents import _parse_agent
from marim_harness.workspace.skills import _parse_skill

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "playwright"


def test_manifest_loads():
    m = load_manifest(PLUGIN_ROOT)
    assert m.name == "playwright"
    assert m.version == "0.1.0"


def test_three_agents_parse_with_expected_tools():
    expected = {
        "planner": {"read_file", "grep", "glob", "tree"},
        "generator": {"read_file", "grep", "glob", "tree"},
        # The healer edits spec files, so it carries the gated write tools.
        "healer": {"read_file", "grep", "glob", "tree", "edit_file", "write_file"},
    }
    for name, tools in expected.items():
        defn = _parse_agent(
            "plugin:playwright", PLUGIN_ROOT / "agents" / f"{name}.md", plugin="playwright"
        )
        assert defn is not None, f"{name}.md failed to parse"
        assert defn.qualified_name == f"playwright:{name}"
        assert set(defn.tools) == tools


def test_e2e_tests_skill_parses():
    # The workflow lives in a lazy-loaded skill (not always-on AGENTS.md); its
    # description must mention browser/e2e/playwright so the model triggers it.
    skill = _parse_skill(
        "plugin:playwright", PLUGIN_ROOT / "skills" / "e2e-tests", plugin="playwright"
    )
    assert skill is not None
    assert skill.qualified_name == "playwright:e2e-tests"
    desc = skill.description.lower()
    assert any(kw in desc for kw in ("playwright", "browser", "end-to-end", "e2e"))


def test_mcp_server_resolves_to_playwright_test():
    m = load_manifest(PLUGIN_ROOT)
    servers = _resolve_mcp_servers(m.mcp_source())
    assert servers is not None
    # marim namespaces a plugin server as ``<plugin>_<server>``, so the grant
    # name documented in AGENTS.md (``playwright_test``) must hold.
    assert set(servers) == {"test"}
    spec = servers["test"]
    assert spec["command"] == "npx"
    assert spec["args"] == ["playwright", "run-test-mcp-server"]
