import inspect
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

from marim_harness import BuilderError, HarnessBuilder
from marim_harness.hooks import HookRunner
from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.workspace.agents import AgentDef


def _tool_names(harness) -> set[str]:
    return set(harness.agent._function_toolset.tools.keys())


def _instruction_closure_names(harness) -> set[str]:
    # See test_config_seams.test_global_instructions_gate for how this
    # reaches into pydantic-ai's Agent._instructions (no public accessor).
    return {
        fn.__name__ for fn in harness.agent._instructions  # noqa: SLF001
        if callable(fn)
    }


def test_deps_is_a_top_level_export():
    # Every custom tool's first parameter is RunContext[Deps], so embedders
    # import Deps constantly — it must not require knowing the runtime
    # package layout (docs/embedding.md's example uses this import).
    import marim_harness

    assert marim_harness.Deps is Deps
    assert "Deps" in dir(marim_harness)


def test_bare_build_defaults(tmp_path: Path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()
    assert _tool_names(h) == {"read_file", "glob", "tree", "grep",
                              "write_file", "edit_file"}
    assert h.deps.workspace.mode is Mode.auto
    assert h.session.store is None          # in-memory: nothing hits XDG
    assert h.lsp is None
    assert h.provider.lsp_toolset() is None


def test_with_methods_enable_groups(tmp_path: Path):
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_bash().with_net().with_tasks().build())
    names = _tool_names(h)
    assert {"bash", "web_search", "fetch_url", "update_tasks"} <= names
    assert "remember" not in names          # memory still off


def test_custom_tool_registers(tmp_path: Path):
    def deploy(ctx: RunContext[Deps], target: str) -> str:
        """Deploy the app to `target`."""
        return f"deployed {target}"

    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_tool(deploy, requires_approval=True).build())
    assert "deploy" in _tool_names(h)


def test_with_subagent_implies_spawn(tmp_path: Path):
    d = AgentDef(name="rev", description="d", prompt="p",
                 tools=frozenset({"read_file"}), source="programmatic")
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).with_subagent(d).build()
    assert "spawn_agent" in _tool_names(h)
    assert h.subagents._resolve_agent("rev") is d


def test_build_reports_all_problems_at_once(tmp_path: Path):
    def read_file(ctx: RunContext[Deps]) -> str:   # collides with builtin
        """Shadow."""
        return ""

    bad_agent = AgentDef(name="x", description="d", prompt="p",
                         tools=frozenset({"nonexistent_tool"}), source="programmatic")
    with pytest.raises(BuilderError) as exc:
        (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_tool(read_file).with_subagent(bad_agent).build())
    msg = str(exc.value)
    assert "read_file" in msg and "nonexistent_tool" in msg   # both reported


def test_build_twice_raises(tmp_path: Path):
    b = HarnessBuilder(workspace=tmp_path, model=TestModel())
    b.build()
    with pytest.raises(RuntimeError):
        b.build()


def test_string_model_unresolvable_is_builder_error(tmp_path: Path):
    with pytest.raises(BuilderError):
        HarnessBuilder(workspace=tmp_path, model="no-such-provider:xyz").build()


def test_sessions_opt_in_writes_under_given_dir(tmp_path: Path):
    sessions = tmp_path / "sess"
    h = (HarnessBuilder(workspace=tmp_path / "ws", model=TestModel())
         .with_sessions(dir=sessions).build())
    assert h.session.store is not None
    assert sessions.exists()


def test_memory_and_skills_knobs_land_on_workspace(tmp_path: Path):
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_memory(dir=tmp_path / "mem")
         .with_skills(dirs=[tmp_path / "sk"]).build())
    assert h.deps.workspace.memory_root == tmp_path / "mem"
    assert h.deps.workspace.skill_dirs == (tmp_path / "sk",)
    assert {"remember", "recall", "activate_skill"} <= _tool_names(h)


def test_subagent_lsp_tool_without_lsp_tools_is_builder_error(tmp_path: Path):
    d = AgentDef(name="nav", description="d", prompt="p",
                 tools=frozenset({"goto_definition"}), source="programmatic")
    with pytest.raises(BuilderError) as exc:
        HarnessBuilder(workspace=tmp_path, model=TestModel()).with_subagent(d).build()
    assert "goto_definition" in str(exc.value)


def test_subagent_lsp_tool_with_lsp_tools_succeeds(tmp_path: Path):
    d = AgentDef(name="nav", description="d", prompt="p",
                 tools=frozenset({"goto_definition"}), source="programmatic")
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_lsp(tools=True).with_subagent(d).build())
    assert h.subagents._resolve_agent("nav") is d


def test_with_lsp_disabled_folds_tools_off_even_when_requested(tmp_path: Path):
    """with_lsp(enabled=False, tools=True) must still end up with tools off —
    the manager switch always wins over the tools switch (see with_lsp's
    docstring: "enabled=False is the escape hatch ... without reaching into
    builder privates")."""
    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_lsp(enabled=False, tools=True).build())
    assert h.lsp is None
    assert h.provider.lsp_toolset() is None


