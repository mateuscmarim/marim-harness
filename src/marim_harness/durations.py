"""Human-readable elapsed-time formatting.

A top-level leaf module (stdlib only, no Textual) so the CLI — e.g.
``marim sessions list`` — can format durations without importing the TUI and
pulling Textual in transitively. The TUI re-exports ``format_duration`` from
``interfaces/tui/widgets/format.py`` so its own ``from .format import …`` paths
are unchanged.
"""


def format_duration(seconds: float, *, precise: bool = False) -> str:
    """Human-readable elapsed time. ``precise`` (for the per-turn stamp) keeps a
    decimal under a minute (``12.4s``); otherwise whole units (``12s``, ``3m``,
    ``1h 5m``)."""
    if seconds < 60:
        return f"{seconds:.1f}s" if precise else f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"
