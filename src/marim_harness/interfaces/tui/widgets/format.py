"""Compact formatting helpers for token counts, costs, and durations in the status chrome."""

# format_duration lives in the top-level ``durations`` leaf (stdlib only) so the
# CLI can format durations without importing this Textual-laden package; re-export
# it here to keep the TUI's ``from .format import format_duration`` paths working.
from ...durations import format_duration  # noqa: F401  (re-export)

# Spinner characters and tick interval — defined here (a leaf module) so that
# tools.py can import them without pulling in status.py, breaking the circular
# import that otherwise forms: tools → status → widgets → tools.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_TICK_INTERVAL = 0.1


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
