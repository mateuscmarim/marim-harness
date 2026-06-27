from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.runtime.deps import Deps
from marim_harness.tools.provider import (
    GATED_TOOLS,
    READ_TOOLS,
    SUBAGENT_TOOLS,
    BuiltinToolProvider,
)
from marim_harness.workspace.agents import (
    AgentDef,
    cap_subagent_output,
    compose_subagent_task,
    subagent_instructions,
)


def test_compose_task_without_extras_is_unchanged():
    assert compose_subagent_task("investigate the auth flow") == "investigate the auth flow"


def test_compose_task_treats_blank_extras_as_absent():
    # A model may pass empty strings for fields it has nothing to say about;
    # those must not produce empty labelled sections.
    out = compose_subagent_task("do it", returns="", constraints="   ", context="")
    assert out == "do it"


def test_compose_task_includes_only_the_sections_given():
    out = compose_subagent_task("do it", constraints="read-only; stay in src/auth")
    assert "do it" in out
    assert "read-only; stay in src/auth" in out
    assert "Constraints" in out
    assert "Context" not in out
    assert "Return" not in out


def test_compose_task_orders_task_context_constraints_return():
    out = compose_subagent_task(
        "find the riskiest call sites",
        returns="a ranked list of file:line with one-line reasons",
        constraints="do not modify anything",
        context="auth was refactored last week in src/auth/session.py",
    )
    # The task leads; the output contract, boundaries, and background each get a
    # labelled section, in a stable order: task, context, constraints, return.
    assert out.index("find the riskiest call sites") < out.index("Context")
    assert out.index("Context") < out.index("Constraints")
    assert out.index("Constraints") < out.index("Return")
    assert "ranked list of file:line" in out
    assert "do not modify anything" in out
    assert "refactored last week" in out


def _defn() -> AgentDef:
    return AgentDef(
        name="explore",
        description="read-only",
        prompt="You are a sub-agent.",
        tools=frozenset(),
        source="built-in",
    )


def test_instructions_without_budget_have_no_budget_text(tmp_path):
    text = subagent_instructions(_defn(), tmp_path)
    assert "budget" not in text.lower()


def test_instructions_with_budget_state_target_and_lead_with_conclusion(tmp_path):
    text = subagent_instructions(_defn(), tmp_path, max_output_chars=500)
    # The original role survives, and the budget is a SOFT target the sub-agent
    # distills toward: it names the number, says lead-with-conclusion, and says
    # summarize rather than get truncated.
    assert "You are a sub-agent." in text
    assert "500" in text
    assert "conclusion" in text.lower()
    assert "summar" in text.lower()


def test_cap_under_budget_returns_output_unchanged():
    out = "short report"
    text, spill = cap_subagent_output(out, 500, "report.txt")
    assert text == out
    assert spill is None


def test_cap_none_is_a_passthrough():
    out = "x" * 10_000
    text, spill = cap_subagent_output(out, None, "report.txt")
    assert text == out
    assert spill is None


def test_cap_over_budget_spills_full_and_returns_pointer_within_budget():
    out = "CONCLUSION first. " + "filler detail. " * 500
    text, spill = cap_subagent_output(out, 200, ".marim/sub/abc.md")
    # Full output is handed back for the caller to spill to the file.
    assert spill == out
    # What the main agent receives stays within its budget and points at the file.
    assert len(text) <= 200
    assert ".marim/sub/abc.md" in text
    # The head — where the conclusion was front-loaded — is preserved.
    assert text.startswith("CONCLUSION first.")


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
    assert names >= GATED_TOOLS


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
