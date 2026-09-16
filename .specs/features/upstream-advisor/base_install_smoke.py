"""Run with the installed base wheel, without project/development extras."""

import asyncio
from importlib.metadata import PackageNotFoundError, requires, version
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness import Advisor

from marim_harness.capabilities import Advisor as LegacyAdvisor
from marim_harness.runtime.builder import HarnessBuilder


def executor(messages, info):
    results = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
    if results:
        assert results[0].content == "base installation advice"
        return ModelResponse(parts=[TextPart("done")])
    return ModelResponse(parts=[ToolCallPart("advisor", {"prompt": "Review"})])


async def main():
    assert LegacyAdvisor is Advisor
    try:
        version("pydantic-monty")
    except PackageNotFoundError:
        pass
    else:
        raise AssertionError("Base install unexpectedly includes Monty")
    assert any(
        "pydantic-ai-harness==0.31.0" in req and "extra" not in req
        for req in requires("marim-harness")
    )
    agent = Agent(
        FunctionModel(executor),
        capabilities=[Advisor(TestModel(custom_output_text="base installation advice"))],
    )
    assert (await agent.run("go")).output == "done"
    with TemporaryDirectory() as workspace:
        harness = (
            HarnessBuilder(workspace=Path(workspace), model=FunctionModel(executor))
            .with_advisor("test")
            .build()
        )
        harness._build_advisor_model = lambda _: TestModel(
            custom_output_text="base installation advice"
        )
        assert (await harness.run_turn("go")).result == "done"
        await harness.aclose()
    print("Base-only wheel Advisor smoke passed; Monty absent")


asyncio.run(main())
