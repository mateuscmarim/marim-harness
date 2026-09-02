"""Typed view of the session-event wire vocabulary (parent spec §2).

The bus and the HTTP layer stay dict-typed (transport); this module is the
RENDERER contract — the TUI pump parses each wire dict once and every
front-end handler consumes only these models. Unknown types parse to None
(forward-compatible: a newer server can add events an older client skips).

In phase 3a, daemon queue mechanics (steer.accepted, etc.) are intentionally
NOT modeled here — they are transport-layer details not consumed by renderers.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter


class TurnStarted(BaseModel):
    type: Literal["turn.started"]
    turn_id: str
    prompt: str = ""
    trigger: str = "user"


class TurnFinished(BaseModel):
    type: Literal["turn.finished"]
    turn_id: str
    output: str | None = None
    usage: dict | None = None
    interrupted: bool = False


class TurnError(BaseModel):
    type: Literal["turn.error"]
    turn_id: str
    error: str


class TextDelta(BaseModel):
    type: Literal["text.delta"]
    text: str


class ThinkingDelta(BaseModel):
    type: Literal["thinking.delta"]
    text: str


class ToolCall(BaseModel):
    type: Literal["tool.call"]
    id: str
    name: str
    args: dict


class ToolResult(BaseModel):
    type: Literal["tool.result"]
    id: str
    content: Any = None


class AskPending(BaseModel):
    type: Literal["ask.pending"]
    id: str
    kind: str  # "approval" | "question" | "plan"
    payload: dict
    created: str


class AskResolved(BaseModel):
    type: Literal["ask.resolved"]
    id: str
    answer: dict | None = None
    cancelled: bool = False
    reason: str | None = None


class SessionStatus(BaseModel):
    type: Literal["session.status"]
    status: str


class SessionRenamed(BaseModel):
    type: Literal["session.renamed"]
    from_: str = Field(alias="from")  # "from" is a keyword
    to: str


class TasksChanged(BaseModel):
    type: Literal["tasks.changed"]


class JobsChanged(BaseModel):
    type: Literal["jobs.changed"]


class CompactionStarted(BaseModel):
    type: Literal["compaction.started"]


class CompactionFinished(BaseModel):
    type: Literal["compaction.finished"]
    before: int | None = None
    after: int | None = None


class SubagentEvent(BaseModel):
    type: Literal["subagent.event"]
    stream_id: str
    event: dict  # nested wire stream event (text.delta/tool.call/...)
    usage: dict | None = None


class SubagentNotice(BaseModel):
    type: Literal["subagent.notice"]
    stream_id: str
    message: str


class SubagentModel(BaseModel):
    type: Literal["subagent.model"]
    stream_id: str
    model: str


class SubagentThinking(BaseModel):
    type: Literal["subagent.thinking"]
    stream_id: str
    level: str


class SubagentUsage(BaseModel):
    type: Literal["subagent.usage"]
    stream_id: str
    usage: dict


class SubagentCliActivity(BaseModel):
    type: Literal["subagent.cli_activity"]
    events: list[dict]  # wire stream events (claude-cli tool_use/tool_result)


class WorkflowSpawned(BaseModel):
    type: Literal["workflow.spawned"]
    stream_id: str
    spawn_type: str
    task: str
    parent_tool_call_id: str


class WorkflowStarted(BaseModel):
    type: Literal["workflow.started"]
    tool_call_id: str
    title: str


class WorkflowLogged(BaseModel):
    type: Literal["workflow.logged"]
    tool_call_id: str
    message: str


class WorkflowFinished(BaseModel):
    type: Literal["workflow.finished"]
    tool_call_id: str
    outcome: str
    failed: bool


class WorkflowSpawnFinished(BaseModel):
    type: Literal["workflow.spawn_finished"]
    stream_id: str
    report: str


class SessionTtft(BaseModel):
    type: Literal["session.ttft"]
    seconds: float


class SessionModeChanged(BaseModel):
    type: Literal["session.mode_changed"]
    mode: str


class SessionNotice(BaseModel):
    type: Literal["session.notice"]
    message: str


class StreamGap(BaseModel):
    type: Literal["stream.gap"]
    resync: str = "history"


WireEvent = Annotated[
    (
        TurnStarted
        | TurnFinished
        | TurnError
        | TextDelta
        | ThinkingDelta
        | ToolCall
        | ToolResult
        | AskPending
        | AskResolved
        | SessionStatus
        | SessionRenamed
        | TasksChanged
        | JobsChanged
        | CompactionStarted
        | CompactionFinished
        | SubagentEvent
        | SubagentNotice
        | SubagentModel
        | SubagentThinking
        | SubagentUsage
        | SubagentCliActivity
        | WorkflowSpawned
        | WorkflowStarted
        | WorkflowLogged
        | WorkflowFinished
        | WorkflowSpawnFinished
        | SessionTtft
        | SessionModeChanged
        | SessionNotice
        | StreamGap
    ),
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter = TypeAdapter(WireEvent)


def parse_wire_event(d: dict) -> WireEvent | None:
    """Parse one wire dict. Unknown/malformed → None (log at the call site)."""
    if not isinstance(d.get("type"), str):
        return None
    try:
        return _ADAPTER.validate_python(d)
    except Exception:
        return None
