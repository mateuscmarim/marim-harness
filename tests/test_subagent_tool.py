from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.deps import Deps
from marim_harness.tools.provider import (
    GATED_TOOLS,
    READ_TOOLS,
    SUBAGENT_TOOLS,
    BuiltinToolProvider,
)


def _tool_names(agent: Agent, deps: Deps) -> set[str]:
    """The tool names a model would see for ``agent`` — capture them by running a
    TestModel that calls nothing and inspecting the request parameters."""
    m = TestModel(call_tools=[])
    with agent.override(model=m):
        agent.run_sync("go", deps=deps)
    return {t.name for t in m.last_model_request_parameters.function_tools}


def test_register_subagent_read_only(tmp_path):
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register_subagent(agent, READ_TOOLS)
    assert _tool_names(agent, Deps(workspace_root=tmp_path)) == set(READ_TOOLS)


def test_register_subagent_full_set(tmp_path):
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register_subagent(agent, SUBAGENT_TOOLS)
    names = _tool_names(agent, Deps(workspace_root=tmp_path))
    assert names == set(SUBAGENT_TOOLS)
    assert GATED_TOOLS <= names


def test_register_subagent_ignores_unknown_and_spawn(tmp_path):
    """spawn_agent and the memory/task tools are main-agent only; they are not in
    the sub-agent registry, so granting them is a no-op (no recursion)."""
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register_subagent(
        agent, {"read_file", "spawn_agent", "update_tasks", "bogus"}
    )
    assert _tool_names(agent, Deps(workspace_root=tmp_path)) == {"read_file"}


def _call_once(tool_name: str, args: dict):
    """A FunctionModel that calls a tool once, then echoes its return."""
    state: dict = {}
    captured: dict = {}

    def model(messages, info):
        if not state:
            state["called"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args)])
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart":
                    captured["ret"] = str(p.content)
        return ModelResponse(parts=[TextPart(content=captured.get("ret", ""))])

    return FunctionModel(model), captured


def test_subagent_gated_tools_run_without_approval(tmp_path):
    """A sub-agent's write tools are registered plain: they execute in one run
    with no deferred-approval round."""
    agent = Agent(TestModel(), deps_type=Deps)
    BuiltinToolProvider().register_subagent(agent, {"write_file"})
    model, captured = _call_once(
        "write_file", {"path": "out.txt", "content": "hello sub"}
    )
    with agent.override(model=model):
        result = agent.run_sync("go", deps=Deps(workspace_root=tmp_path))
    assert (tmp_path / "out.txt").read_text() == "hello sub"
    # The run produced a plain string output, not a DeferredToolRequests.
    assert isinstance(result.output, str)
