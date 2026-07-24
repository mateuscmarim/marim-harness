"""Map Pydantic AI streaming events to plain JSON-serializable dicts.

One mapping, two consumers: the headless CLI's ``stream-json`` output and the
server's per-session event bus. Keeping it shared means an app consuming
``marim -p --output-format stream-json`` and one consuming ``marim serve``'s
WebSocket stream see the same event vocabulary."""

import json

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

from .binary_safe import has_binary_content, render_binary_safe


def _jsonify_tool_content(content) -> str:
    """Serialize tool-return content for the JSON event stream. A plain string is
    passed through untouched; a read_file image return (BinaryContent, scalar or
    inside a list) is routed through the shared binary-safe placeholder — headless
    stream-json and the WebSocket serve clients would otherwise get
    ``json.dumps(default=str)``'s dump of the full base64 body (up to ~20MB) per
    image read. Anything else (structured content — a list/dict of content
    blocks) is JSON-encoded — with a ``str`` fallback for anything non-
    serializable — so consumers get valid JSON instead of a Python ``repr``
    (single-quoted, unparseable)."""
    if isinstance(content, str):
        return content
    if has_binary_content(content):
        return render_binary_safe(content)
    try:
        return json.dumps(content, default=str)
    except (TypeError, ValueError):
        return str(content)


def event_to_dict(event) -> dict | None:
    """Map a Pydantic AI streaming event to a JSON-serializable dict, or None to
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
            "content": _jsonify_tool_content(getattr(event.part, "content", "")),
        }
    return None
