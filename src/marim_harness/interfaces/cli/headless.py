"""Headless (non-interactive) execution: run one turn and render the result to
a stream, without the TUI. Supports three output formats — plain text, a single
JSON object, and newline-delimited JSON streaming."""

import json
import sys
from typing import Optional

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from ...agent import Harness


def _usage_dict(harness: Harness) -> dict:
    u = harness.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "total_tokens": u.total_tokens,
    }


def _result_obj(harness: Harness, output: str) -> dict:
    return {
        "type": "result",
        "output": output,
        "session_id": harness.store.session_id if harness.store is not None else None,
        "name": harness.session_name,
        "usage": _usage_dict(harness),
    }


def _event_obj(event) -> Optional[dict]:
    """Map a Pydantic AI streaming event to a JSON-serializable line, or None to
    skip events we don't surface."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return {"type": "text", "text": event.part.content or ""}
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return {"type": "text", "text": event.delta.content_delta or ""}
    if isinstance(event, FunctionToolCallEvent):
        return {
            "type": "tool_call",
            "name": event.part.tool_name,
            "args": event.part.args_as_dict(),
            "id": event.part.tool_call_id,
        }
    if isinstance(event, FunctionToolResultEvent):
        return {
            "type": "tool_result",
            "id": event.tool_call_id,
            "content": str(getattr(event.part, "content", "")),
        }
    return None


async def run_headless(
    harness: Harness,
    prompt: str,
    output_format: str,
    *,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    """Run a single turn and render it in ``output_format`` (``text``, ``json``,
    or ``stream-json``). Returns a process exit code: 0 on success, 1 on a turn
    failure (the error is written to ``err``).

    Always runs the turn in streaming mode — for ``stream-json`` the events are
    emitted as NDJSON, otherwise they are drained silently. This mirrors the TUI,
    which streams every turn; some providers' non-streaming endpoints are flakier
    than their streaming ones, so streaming here keeps headless runs as reliable
    as the interactive app."""

    async def handler(ctx, events):
        async for event in events:
            if output_format != "stream-json":
                continue  # drain to force a streaming request; emit nothing
            obj = _event_obj(event)
            if obj is not None:
                print(json.dumps(obj), file=out, flush=True)

    try:
        await harness.connect()  # open any configured MCP servers for this run
        output = await harness.run_turn(prompt, event_stream_handler=handler)
    except Exception as exc:  # keep the failure surface small and scriptable
        print(f"{type(exc).__name__}: {exc}", file=err)
        return 1
    finally:
        await harness.aclose()

    if output_format == "json":
        obj = _result_obj(harness, output)
        del obj["type"]  # the single-object form has no event envelope
        print(json.dumps(obj), file=out)
    elif output_format == "stream-json":
        print(json.dumps(_result_obj(harness, output)), file=out)
    else:  # text
        print(output, file=out)
    return 0
