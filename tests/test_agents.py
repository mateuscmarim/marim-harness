from pathlib import Path

import pytest

from marim_harness.tools.provider import (
    GATED_TOOLS,
    NET_TOOLS,
    READ_TOOLS,
    SUBAGENT_TOOLS,
)
from marim_harness.workspace import (
    AgentDef,
    agent_roots,
    agents_index_text,
    discover_agents,
    effective_tools,
    find_agent,
    subagent_instructions,
)


def _make_agent(
    root: Path,
    name: str,
    description: str = "Does a thing. Use when the user wants the thing.",
    body: str = "You are a helpful sub-agent.",
    extra_fm: str = "",
    fm_name: str | None = None,
) -> Path:
    """Create a custom agent definition file (``<name>.md``)."""
    root.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        f"name: {name if fm_name is None else fm_name}\n"
        f"description: {description}\n"
        f"{extra_fm}"
        "---\n"
    )
    path = root / f"{name}.md"
    path.write_text(fm + "\n" + body + "\n", encoding="utf-8")
    return path


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def test_agent_roots_order_and_precedence(tmp_path):
    from marim_harness.config import builtin_root

    ws = tmp_path / "ws"
    roots = agent_roots(ws)
    sources = [s for s, _ in roots]
    assert sources == ["project", "global", "builtin"]
    assert roots[2][1] == builtin_root() / "agents"


