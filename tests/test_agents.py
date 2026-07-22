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
    # These tests assert on project-local `.marim/agents`, so run them as a
    # TRUSTED workspace — project agents are otherwise gated (see the untrusted
    # tests below and workspace.agents._project_trusted).
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
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
    # network tools survive (ask mode: the user still approves the main agent's
    # own net calls, and a spawn's grant follows the definition).
    assert effective_tools(general, allow_gated=False, allow_net=True) == (
        READ_TOOLS | NET_TOOLS
    )
    assert effective_tools(general, allow_gated=True, allow_net=True) == SUBAGENT_TOOLS


def test_effective_tools_keeps_net_but_drops_gated_without_auto():
    """With allow_net, network tools ride the definition — only workspace
    mutators are stripped outside auto."""
    defn = AgentDef(
        "net-writer", "d", "p",
        frozenset({"read_file", "web_search", "fetch_url", "write_file"}), "p",
    )
    assert effective_tools(defn, allow_gated=False, allow_net=True) == frozenset(
        {"read_file", "web_search", "fetch_url"}
    )
    assert effective_tools(defn, allow_gated=True, allow_net=True) == defn.tools


def test_effective_tools_strips_net_when_disallowed():
    """Plan mode's egress boundary: without allow_net, web_search/fetch_url are
    stripped no matter what the definition grants — a spawn must not become an
    unapproved exfiltration path when the main agent's own net tools are denied
    (see runtime/permissions._plan_decision)."""
    defn = AgentDef(
        "net-writer", "d", "p",
        frozenset({"read_file", "web_search", "fetch_url", "write_file"}), "p",
    )
    assert effective_tools(defn, allow_gated=False, allow_net=False) == frozenset(
        {"read_file"}
    )
    # allow_gated without allow_net is not a combination any current mode
    # produces (plan implies no gated tools either), but the axes are
    # independent — each strips only its own set.
    assert effective_tools(defn, allow_gated=True, allow_net=False) == frozenset(
        {"read_file", "write_file"}
    )


def test_effective_tools_net_strip_removes_whole_net_set():
    explore = AgentDef("explore", "d", "p", READ_TOOLS | NET_TOOLS, "built-in")
    stripped = effective_tools(explore, allow_gated=False, allow_net=False)
    assert stripped == READ_TOOLS
    assert not (stripped & NET_TOOLS)


def test_tool_groups_are_disjoint():
    """The three trust tiers must not overlap, or the boundaries blur."""
    assert not (READ_TOOLS & NET_TOOLS)
    assert not (READ_TOOLS & GATED_TOOLS)
    assert not (NET_TOOLS & GATED_TOOLS)
    assert SUBAGENT_TOOLS == READ_TOOLS | NET_TOOLS | GATED_TOOLS


def test_effective_tools_read_only_agent_unaffected():
    explore = AgentDef("explore", "d", "p", READ_TOOLS, "built-in")
    assert effective_tools(explore, allow_gated=True, allow_net=True) == READ_TOOLS
    assert effective_tools(explore, allow_gated=False, allow_net=False) == READ_TOOLS


def test_effective_tools_keeps_only_known_gated():
    """A custom agent granting one gated tool keeps just that one in auto mode."""
    writer = AgentDef("writer", "d", "p", frozenset({"read_file", "write_file"}), "p")
    assert effective_tools(writer, allow_gated=True, allow_net=True) == frozenset(
        {"read_file", "write_file"}
    )
    assert effective_tools(writer, allow_gated=False, allow_net=True) == frozenset(
        {"read_file"}
    )
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


# -- project-local trust gate -------------------------------------------------
# A cloned untrusted repo's `.marim/agents` must NOT load: a custom agent def
# chooses a spawn's system prompt, its tool grants (up to bash in auto mode), and
# its backend/model — arming a sub-agent before consent. The built-ins always
# remain (they aren't on disk), so only custom project agents are gated.


