"""What a CLI backend says about its own context: the ``ContextReport``.

Under ``claude-cli`` and ``codex-cli`` the conversation lives inside the
backend, and marim's history is a mirror of it — so the status bar's chars/4
estimate over that mirror (denominated against marim's own budget) is a
guess about a context it does not own. Both backends report the truth on
the wire: Claude's ``assistant`` events carry the request's ``usage`` (the
real prompt size) and its ``result`` the model's ``contextWindow``; Codex's
``thread/tokenUsage/updated`` carries the last response's usage and
``modelContextWindow``. Each CLI model adapter keeps the newest reading on
a ``context_report`` attribute (the same pattern as ``quota_hint``); the
status bar and the ``GET session`` payload prefer it over the estimate.

The report also rides on the turn's ``ModelResponse.provider_details`` so a
resumed session shows the backend's last known number before its first new
turn (``last_context_report``). Pure value object + pure helpers; nothing
here touches a process.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse

# provider_details key the report persists under (next to ``cli_activity``).
CONTEXT_REPORT_KEY = "cli_context"


@dataclass(frozen=True)
class ContextReport:
    """The backend's context gauge: ``used`` is the prompt size of its most
    recent model request (cache-inclusive input tokens — exactly what the
    backend's own ``/context`` counts), ``window`` the model's context window
    when the backend said (None until it does; the first Claude turn learns
    it only at its ``result``)."""

    used: int
    window: int | None = None

    @property
    def percent(self) -> int | None:
        """Percent of the window in use, or None without a window."""
        if not self.window:
            return None
        return round(self.used / self.window * 100)

    def with_window(self, window: int | None) -> ContextReport:
        """The same reading with ``window`` filled in; a None keeps what the
        report already has (a backend that stops reporting the window does
        not erase a known one)."""
        return replace(self, window=window) if window else self

    def to_payload(self) -> dict:
        return {"used": self.used, "window": self.window}

    @classmethod
    def from_payload(cls, raw: Any) -> ContextReport | None:
        """Rebuild from ``to_payload`` output (the wire, or persisted
        ``provider_details``); None for anything malformed."""
        if not isinstance(raw, dict):
            return None
        used = raw.get("used")
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            return None
        window = raw.get("window")
        return cls(used=used, window=window if isinstance(window, int) and window > 0 else None)


def prompt_tokens(usage: dict | None) -> int:
    """The prompt size of one Anthropic-shaped ``usage`` block: the uncached
    input plus both cache buckets (Anthropic reports ``input_tokens`` as the
    uncached bucket only, so the three together are the request's size).
    Best-effort: a bucket that is not a number (a protocol drift) counts as
    zero rather than failing the turn over a gauge."""
    u = usage if isinstance(usage, dict) else {}
    total = 0
    for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        v = u.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total += int(v)
    return total


def last_context_report(
    history: list[ModelMessage], provider: str | None = None
) -> ContextReport | None:
    """The report persisted on the NEWEST ``ModelResponse``, or None.

    Only the newest response is consulted: an older one's report describes
    a context that has moved on. With ``provider`` given, the response must
    also come from that provider — after a backend switch (claude-cli →
    codex-cli) the newest response's report describes the OTHER backend's
    context, and showing it for the new one would be a lie until its first
    turn replaces it."""
    for msg in reversed(history):
        if isinstance(msg, ModelResponse):
            if provider is not None and msg.provider_name != provider:
                return None
            return ContextReport.from_payload((msg.provider_details or {}).get(CONTEXT_REPORT_KEY))
    return None


_NOT_A_BACKEND = object()


def current_context_report(model: object, history: list[ModelMessage]) -> ContextReport | None:
    """The report to show for ``model``: its live reading when it has one,
    else (a resumed session before its first turn) the one persisted on the
    newest response BY THE SAME PROVIDER; None for a model that never
    reports (marim's own providers), so the estimate stays in charge there."""
    live = getattr(model, "context_report", _NOT_A_BACKEND)
    if live is _NOT_A_BACKEND:
        return None
    if isinstance(live, ContextReport):
        return live
    return last_context_report(history, getattr(model, "provider_name", None))