def test_ignores_claude_agents_dir(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(ws / ".claude" / "agents", "from-claude")
    names = {a.name for a in discover_agents(ws)}
    assert "from-claude" not in names


def test_builtins_always_present(isolated_home):
    ws = isolated_home / "ws"
    names = {a.name for a in discover_agents(ws)}
    assert {"explore", "general"} <= names


def test_builtin_explore_has_local_reads_and_net_but_no_mutators(isolated_home):
    ws = isolated_home / "ws"
    explore = find_agent(ws, "explore")
    assert explore is not None
    # Local reads + network egress (web lookups), but nothing that mutates.
    assert explore.tools == READ_TOOLS | NET_TOOLS
    assert explore.tools >= NET_TOOLS
    assert not (explore.tools & GATED_TOOLS)
    assert explore.source == "built-in"


def test_builtin_general_has_full_set(isolated_home):
    ws = isolated_home / "ws"
    general = find_agent(ws, "general")
    assert general.tools == SUBAGENT_TOOLS


def test_discover_custom_agent(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(
        ws / ".marim" / "agents", "reviewer",
        description="Reviews a diff.", body="You review diffs for bugs.",
        extra_fm="tools: read_file, grep\n",
    )
    reviewer = find_agent(ws, "reviewer")
    assert reviewer is not None
    assert reviewer.description == "Reviews a diff."
    assert "You review diffs for bugs." in reviewer.prompt
    assert reviewer.tools == frozenset({"read_file", "grep"})
    assert reviewer.source == "project"


def test_custom_agent_without_tools_defaults_read_only(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(ws / ".marim" / "agents", "notes", description="Takes notes.")
    tools = find_agent(ws, "notes").tools
    assert tools == READ_TOOLS
    # No network egress by default — a custom agent must opt into NET_TOOLS.
    assert not (tools & NET_TOOLS)


def test_custom_tools_intersect_known_set(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(
        ws / ".marim" / "agents", "writer", description="Writes code.",
        extra_fm="tools: read_file, write_file, telepathy\n",
    )
    # Unknown 'telepathy' is dropped; known names (incl. gated) are kept.
    assert find_agent(ws, "writer").tools == frozenset({"read_file", "write_file"})


def test_custom_agent_overrides_builtin(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(
        ws / ".marim" / "agents", "explore",
        description="My custom explorer.", body="Custom explore prompt.",
    )
    explore = find_agent(ws, "explore")
    assert explore.source == "project"
    assert explore.description == "My custom explorer."


def test_precedence_project_over_global(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(ws / ".marim" / "agents", "dup", description="project version")
    _make_agent(
        isolated_home / "xdg" / "marim" / "agents", "dup",
        description="global version",
    )
    dup = find_agent(ws, "dup")
    assert dup.source == "project"
    assert dup.description == "project version"


def test_discover_skips_malformed(isolated_home):
    ws = isolated_home / "ws"
    d = ws / ".marim" / "agents"
    d.mkdir(parents=True)
    (d / "bad.md").write_text("no frontmatter\n", encoding="utf-8")
    (d / "nodesc.md").write_text("---\nname: nodesc\n---\nbody\n", encoding="utf-8")
    names = {a.name for a in discover_agents(ws)}
    assert "bad" not in names
    assert "nodesc" not in names


def test_discover_skips_name_file_mismatch(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(ws / ".marim" / "agents", "real", fm_name="other")
    assert find_agent(ws, "real") is None


def test_discover_skips_invalid_name(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(ws / ".marim" / "agents", "Bad Name", fm_name="Bad Name")
    assert find_agent(ws, "Bad Name") is None


def test_find_agent_unknown_returns_none(isolated_home):
    ws = isolated_home / "ws"
    assert find_agent(ws, "ghost") is None


def test_agents_index_text_lists_all(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(ws / ".marim" / "agents", "reviewer", description="Reviews diffs.")
    text = agents_index_text(discover_agents(ws))
    assert "explore" in text
    assert "general" in text
    assert "reviewer — Reviews diffs." in text


def test_effective_tools_drops_gated_without_auto():
    general = AgentDef("general", "d", "p", SUBAGENT_TOOLS, "built-in")
    # Without auto, only the mutating (gated) tools are dropped — local reads and
    # network tools survive.
    assert effective_tools(general, allow_gated=False) == READ_TOOLS | NET_TOOLS
    assert effective_tools(general, allow_gated=True) == SUBAGENT_TOOLS


def test_effective_tools_keeps_net_but_drops_gated_without_auto():
    """Network tools aren't gated by mode — only workspace mutators are."""
    defn = AgentDef(
        "net-writer", "d", "p",
        frozenset({"read_file", "web_search", "fetch_url", "write_file"}), "p",
    )
    assert effective_tools(defn, allow_gated=False) == frozenset(
        {"read_file", "web_search", "fetch_url"}
    )
    assert effective_tools(defn, allow_gated=True) == defn.tools


def test_tool_groups_are_disjoint():
    """The three trust tiers must not overlap, or the boundaries blur."""
    assert not (READ_TOOLS & NET_TOOLS)
    assert not (READ_TOOLS & GATED_TOOLS)
    assert not (NET_TOOLS & GATED_TOOLS)
    assert SUBAGENT_TOOLS == READ_TOOLS | NET_TOOLS | GATED_TOOLS


def test_effective_tools_read_only_agent_unaffected():
    explore = AgentDef("explore", "d", "p", READ_TOOLS, "built-in")
    assert effective_tools(explore, allow_gated=True) == READ_TOOLS
    assert effective_tools(explore, allow_gated=False) == READ_TOOLS


def test_effective_tools_keeps_only_known_gated():
    """A custom agent granting one gated tool keeps just that one in auto mode."""
    writer = AgentDef("writer", "d", "p", frozenset({"read_file", "write_file"}), "p")
    assert effective_tools(writer, allow_gated=True) == frozenset(
        {"read_file", "write_file"}
    )
    assert effective_tools(writer, allow_gated=False) == frozenset({"read_file"})
    assert GATED_TOOLS  # sanity: the gated set is non-empty


def test_subagent_instructions_includes_prompt_and_workspace():
    defn = AgentDef("explore", "d", "Investigate carefully.", READ_TOOLS, "built-in")
    text = subagent_instructions(defn, Path("/work/space"))
    assert "Investigate carefully." in text
    assert "/work/space" in text


def test_discover_agents_caches_and_skips_reparse(isolated_home, monkeypatch):
    """A second discovery with nothing changed on disk is served from cache — the
    expensive YAML read+parse doesn't run again."""
    from marim_harness.workspace import agents as agents_mod

    ws = isolated_home / "ws"
    _make_agent(ws / ".marim" / "agents", "probe")

    calls = {"n": 0}
    real = agents_mod._parse_agent

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(agents_mod, "_parse_agent", counting)
    discover_agents(ws)
    first = calls["n"]
    assert first >= 1
    discover_agents(ws)
    assert calls["n"] == first  # no re-parse on the second call


def test_discover_agents_reflects_added_file(isolated_home):
    """Adding an agent file invalidates the cache — the new agent shows up."""
    ws = isolated_home / "ws"
    root = ws / ".marim" / "agents"
    _make_agent(root, "first")
    assert "second" not in {a.name for a in discover_agents(ws)}
    _make_agent(root, "second")
    assert "second" in {a.name for a in discover_agents(ws)}


def test_discover_agents_reflects_edited_file(isolated_home):
    """Editing an agent file's contents invalidates the cache — the change is
    reflected, not served stale from a prior discovery."""
    ws = isolated_home / "ws"
    root = ws / ".marim" / "agents"
    _make_agent(root, "probe", description="The first description, fairly long.")
    d1 = next(a for a in discover_agents(ws) if a.name == "probe").description
    assert "first" in d1
    _make_agent(root, "probe", description="Second.")
    d2 = next(a for a in discover_agents(ws) if a.name == "probe").description
    assert "Second" in d2


def test_researcher_is_builtin(isolated_home):
    ws = isolated_home / "ws"
    agent = find_agent(ws, "researcher")
    assert agent is not None
    assert agent.source == "builtin"
    assert agent.backend == "native"
    assert agent.tools == frozenset(
        {"web_search", "fetch_url", "read_file", "glob", "grep", "tree"}
    )
    # Read-only and cannot recurse.
    assert "spawn_agent" not in agent.tools
    assert GATED_TOOLS.isdisjoint(agent.tools)


def test_project_agent_shadows_builtin_researcher(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(ws / ".marim" / "agents", "researcher", description="Custom override.")
    agent = find_agent(ws, "researcher")
    assert agent.source == "project"
    assert agent.description == "Custom override."
