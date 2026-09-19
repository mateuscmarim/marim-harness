"""The claude-cli quota hint: a compact reading of the ``get_usage`` control
request for the status line.

Claude Code answers ``get_usage`` with the subscription's rolling rate-limit
windows under ``rate_limits`` — ``five_hour`` and ``seven_day``, each with
``utilization`` as a whole percent (verified live on 2.1.270 with a Max
plan; ``rate_limits_available`` is False on an API-key login, where there
is nothing to show). marim asks once per turn after the ``result``
(``skip_behaviors`` so the CLI does not react to the reading itself) and
renders the shared hint: ``quota 11% (5h) · 59% (1w)``.
Pure parse helpers; the request itself is on ``ClaudeProcess``.
"""

from __future__ import annotations

from ..config.quota import QuotaHint, QuotaWindow

# The two windows the CLI names, and their lengths (the wire gives only a
# reset time, not a duration).
_WINDOWS = (("five_hour", 300), ("seven_day", 10080))


def _window(raw: object, mins: int) -> QuotaWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("utilization")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    return QuotaWindow(used_percent=int(round(used)), window_mins=mins)


def quota_from_usage(payload: object) -> QuotaHint | None:
    """A ``QuotaHint`` from a ``get_usage`` response, or None when it carries
    no window (``rate_limits_available`` False, or a malformed payload) —
    nothing to show rather than a misleading ``quota 0%``."""
    if not isinstance(payload, dict) or not payload.get("rate_limits_available", True):
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    primary, secondary = (_window(limits.get(name), mins) for name, mins in _WINDOWS)
    if primary is None and secondary is None:
        return None
    return QuotaHint(primary=primary, secondary=secondary)
