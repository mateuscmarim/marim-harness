"""Compact formatting helpers for token counts, costs, and durations in the status chrome."""

# Spinner characters and tick interval — defined here (a leaf module) so that
# tools.py can import them without pulling in status.py, breaking the circular
# import that otherwise forms: tools → status → widgets → tools.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_TICK_INTERVAL = 0.1


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


def human_tokens(n: int) -> str:
    """Compact token count: 950 -> '950', 1500 -> '1.5k', 100000 -> '100k',
    1500000 -> '1.5M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def format_cost(cost: float) -> str:
    """Render a USD cost compactly — four decimals below a cent so small spends
    don't collapse to ``$0.00``, two decimals above: ``$0.0042``, ``$0.07``."""
    return f"${cost:.4f}" if cost < 0.01 else f"${cost:.2f}"


def format_token_split(usage) -> str:
    """The compact status-bar token split: ``1k↑ 55k⚡ 2k↓`` — ``↑`` uncached
    input, ``⚡`` cached (read + write), ``↓`` output. All three buckets always
    render (even at zero) so the bar keeps a stable width."""
    from ....usage import split_tokens

    s = split_tokens(usage)
    return (
        f"{human_tokens(s.uncached_input)}↑ "
        f"{human_tokens(s.cached_input)}⚡ "
        f"{human_tokens(s.output)}↓"
    )
