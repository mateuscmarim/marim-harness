from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.deps import Deps
from marim_harness.tools.provider import BuiltinToolProvider


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
        agent.run_sync("remember this", deps=Deps(workspace_root=tmp_path))
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
        agent.run_sync("remember this", deps=Deps(workspace_root=tmp_path))
    saved = tmp_path / "cfg" / "marim" / "memory" / "my-name.md"
    assert saved.exists()
    # global save must not have touched the project dir
    assert not (tmp_path / ".marim").exists()


def test_remember_is_not_an_approval_gated_tool(tmp_path: Path):
    """The remember tool writes only inside marim's memory dirs, so it must run
    without an approval prompt (unlike write_file/edit_file/bash)."""
    agent = _agent()
    args = {"title": "X", "description": "d", "body": "b"}
    # No DeferredToolRequests handling here; if it required approval the run
    # would not complete the save.
    with agent.override(model=_call_remember(args)):
        agent.run_sync("go", deps=Deps(workspace_root=tmp_path))
    assert (tmp_path / ".marim" / "memory" / "x.md").exists()