# -- Finding 1: instruction closures gated by tool groups --------------------
#
# register_instructions used to gate only the global-instructions closure;
# _agent_index/_skill_index/_memory_indexes/_plugin_instructions registered
# unconditionally regardless of which tool groups the builder actually
# loaded. discover_agents ALWAYS returns the built-in agents (see
# workspace/agents.py), so a bare build's _agent_index (if it registered)
# would unconditionally advertise spawn_agent — a tool a bare build never
# grants.

def test_bare_build_excludes_group_gated_instruction_closures(tmp_path: Path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()
    names = _instruction_closure_names(h)
    # Gated on tool groups the bare build doesn't load (spawn/skills/memory).
    assert "_agent_index" not in names
    assert "_skill_index" not in names
    assert "_memory_indexes" not in names
    # Gated on global_instructions (bare build never reaches into the
    # embedding user's ~/.config/marim).
    assert "_global_instructions" not in names
    assert "_plugin_instructions" not in names
    # Ungated closures still register.
    assert "_project_instructions" in names
    assert "_mcp_index" in names
    assert "_memory_policy" in names


def test_with_defaults_includes_group_gated_instruction_closures(tmp_path: Path):
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).with_defaults().build()
    names = _instruction_closure_names(h)
    assert {"_agent_index", "_skill_index", "_memory_indexes",
            "_global_instructions", "_plugin_instructions"} <= names


def test_bare_build_instructions_never_mention_ungranted_tools(tmp_path: Path):
    """Behavioral (not just identity) check: evaluate every registered
    synchronous instruction closure on a bare build and confirm none of them
    mention spawn_agent/activate_skill — the tools _agent_index/_skill_index
    would have advertised had they registered. (The async _tool_catalog
    closure is skipped: it needs a live mcp_manager RunContext dance that's
    impractical to fake here, and it never mentions these tool names anyway
    — it just reports search knobs.)"""
    h = HarnessBuilder(workspace=tmp_path, model=TestModel()).build()

    class _Ctx:
        deps = h.deps

    rendered = []
    for fn in h.agent._instructions:  # noqa: SLF001
        if not callable(fn) or inspect.iscoroutinefunction(fn):
            continue
        rendered.append(fn(_Ctx()) or "")
    text = "\n".join(rendered)
    assert "spawn_agent" not in text
    assert "activate_skill" not in text


# -- Finding 2: custom-tool collision check misses LSP and forge names -------

def test_custom_tool_collides_with_lsp_name_when_lsp_tools_on(tmp_path: Path):
    def goto_definition(ctx: RunContext[Deps]) -> str:
        """Shadows the LSP navigation tool."""
        return ""

    with pytest.raises(BuilderError) as exc:
        (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_lsp(tools=True).with_tool(goto_definition).build())
    assert "goto_definition" in str(exc.value)


def test_custom_tool_named_like_lsp_tool_ok_without_lsp_tools(tmp_path: Path):
    def goto_definition(ctx: RunContext[Deps]) -> str:
        """Not a collision: LSP tools are off, so the name is free."""
        return ""

    h = (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_tool(goto_definition).build())
    assert "goto_definition" in _tool_names(h)


def test_custom_tool_collides_with_forge_name_under_with_forge(tmp_path: Path):
    def create_pr(ctx: RunContext[Deps]) -> str:
        """Shadows the forge create_pr tool."""
        return ""

    with pytest.raises(BuilderError) as exc:
        (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_forge(object()).with_tool(create_pr).build())
    assert "create_pr" in str(exc.value)


# -- Finding 4: with_hooks silently discarded when with_deps set -------------

def test_with_hooks_and_with_deps_together_is_builder_error(tmp_path: Path):
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    with pytest.raises(BuilderError) as exc:
        (HarnessBuilder(workspace=tmp_path, model=TestModel())
         .with_hooks(HookRunner({}))
         .with_deps(deps)
         .build())
    msg = str(exc.value)
    assert "with_hooks" in msg and "with_deps" in msg


def test_builder_resolves_workspace_path(tmp_path, monkeypatch):
    """An embedder-supplied relative or symlinked workspace must normalize to
    one canonical root at construction: every workspace-keyed artifact
    (session-storage slug, scratchpad slug, checkpoint refs) hashes
    str(root), and SessionManager resolves its own copy — an unresolved
    Deps root would silently mis-key those artifacts against each other."""
    real = tmp_path / "real-ws"
    real.mkdir()
    link = tmp_path / "link-ws"
    link.symlink_to(real)
    via_link = HarnessBuilder(workspace=link, model=TestModel()).build()
    assert via_link.deps.workspace.root == real.resolve()

    monkeypatch.chdir(tmp_path)
    via_relative = HarnessBuilder(workspace=Path("real-ws"), model=TestModel()).build()
    assert via_relative.deps.workspace.root == real.resolve()
