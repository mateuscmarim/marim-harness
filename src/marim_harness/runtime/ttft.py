"""Client-side time-to-first-token measurement.

Why a model wrapper and not the event-stream handler: pydantic-ai's models
wait for the first chunk *inside* ``request_stream.__aenter__`` (the OpenAI
model peeks it to learn the model name before yielding the stream), and the
agent graph only invokes the event-stream handler once that context is
entered. A timestamp taken at handler start therefore misses the entire real
wait — prompt processing, network, generation start — and always reads ~0
regardless of provider. The wrapper stamps the clock *before* delegating
``request_stream``, then reads the public
``StreamedResponse.time_to_first_chunk`` (which stamps the instant the first
event is surfaced to the consumer) once the consumer is done with the stream.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings


class TtftTrackingModel(WrapperModel):
    """Wraps a model to report each streamed request's time-to-first-token.

    ``on_ttft`` is called at most once per streamed request — after the
    consumer finishes with the stream, and only if at least one event was
    surfaced (a request cancelled mid-stream still had a first token; one
    aborted before any output reports nothing). Non-streamed ``request()``
    calls are not measured: the value feeds the interactive status bar, and
    interactive turns always stream.
    """

    def __init__(self, wrapped: Model, on_ttft: Callable[[float], None]) -> None:
        super().__init__(wrapped)
        self._on_ttft = on_ttft

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse, None]:
        request_start = time.perf_counter()
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as streamed:
            try:
                yield streamed
            finally:
                # In finally so teardown paths (cancel, render error) still
                # report: the first token had arrived even if the run died.
                ttft = streamed.time_to_first_chunk(request_start)
                if ttft is not None:
                    self._on_ttft(ttft)
