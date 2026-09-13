"""The subscription quota hint the status bar shows: ``quota 37% (5h) · 12% (1w)``.

Both CLI backends run on a subscription with rolling rate-limit windows —
Codex reports a "primary" and a "secondary" window (``account/rateLimits/
read``), Claude Code a five-hour and a seven-day one (the ``get_usage``
control request). The provider-neutral value objects live here so the two
adapters and the status bar share one rendering; the wire-shape parsers
stay with their backend (``codex/quota.py``, ``claude/quota.py``).
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
