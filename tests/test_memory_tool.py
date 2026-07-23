from pathlib import Path
from types import SimpleNamespace

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider
from tests.conftest import _make_deps


def _agent() -> Agent:
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register(agent)
    return agent


def _call_remember(args: dict):
    state = {}

    def model(messages, info):
        if not state:
            state["called"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name="remember", args=args)])
        return ModelResponse(parts=[TextPart(content="done")])

    return FunctionModel(model)


def test_remember_saves_to_project_scope(tmp_path: Path):
    agent = _agent()
    args = {
        "title": "Build tool",
        "description": "uses uv",
        "body": "Run uv for everything.",
        "scope": "project",
        "type": "project",
    }
    with agent.override(model=_call_remember(args)):
        agent.run_sync("remember this", deps=_make_deps(tmp_path, mode=Mode.ask))
    saved = tmp_path / ".marim" / "memory" / "build-tool.md"
    assert saved.exists()
    assert "Run uv for everything." in saved.read_text()
    assert "build-tool.md" in (tmp_path / ".marim" / "memory" / "MEMORY.md").read_text()


def test_remember_saves_to_global_scope(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    agent = _agent()
    args = {
        "title": "My name",
        "description": "user is Mateus",
        "body": "Call me Mateus.",
        "scope": "global",
        "type": "user",
    }
    with agent.override(model=_call_remember(args)):
        agent.run_sync("remember this", deps=_make_deps(tmp_path, mode=Mode.ask))
    saved = tmp_path / "cfg" / "marim" / "memory" / "my-name.md"
    assert saved.exists()
    # global save must not have touched the project dir
    assert not (tmp_path / ".marim").exists()


def _call_recall(args: dict):
    state = {}
    captured = {}

    def model(messages, info):
        if not state:
            state["called"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name="recall", args=args)])
        # Surface the tool return so the test can assert on it.
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart":
                    captured["ret"] = str(p.content)
        return ModelResponse(parts=[TextPart(content=captured.get("ret", ""))])

    return FunctionModel(model), captured


def _tool_schema(tool_name: str) -> dict:
    """Build the agent, run one no-op turn, and return the JSON schema the model
    sees for ``tool_name``'s parameters."""
    agent = _agent()
    m = TestModel(call_tools=[])
    with agent.override(model=m):
        agent.run_sync("hi", deps=Deps(workspace=WorkspaceConfig(root=Path("."))))
    tools = {t.name: t for t in m.last_model_request_parameters.function_tools}
    return tools[tool_name].parameters_json_schema


def test_recall_scope_is_constrained_to_two_values():
    schema = _tool_schema("recall")
    scope = schema["properties"]["scope"]
    assert scope.get("enum") == ["project", "global"]


def test_remember_scope_is_constrained_to_two_values():
    schema = _tool_schema("remember")
    scope = schema["properties"]["scope"]
    assert scope.get("enum") == ["project", "global"]


def test_recall_reads_project_memory_body(tmp_path: Path):
    from marim_harness.workspace import memory

    memory.save_memory(
        memory.project_scope(tmp_path), name="My name", description="hook",
        mem_type="user", body="The user is Mateus.", title="My name",
    )
    agent = _agent()
    model, captured = _call_recall({"name": "My name", "scope": "project"})
    with agent.override(model=model):
        agent.run_sync("recall", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert "The user is Mateus." in captured["ret"]


def test_recall_reads_global_memory_outside_workspace(tmp_path: Path, monkeypatch):
    from marim_harness.workspace import memory

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    memory.save_memory(
        memory.global_scope(), name="My name", description="hook",
        mem_type="user", body="The user is Mateus.", title="My name",
    )
    agent = _agent()
    model, captured = _call_recall({"name": "My name", "scope": "global"})
    with agent.override(model=model):
        agent.run_sync("recall", deps=_make_deps(tmp_path, mode=Mode.ask))
    # Global memory lives outside the workspace; recall must still reach it.
    assert "The user is Mateus." in captured["ret"]


def test_remember_is_not_an_approval_gated_tool(tmp_path: Path):
    """The remember tool writes only inside marim's memory dirs, so it must run
    without an approval prompt (unlike write_file/edit_file/bash)."""
    agent = _agent()
    args = {"title": "X", "description": "d", "body": "b"}
    # No DeferredToolRequests handling here; if it required approval the run
    # would not complete the save.
    with agent.override(model=_call_remember(args)):
        agent.run_sync("go", deps=_make_deps(tmp_path, mode=Mode.ask))
    assert (tmp_path / ".marim" / "memory" / "x.md").exists()


def test_forget_deletes_memory_and_index_line(tmp_path: Path):
    from marim_harness.tools.memory_tools import forget
    from marim_harness.workspace import memory

    sc = memory.project_scope(tmp_path)
    memory.save_memory(sc, name="Build tool", description="d",
                       mem_type="project", body="b", title="Build tool")
    ctx = SimpleNamespace(
        deps=SimpleNamespace(workspace=SimpleNamespace(memory_root=None, root=tmp_path))
    )
    result = forget(ctx, name="Build tool")
    assert "deleted" in result.lower()
    assert not (sc.root / "build-tool.md").exists()
    assert "build-tool.md" not in (sc.root / "MEMORY.md").read_text()


def test_forget_missing_memory_returns_notice(tmp_path: Path):
    from marim_harness.tools.memory_tools import forget

    ctx = SimpleNamespace(
        deps=SimpleNamespace(workspace=SimpleNamespace(memory_root=None, root=tmp_path))
    )
    result = forget(ctx, name="never-saved")
    assert "no project memory" in result.lower()


def test_forget_requires_approval():
    """Pins requires_approval=True on the registration itself (provider.py) —
    deletion is the one irreversible memory operation, so unlike remember/recall
    it must route through resolve_approvals (ask prompts, plan denies)."""
    agent = _agent()
    tool = agent._function_toolset.tools["forget"]
    assert tool.requires_approval is True


def test_forget_scope_is_constrained_to_two_values():
    schema = _tool_schema("forget")
    scope = schema["properties"]["scope"]
    assert scope.get("enum") == ["project", "global"]


def test_forget_is_not_grantable_to_subagents():
    from marim_harness.tools.names import SUBAGENT_TOOLS, TOOL_GROUPS

    assert "forget" not in SUBAGENT_TOOLS
    # But it IS part of the builder's memory composition group.
    assert "forget" in TOOL_GROUPS["memory"]
