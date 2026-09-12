"""The codex-cli quota hint: a compact reading of ``account/rateLimits/read``
for the status line.

Codex reports subscription usage as up to two rolling windows (a short
"primary" one — typically 5 hours — and a longer "secondary" one, typically a
week), each with the percentage consumed. marim polls the snapshot once per
turn (spec §Usage: "polled at most once per turn for the status line;
failures are ignored") and renders it as ``quota 37% (5h) · 12% (1w)``. Pure
parse/format helpers live here so the model and the status bar share one
reading of the wire shape; the RPC itself is on ``CodexServer``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuotaWindow:
    """One rate-limit window: percent used and the window length in minutes
    (None when the server did not say)."""

    used_percent: int
    window_mins: int | None

    def render(self) -> str:
        span = format_window(self.window_mins)
        return f"{self.used_percent}%" + (f" ({span})" if span else "")


@dataclass(frozen=True)
class QuotaHint:
    primary: QuotaWindow | None
    secondary: QuotaWindow | None

    def render(self) -> str:
        parts = [w.render() for w in (self.primary, self.secondary) if w is not None]
        return "quota " + " · ".join(parts) if parts else ""


def format_window(mins: int | None) -> str:
    """``300`` → ``5h``, ``10080`` → ``1w``, ``90`` → ``90m``; empty for unknown."""
    if not mins or mins <= 0:
        return ""
    if mins % 10080 == 0:
        return f"{mins // 10080}w"
    if mins % 1440 == 0:
        return f"{mins // 1440}d"
    if mins % 60 == 0:
        return f"{mins // 60}h"
    return f"{mins}m"


def _window(raw: object) -> QuotaWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("usedPercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    mins = raw.get("windowDurationMins")
    return QuotaWindow(
        used_percent=int(used),
        window_mins=int(mins) if isinstance(mins, int) and mins > 0 else None,
    )


def quota_from(snapshot: object) -> QuotaHint | None:
    """A ``QuotaHint`` from a ``RateLimitSnapshot`` (the ``rateLimits`` member of
    the read response or the rolling notification), or None when it carries
    no window at all (an API-key account, or a malformed payload) — nothing to
    show rather than a misleading ``quota 0%``."""
    if not isinstance(snapshot, dict):
        return None
    hint = QuotaHint(
        primary=_window(snapshot.get("primary")), secondary=_window(snapshot.get("secondary"))
    )
    return hint if hint.primary is not None or hint.secondary is not None else None