@pytest.fixture
def untrusted_home(tmp_path, monkeypatch):
    """Like isolated_home but WITHOUT the trust flag — the default, untrusted
    posture a freshly cloned repo runs under."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MARIM_TRUST_PROJECT_HOOKS", raising=False)
    return tmp_path


def test_untrusted_project_agent_not_loaded(untrusted_home):
    ws = untrusted_home / "ws"
    _make_agent(ws / ".marim" / "agents", "sneaky", description="Armed.")
    names = {a.name for a in discover_agents(ws)}
    assert "sneaky" not in names
    # Built-ins are unaffected by the gate.
    assert {"explore", "general"} <= names
    assert find_agent(ws, "sneaky") is None


def test_untrusted_project_agent_excluded_from_index(untrusted_home):
    ws = untrusted_home / "ws"
    _make_agent(ws / ".marim" / "agents", "sneaky", description="Armed desc.")
    assert "sneaky" not in agents_index_text(discover_agents(ws))


def test_trusted_project_agent_loaded(untrusted_home, monkeypatch):
    ws = untrusted_home / "ws"
    _make_agent(ws / ".marim" / "agents", "reviewer", description="Legit.")
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    assert find_agent(ws, "reviewer") is not None


def test_explicit_trust_project_param_overrides_env(untrusted_home, monkeypatch):
    ws = untrusted_home / "ws"
    _make_agent(ws / ".marim" / "agents", "reviewer", description="Legit.")
    # Explicit True loads it even with the env unset...
    assert find_agent(ws, "reviewer", trust_project=True) is not None
    # ...and explicit False gates it even when the env would trust it.
    monkeypatch.setenv("MARIM_TRUST_PROJECT_HOOKS", "1")
    assert find_agent(ws, "reviewer", trust_project=False) is None


def test_subagent_instructions_mention_scratchpad():
    defn = AgentDef("explore", "d", "Investigate.", READ_TOOLS, "built-in")
    scratch = Path("/tmp/marim-1/proj-abc/sess/scratchpad")
    text = subagent_instructions(defn, Path("/work/space"), scratchpad=scratch)
    assert str(scratch) in text


def test_subagent_instructions_omit_scratchpad_when_none():
    defn = AgentDef("explore", "d", "Investigate.", READ_TOOLS, "built-in")
    text = subagent_instructions(defn, Path("/work/space"))
    assert "scratchpad" not in text.lower()


def test_subagent_instructions_scratchpad_writable_keeps_use_it_wording():
    defn = AgentDef("explore", "d", "Investigate.", READ_TOOLS, "built-in")
    scratch = Path("/tmp/marim-1/proj-abc/sess/scratchpad")
    text = subagent_instructions(
        defn, Path("/work/space"), scratchpad=scratch, scratchpad_writable=True
    )
    assert str(scratch) in text
    assert "Use it" in text


def test_subagent_instructions_scratchpad_read_only_drops_write_wording():
    defn = AgentDef("explore", "d", "Investigate.", READ_TOOLS, "built-in")
    scratch = Path("/tmp/marim-1/proj-abc/sess/scratchpad")
    text = subagent_instructions(
        defn, Path("/work/space"), scratchpad=scratch, scratchpad_writable=False
    )
    assert str(scratch) in text
    assert "Use it" not in text
    assert "cannot write" in text.lower()


def test_subagent_instructions_omit_scratchpad_when_none_regardless_of_writable():
    defn = AgentDef("explore", "d", "Investigate.", READ_TOOLS, "built-in")
    text = subagent_instructions(
        defn, Path("/work/space"), scratchpad=None, scratchpad_writable=False
    )
    assert "scratchpad" not in text.lower()


def test_parse_agent_reads_valid_tier(tmp_path):
    from marim_harness.workspace.agents import _parse_agent
    p = tmp_path / "researcher.md"
    p.write_text(
        "---\ndescription: deep read\ntier: med\n---\nDo research.\n",
        encoding="utf-8",
    )
    defn = _parse_agent("project", p)
    assert defn is not None
    assert defn.tier == "med"


def test_parse_agent_drops_invalid_tier(tmp_path):
    from marim_harness.workspace.agents import _parse_agent
    p = tmp_path / "bad.md"
    p.write_text(
        "---\ndescription: x\ntier: enormous\n---\nBody.\n", encoding="utf-8"
    )
    defn = _parse_agent("project", p)
    assert defn is not None
    assert defn.tier is None


def test_parse_agent_tier_absent_is_none(tmp_path):
    from marim_harness.workspace.agents import _parse_agent
    p = tmp_path / "plain.md"
    p.write_text("---\ndescription: x\n---\nBody.\n", encoding="utf-8")
    defn = _parse_agent("project", p)
    assert defn is not None
    assert defn.tier is None
