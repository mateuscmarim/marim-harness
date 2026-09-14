"""Translated Codex items → pydantic-ai stream events and a message list.

Two consumers need the same fold: a ``backend: codex-cli`` spawn (its
transcript streams into the sub-agents screen and is persisted to the
spawn's sidecar) and, since Codex-side sub-agents became first-class cards
(``codex/collab.py``), every adopted child thread of the main-loop provider
or of a spawn. ``ItemTranscript`` is that fold — the codex analog of
``subagents.cli_backend.CliStreamTranslator``. ``activity_events`` (the
tool-card shapes ``on_activity`` sinks receive) lives here too so the
transcript never has to import the model layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)

from ..config.lifecycle import BackendNotice, notice_part
from .translate import ActivityEnd, ActivityStart, Notice, TextDelta, ThinkingDelta


# `item` takes `ActivityStart | ActivityEnd` in practice, but stays annotated
# as `object` here so this matches `TextFolder.__init__`'s callback shape
# (`Callable[[object], ...]`, shared with claude-cli's untyped equivalents) —
# a narrower parameter type is a real contravariance mismatch pyright flags.
def activity_events(item: object) -> list:
    """pydantic-ai tool events for a Codex activity, for the TUI card sinks
    (``on_activity``) — the same shapes ``cli_activity_events`` builds for
    claude-cli. Never enters the ModelResponse."""
    if isinstance(item, ActivityStart):
        part = ToolCallPart(tool_name=item.tool_name, args=item.args, tool_call_id=item.item_id)
        return [FunctionToolCallEvent(part=part)]
    assert isinstance(item, ActivityEnd)
    part = ToolReturnPart(
        tool_name="tool",
        content=item.content,
        tool_call_id=item.item_id,
        timestamp=datetime.now(tz=timezone.utc),
        outcome="failed" if item.is_error else "success",
    )
    return [FunctionToolResultEvent(part=part)]


@dataclass
class ItemTranscript:
    """Folds translated Codex items into (a) pydantic-ai stream events for the
    sub-agents screen and (b) a pydantic-ai message list for a sidecar. The
    stream's *output* is the text of the LAST agent message (with
    ``outputSchema`` that is the JSON document)."""

    messages: list[Any] = field(default_factory=list)
    _index: int = 0
    _texts: dict[str, list[str]] = field(default_factory=dict)
    _open_text: str | None = None  # item id of the TextPart being streamed
    _call_names: dict[str, str] = field(default_factory=dict)
    # The backing list for the in-progress ModelResponse's parts. `ModelResponse.parts`
    # is typed `Sequence[ModelResponsePart]` (pyright rejects `.append` on it), so the
    # mutable list lives here and is handed to ModelResponse by reference — same object,
    # so appending here is visible through `resp.parts` too.
    _parts: list[Any] = field(default_factory=list)

    def _response(self) -> ModelResponse:
        if self.messages and isinstance(self.messages[-1], ModelResponse):
            return self.messages[-1]
        self._parts = []
        resp = ModelResponse(parts=self._parts)
        self.messages.append(resp)
        return resp

    def feed(self, item: object) -> list[Any]:
        """Fold one item; return the stream events it produced. Items this
        fold does not render (collab bookkeeping, usage) yield nothing."""
        if isinstance(item, TextDelta):
            return self._text(item)
        if isinstance(item, ThinkingDelta):
            return self._thinking(item)
        if isinstance(item, ActivityStart):
            self._open_text = None
            self._call_names[item.item_id] = item.tool_name
            self._response()
            self._parts.append(
                ToolCallPart(tool_name=item.tool_name, args=item.args, tool_call_id=item.item_id)
            )
            return activity_events(item)
        if isinstance(item, ActivityEnd):
            self._open_text = None
            self.messages.append(
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name=self._call_names.get(item.item_id, "tool"),
                            content=item.content,
                            tool_call_id=item.item_id,
                            timestamp=datetime.now(tz=timezone.utc),
                        )
                    ]
                )
            )
            return activity_events(item)
        if isinstance(item, (Notice, BackendNotice)):
            notice = item.normalized() if isinstance(item, Notice) else item
            self._open_text = None
            self._response()
            self._parts.append(notice_part(notice.to_payload()))
            return [notice]
        return []

    def _text(self, item: TextDelta) -> list[Any]:
        events: list[Any] = []
        self._response()
        if self._open_text != item.item_id:
            self._open_text = item.item_id
            self._index += 1
            self._parts.append(TextPart(content=""))
            events.append(PartStartEvent(index=self._index, part=TextPart(content="")))
        part = self._parts[-1]
        assert isinstance(part, TextPart)
        part.content += item.delta
        self._texts.setdefault(item.item_id, []).append(item.delta)
        events.append(
            PartDeltaEvent(index=self._index, delta=TextPartDelta(content_delta=item.delta))
        )
        return events

    def _thinking(self, item: ThinkingDelta) -> list[Any]:
        self._response()
        self._open_text = None
        last = self._parts[-1] if self._parts else None
        if isinstance(last, ThinkingPart):
            last.content += item.delta
            return [
                PartDeltaEvent(index=self._index, delta=ThinkingPartDelta(content_delta=item.delta))
            ]
        self._index += 1
        self._parts.append(ThinkingPart(content=item.delta))
        return [PartStartEvent(index=self._index, part=ThinkingPart(content=item.delta))]

    def output(self) -> str:
        if not self._texts:
            return ""
        last_id = next(reversed(self._texts))
        return "".join(self._texts[last_id])
