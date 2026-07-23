"""Map Pydantic AI streaming events to plain JSON-serializable dicts.

One mapping, two consumers: the headless CLI's ``stream-json`` output and the
server's per-session event bus. Keeping it shared means an app consuming
``marim -p --output-format stream-json`` and one consuming ``marim serve``'s
WebSocket stream see the same event vocabulary."""

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
            "content": str(getattr(event.part, "content", "")),
        }
    return None
