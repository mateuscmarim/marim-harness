from pathlib import Path

import pytest

from marim_harness.agents import (
    AgentDef,
    agent_roots,
    agents_index_text,
    discover_agents,
    effective_tools,
    find_agent,
    subagent_instructions,
)
from marim_harness.tools.provider import GATED_TOOLS, READ_TOOLS, SUBAGENT_TOOLS


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
    ws = tmp_path / "ws"
    roots = agent_roots(ws)
    sources = [s for s, _ in roots]
    assert sources == ["project", "project/.claude", "global", "global/.claude"]
    assert roots[0][1] == ws / ".marim" / "agents"
    assert roots[1][1] == ws / ".claude" / "agents"


def test_builtins_always_present(isolated_home):
    ws = isolated_home / "ws"
    names = {a.name for a in discover_agents(ws)}
    assert {"explore", "general"} <= names


def test_builtin_explore_is_read_only(isolated_home):
    ws = isolated_home / "ws"
    explore = find_agent(ws, "explore")
    assert explore is not None
    assert explore.tools == READ_TOOLS
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
    assert find_agent(ws, "notes").tools == READ_TOOLS


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
    assert effective_tools(general, allow_gated=False) == READ_TOOLS
    assert effective_tools(general, allow_gated=True) == SUBAGENT_TOOLS


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
