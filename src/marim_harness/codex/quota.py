"""The codex-cli quota hint: a compact reading of ``account/rateLimits/read``
for the status line.

Codex reports subscription usage as up to two rolling windows (a short
"primary" one — typically 5 hours — and a longer "secondary" one, typically a
week), each with the percentage consumed. marim polls the snapshot once per
turn (spec §Usage: "polled at most once per turn for the status line;
failures are ignored") and renders it as ``quota 37% (5h) · 12% (1w)``. The
parser of the wire shape lives here (the value objects are the
provider-neutral ones in ``config/quota.py``); the RPC itself is on
``CodexServer``.
"""

from __future__ import annotations

from ..config.quota import QuotaHint, QuotaWindow, format_window

# Re-exported: the value objects moved to config/quota.py once claude-cli grew
# a quota hint of its own; existing importers keep working.
__all__ = ["QuotaHint", "QuotaWindow", "format_window", "quota_from"]


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
