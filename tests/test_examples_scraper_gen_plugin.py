"""Regression guard for the bundled ``examples/scraper-gen`` plugin: keep its
manifest, agents, and skill parseable by marim's own loaders so the example
can't silently rot as the plugin format evolves."""

from pathlib import Path

from marim_harness.plugins.manifest import load_manifest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "scraper-gen"


def test_manifest_loads():
    m = load_manifest(PLUGIN_ROOT)
    assert m.name == "scraper-gen"
    assert m.version == "0.1.0"


def test_three_agents_parse_with_expected_tools():
    from marim_harness.workspace.agents import _parse_agent

    expected = {
        # Explores over HTTP (bash/fetch_url) and writes only specs/plan.md.
        "planner": {
            "read_file", "grep", "glob", "tree",
            "fetch_url", "web_search", "bash", "write_file",
        },
        # Writes and iterates on its one script.
        "generator": {
            "read_file", "grep", "glob", "tree", "write_file", "edit_file", "bash",
        },
        # Repairs existing scripts; deliberately no write_file.
        "healer": {"read_file", "grep", "glob", "tree", "edit_file", "bash"},
    }
    for name, tools in expected.items():
        defn = _parse_agent(
            "plugin:scraper-gen", PLUGIN_ROOT / "agents" / f"{name}.md", plugin="scraper-gen"
        )
        assert defn is not None, f"{name}.md failed to parse"
        assert defn.qualified_name == f"scraper-gen:{name}"
        assert set(defn.tools) == tools, f"{name} tools drifted"
