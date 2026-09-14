"""Typed view of the session-event wire vocabulary (parent spec §2).

The bus and the HTTP layer stay dict-typed (transport); this module is the
RENDERER contract — the TUI pump parses each wire dict once and every
front-end handler consumes only these models. Unknown types parse to None
(forward-compatible: a newer server can add events an older client skips).

Daemon queue mechanics beyond ``steer.accepted`` (queue depth etc.) are
intentionally NOT modeled here — they are transport-layer details not
consumed by renderers.
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


class TurnUsage(BaseModel):
    """The running token total of the turn's current model run, republished
    whenever it changes (roughly once per model response, not per delta). A
    live client renders it as the in-flight ``+N`` counter; ``turn.finished``
    still carries the authoritative per-turn summary."""

    type: Literal["turn.usage"]
    turn_id: str
    total_tokens: int = 0


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
    # "done" | "failed" | "denied" — the call's outcome (stream_events.
    # status_from_part). Defaulted so an older server that omits it renders as a
    # plain success, exactly as before the field existed.
    status: str = "done"
    # Image returns (read_file on a PNG/JPEG) as references, never bytes:
    # ``[{"sha", "media_type", "bytes"}]``, each resolvable at
    # ``GET .../images/{sha}``. ``content`` keeps the text placeholder. Empty
    # for a text-only result, and for an older server that predates the field.
    images: list[dict] = Field(default_factory=list)


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
    id: str | None = None
    backend: str | None = None
    kind: str | None = None
    severity: str | None = None
    data: dict = Field(default_factory=dict)


class SessionBackendState(BaseModel):
    type: Literal["session.backend_state"]
    inventory: dict = Field(default_factory=dict)
    telemetry: dict = Field(default_factory=dict)


class BackendTaskChanged(BaseModel):
    type: Literal["backend.task"]
    id: str
    backend: str
    description: str
    status: str


class SteerAccepted(BaseModel):
    """A mid-turn steer the host buffered. ``attachments`` is a count: the
    bytes stay on the submitting client and never cross the wire."""

    type: Literal["steer.accepted"]
    text: str
    attachments: int = 0


class StreamGap(BaseModel):
    type: Literal["stream.gap"]
    resync: str = "history"


WireEvent = Annotated[
    (
        TurnStarted
        | TurnFinished
        | TurnUsage
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
        | WorkflowSpawned
        | WorkflowStarted
        | WorkflowLogged
        | WorkflowFinished
        | WorkflowSpawnFinished
        | SessionTtft
        | SessionModeChanged
        | SessionNotice
        | BackendTaskChanged
        | SessionBackendState
        | SteerAccepted
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
