"""TtftTrackingModel measures what the event-stream handler cannot: pydantic-ai
models wait for the first chunk INSIDE request_stream.__aenter__ (the OpenAI
model peeks it before yielding), so a handler-side timestamp starts after the
real wait and always reads ~0. The wrapper stamps the clock before delegating
and reads the public StreamedResponse.time_to_first_chunk on the way out."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from marim_harness.runtime.ttft import TtftTrackingModel

pytestmark = pytest.mark.anyio


class _SlowToOpenModel(WrapperModel):
    """Delays inside request_stream entry — the shape of the real wait: the
    provider processes the prompt before the wrapped model's __aenter__
    (which awaits/peeks the first chunk) returns."""

    def __init__(self, wrapped: Model, delay: float) -> None:
        super().__init__(wrapped)
        self._delay = delay

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ) -> AsyncGenerator[StreamedResponse, None]:
        await asyncio.sleep(self._delay)
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as streamed:
            yield streamed


def _request() -> list[ModelMessage]:
    return [ModelRequest(parts=[UserPromptPart(content="hi")])]


async def test_reports_wait_spent_opening_the_stream():
    """The measurement must include time spent before the stream context is
    entered — exactly the window a handler-side timestamp misses."""
    reported: list[float] = []
    model = TtftTrackingModel(
        _SlowToOpenModel(TestModel(custom_output_text="ok"), delay=0.05),
        on_ttft=reported.append,
    )
    async with model.request_stream(_request(), None, ModelRequestParameters()) as sr:
        async for _ in sr:
            pass
    assert len(reported) == 1
    assert reported[0] >= 0.05

async def test_aborted_consumer_still_reports_after_first_event():
    """A run cancelled mid-stream still stared at a first token; the report
    fires from the context teardown, not from stream completion."""
    reported: list[float] = []
    model = TtftTrackingModel(TestModel(custom_output_text="ok"), on_ttft=reported.append)
    async with model.request_stream(_request(), None, ModelRequestParameters()) as sr:
        async for _ in sr:
            break  # consumer bails after the first event
    assert len(reported) == 1


async def test_stream_never_consumed_reports_nothing():
    """No event surfaced -> time_to_first_chunk is None -> no callback (the
    status bar keeps showing the previous request's value, not a bogus 0)."""
    reported: list[float] = []
    model = TtftTrackingModel(TestModel(custom_output_text="ok"), on_ttft=reported.append)
    async with model.request_stream(_request(), None, ModelRequestParameters()):
        pass  # opened, never iterated
    assert reported == []


async def test_agent_run_reports_one_ttft_per_streamed_request():
    """End-to-end through agent.run: with an event_stream_handler the run
    streams, and each model request reports exactly one TTFT."""
    reported: list[float] = []
    agent = Agent(TtftTrackingModel(TestModel(custom_output_text="done"), on_ttft=reported.append))

    async def handler(ctx, events):
        async for _ in events:
            pass

    result = await agent.run("hello", event_stream_handler=handler)
    assert result.output == "done"
    assert len(reported) == 1
    assert reported[0] >= 0.0


async def test_non_streamed_request_reports_nothing():
    """Without a handler the run uses plain request(); the wrapper only
    measures streams (the value feeds the interactive status bar, and
    interactive turns always stream)."""
    reported: list[float] = []
    agent = Agent(TtftTrackingModel(TestModel(custom_output_text="done"), on_ttft=reported.append))
    result = await agent.run("hello")
    assert result.output == "done"
    assert reported == []
