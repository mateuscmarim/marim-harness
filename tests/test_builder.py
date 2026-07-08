from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

from marim_harness import BuilderError, HarnessBuilder
from marim_harness.runtime.deps import Deps
from marim_harness.runtime.permissions import Mode
from marim_harness.workspace.agents import AgentDef


def _tool_names(harness) -> set[str]:
    return set(harness.agent._function_toolset.tools.keys())


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
