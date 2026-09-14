"""Display-only backend lifecycle records shared by live and history consumers."""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TextIO
from uuid import uuid4

from pydantic_ai.messages import TextPart

logger = logging.getLogger(__name__)
NOTICE_KEY = "backend_notice"
_NOTICE_OUTPUT: ContextVar[TextIO | None] = ContextVar("backend_notice_output", default=None)


@contextmanager
def notice_output(output: TextIO) -> Iterator[None]:
    """Scope headless diagnostics without redirecting other sessions' stderr."""
    token = _NOTICE_OUTPUT.set(output)
    try:
        yield
    finally:
        _NOTICE_OUTPUT.reset(token)


@dataclass(frozen=True)
class BackendObservation:
    inventory: dict = field(default_factory=dict)
    telemetry: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BackendNotice:
    message: str
    backend: str
    kind: str
    severity: str = "info"
    id: str = field(default_factory=lambda: str(uuid4()))
    data: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "message": self.message,
            "backend": self.backend,
            "kind": self.kind,
            "severity": self.severity,
            "data": dict(self.data),
        }


def notice_part(payload: dict) -> TextPart:
    """A blank display marker: the backend message never becomes assistant prose."""
    return TextPart("", provider_name="marim", provider_details={NOTICE_KEY: payload})


def notice_from_part(part: object) -> dict | None:
    details = getattr(part, "provider_details", None)
    if not isinstance(details, dict):
        return None
    payload = details.get(NOTICE_KEY)
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("message"), str) or not payload["message"].strip():
        return None
    return payload


async def deliver_notice(
    notice: BackendNotice,
    callback: Callable[[list], Awaitable[None]] | None,
    *,
    ephemeral: bool = False,
) -> None:
    """Optional display I/O cannot fail a model turn; aux models remain silent."""
    if ephemeral:
        return
    try:
        if callback is not None:
            await callback([notice])
        else:
            print(notice.message, file=_NOTICE_OUTPUT.get() or sys.stderr, flush=True)
    except Exception as exc:
        logger.warning(
            "backend notice delivery failed backend=%s cause=%s", notice.backend, type(exc).__name__
        )


async def deliver_child_notice(notice, stream_id, on_event, on_notice=None, usage=None) -> None:
    """Reuse the best-effort boundary while keeping notices on their child stream."""

    async def callback(events):
        if on_event is not None:
            await on_event(stream_id, notice, usage)
        elif on_notice is not None:
            await on_notice(stream_id, notice.message)

    sink = callback if on_event is not None or on_notice is not None else None
    await deliver_notice(notice, sink)


def nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
