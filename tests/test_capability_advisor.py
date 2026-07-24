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
