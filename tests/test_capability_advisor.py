"""The exported Advisor capability. TestModel/FunctionModel only — no live
providers. The main-agent FunctionModel scripts count ToolReturnParts to
decide whether to call the advisor tool again or finish."""

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.advisor import ADVISOR_GUIDANCE
from marim_harness.capabilities import Advisor


def _consult_once_main():
    """A main model that calls the advisor tool once, then finishes."""

    def fn(messages, info):
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("advisor", {})])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


def _advisor_returns(text):
    return TestModel(custom_output_text=text)


def _advisor_tool_returns(result):
    return [
        p
        for m in result.all_messages()
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == "advisor"
    ]


@pytest.mark.anyio
async def test_capability_exposes_tool_and_guidance():
    seen = {}

    def capture(messages, info):
        seen["tools"] = [t.name for t in info.function_tools]
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(
        FunctionModel(capture),
        capabilities=[Advisor(model=_advisor_returns("advice"))],
    )
    result = await agent.run("hi")
    assert "advisor" in seen["tools"]
    assert ADVISOR_GUIDANCE in (result.all_messages()[0].instructions or "")


@pytest.mark.anyio
async def test_advisor_tool_round_trip_with_usage_trailer():
    agent = Agent(
        _consult_once_main(),
        capabilities=[Advisor(model=_advisor_returns("Check edge cases first."))],
    )
    result = await agent.run("build the thing")
    (ret,) = _advisor_tool_returns(result)
    assert "Check edge cases first." in ret.content
    assert "[advisor usage:" in ret.content
    assert result.output == "done"


def _consult_twice_main():
    """A main model that calls the advisor tool until it has two returns."""

    def fn(messages, info):
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if len(returns) < 2:
            return ModelResponse(parts=[ToolCallPart("advisor", {})])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_max_uses_caps_consultations_within_a_run():
    calls = {"n": 0}

    def advisor_model(messages, info):
        calls["n"] += 1
        return ModelResponse(parts=[TextPart("advice")])

    agent = Agent(
        _consult_twice_main(),
        capabilities=[Advisor(model=FunctionModel(advisor_model), max_uses=1)],
    )
    result = await agent.run("go")
    assert calls["n"] == 1  # second consult refused before reaching the model
    first, second = _advisor_tool_returns(result)
    assert "advice" in first.content
    assert "max_uses=1" in second.content
    assert "Continue without advice" in second.content


@pytest.mark.anyio
async def test_max_uses_resets_on_the_next_run():
    calls = {"n": 0}

    def advisor_model(messages, info):
        calls["n"] += 1
        return ModelResponse(parts=[TextPart("advice")])

    agent = Agent(
        _consult_once_main(),
        capabilities=[Advisor(model=FunctionModel(advisor_model), max_uses=1)],
    )
    await agent.run("first")
    await agent.run("second")
    assert calls["n"] == 2  # for_run gave the second run a fresh counter


@pytest.mark.anyio
async def test_unresolvable_model_returns_text_not_raise():
    agent = Agent(
        _consult_once_main(),
        capabilities=[Advisor(model="not-a-provider:not-a-model")],
    )
    result = await agent.run("go")
    (ret,) = _advisor_tool_returns(result)
    assert "Advisor unavailable" in ret.content
    assert result.output == "done"  # the run completed


@pytest.mark.anyio
async def test_defer_loading_marks_the_tool_deferred_until_loaded():
    seen = {}

    def capture(messages, info):
        seen.setdefault("defs", {t.name: t for t in info.function_tools})
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(
        FunctionModel(capture),
        capabilities=[
            Advisor(model=_advisor_returns("x"), id="advisor", defer_loading=True)
        ],
    )
    await agent.run("hi")
    # defer_loading=True marks the advisor ToolDefinition itself as deferred...
    assert seen["defs"]["advisor"].defer_loading is True
    # ...and adds a load_capability tool to bring it in when needed
    assert any("load_capability" in name for name in seen["defs"])
