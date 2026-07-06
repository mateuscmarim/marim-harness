"""Transport-neutral wire schema: the event envelope every transport carries
and the request-body models the HTTP layer validates with.

The envelope is the contract — SSE + POST is merely the first transport. A
future WebSocket endpoint pipes the same ``Event`` dicts both ways with no
changes here (which is why nothing in this module knows about HTTP)."""

import json
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class Event:
    """One bus message. ``seq`` is monotonic per session and doubles as the
    SSE event id for Last-Event-ID resume."""

    seq: int
    ts: str
    type: str
    data: dict

    def as_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "data": self.data}


def sse_format(event: Event) -> str:
    """Render an Event as a Server-Sent Events frame."""
    return f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(event.data)}\n\n"


# Maps stream_events.event_to_dict()'s "type" field to the wire event type
# published on the bus. Events whose type isn't listed here are not surfaced.
STREAM_EVENT_TYPES = {
    "text": "text.delta",
    "thinking": "thinking.delta",
    "tool_call": "tool.call",
    "tool_result": "tool.result",
}


class WorkspaceIn(BaseModel):
    """POST /v1/workspaces. ``path`` registers an existing directory;
    otherwise a managed workspace is created (cloned when ``git_url`` set)."""

    name: str
    path: str | None = None
    git_url: str | None = None


class SessionIn(BaseModel):
    name: str | None = None
    mode: str | None = None  # "auto" | "ask" | "plan"; None -> configured default


class Attachment(BaseModel):
    data_b64: str
    media_type: str


class MessageIn(BaseModel):
    prompt: str
    attachments: list[Attachment] | None = None


class SteerIn(BaseModel):
    text: str


class AskAnswerIn(BaseModel):
    """POST answer for a parked ask. Approvals use approve/reason; ask_user
    questions use answers (or cancel)."""

    approve: bool | None = None
    reason: str | None = None
    answers: dict | None = None
    cancel: bool = False

    def as_answer(self) -> dict:
        if self.answers is not None:
            return {"answers": self.answers}
        if self.cancel:
            return {"cancel": True}
        return {"approve": bool(self.approve), "reason": self.reason}
