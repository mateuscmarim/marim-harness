"""Headless (non-interactive) execution: run one turn and render the result to
a stream, without the TUI. Supports three output formats — plain text, a single
JSON object, and newline-delimited JSON streaming."""

import json
import sys

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)

from ...agent import Harness
from ...errors import format_provider_error
from ...usage import usage_summary


def _notify(harness: Harness, title: str, body: str, event_type: str) -> None:
    """Fire a desktop notification if one is wired on deps. Best-effort."""
    notifier = harness.deps.notifier
    if notifier is not None:
        notifier.send(title, body, event_type)


def _preview(text: str, max_len: int = 80) -> str:
    """Return a short preview of *text* for notification bodies.

    Newlines are collapsed to spaces and the result is truncated to *max_len*
    characters, with an ellipsis when trimmed.  Empty input yields a
    generic fallback so the notification is never blank."""
    if not text or not text.strip():
        return "(empty response)"
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1] + "…"


def _usage_dict(harness: Harness) -> dict:
    return usage_summary(harness.session.usage, harness.model_id)


def _result_obj(harness: Harness, output: str) -> dict:
    store = harness.session.store
    return {
        "type": "result",
        "output": output,
        "session_id": store.session_id if store is not None else None,
        "name": harness.session.session_name,
        "usage": _usage_dict(harness),
    }


def _event_obj(event) -> dict | None:
    """Map a Pydantic AI streaming event to a JSON-serializable line, or None to
    skip events we don't surface."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return {"type": "text", "text": event.part.content or ""}
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return {"type": "text", "text": event.delta.content_delta or ""}
    if isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
        return {"type": "thinking", "text": event.part.content or ""}
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta):
        return {"type": "thinking", "text": event.delta.content_delta or ""}
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
        await harness.session_start("resume" if harness.session.history else "startup")
        output = await harness.run_turn(prompt, event_stream_handler=handler)
    except Exception as exc:  # keep the failure surface small and scriptable
        detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
        print(detail, file=err)
        _notify(harness, "Turn error", detail, "error")
        return 1
    finally:
        await harness.session_end("exit")
        await harness.aclose()

    _notify(harness, "Turn complete", _preview(output), "turn_complete")

    if output_format == "json":
        obj = _result_obj(harness, output)
        del obj["type"]  # the single-object form has no event envelope
        print(json.dumps(obj), file=out)
    elif output_format == "stream-json":
        print(json.dumps(_result_obj(harness, output)), file=out)
    else:  # text
        print(output, file=out)
    return 0
