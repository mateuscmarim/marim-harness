"""What a CLI backend says about its own context: the ``ContextReport``.

Under ``claude-cli`` the conversation lives inside the
backend, and marim's history is a mirror of it — so the status bar's chars/4
estimate over that mirror (denominated against marim's own budget) is a
guess about a context it does not own. Claude reports context on
the wire: Claude's ``assistant`` events carry the request's ``usage``, its
``result`` the model's ``contextWindow``, and ``get_context_usage`` refines
the total with the same local category estimates as ``/context``.
Historical Codex context reports remain readable. Each adapter keeps its reading on a
``context_report`` attribute (the same pattern as ``quota_hint``); the status
bar and the ``GET session`` payload prefer it over the estimate.

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


def context_report_from_usage(raw: Any, *, window: int | None = None) -> ContextReport | None:
    """Parse Claude's ``get_context_usage`` summary into the shared gauge.

    ``rawMaxTokens`` is the model's physical window; ``maxTokens`` is the
    auto-compaction allowance shown by Claude's UI, so the raw value wins when
    both are present. Older responses may omit either one, in which case the
    last window learned from ``modelUsage`` is retained. A malformed response
    is ignored so this informational control request can never fail a turn."""
    if not isinstance(raw, dict):
        return None
    used = raw.get("totalTokens")
    if isinstance(used, bool) or not isinstance(used, int) or used < 0:
        return None
    reported_window = raw.get("rawMaxTokens")
    if (
        isinstance(reported_window, bool)
        or not isinstance(reported_window, int)
        or reported_window <= 0
    ):
        reported_window = raw.get("maxTokens")
    if (
        isinstance(reported_window, bool)
        or not isinstance(reported_window, int)
        or reported_window <= 0
    ):
        reported_window = window
    return ContextReport(used, reported_window)


def last_context_report(
    history: list[ModelMessage],
    provider: str | None = None,
    model_name: str | None = None,
) -> ContextReport | None:
    """The report persisted on the NEWEST ``ModelResponse``, or None.

    Only the newest response is consulted: an older one's report describes
    a context that has moved on. Optional provider and model filters reject a
    known mismatch after a backend or model switch. A missing persisted model
    name remains valid for histories written before that field was stored."""
    for msg in reversed(history):
        if isinstance(msg, ModelResponse):
            if provider is not None and msg.provider_name != provider:
                return None
            # Current CLI responses persist the configured model name. Reject
            # a known mismatch after a model switch: its window can differ by
            # hundreds of thousands of tokens. Older histories omitted the
            # name, so keep accepting those as a backward-compatible estimate.
            if (
                model_name is not None
                and msg.model_name is not None
                and msg.model_name != model_name
            ):
                return None
            return ContextReport.from_payload((msg.provider_details or {}).get(CONTEXT_REPORT_KEY))
    return None


_NOT_A_BACKEND = object()


def current_context_report(model: object, history: list[ModelMessage]) -> ContextReport | None:
    """The report to show for ``model``: its live reading when it has one,
    else (a resumed session before its first turn) the one persisted on the
    newest response by the same provider and configured model; None for a
    model that never reports (marim's own providers), so the estimate stays
    in charge there."""
    if getattr(model, "context_invalidated", False):
        return None
    live = getattr(model, "context_report", _NOT_A_BACKEND)
    if live is _NOT_A_BACKEND:
        return None
    if isinstance(live, ContextReport):
        return live
    return last_context_report(
        history,
        getattr(model, "provider_name", None),
        getattr(model, "model_name", None),
    )
