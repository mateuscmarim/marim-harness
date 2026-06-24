"""Compact formatting helpers for token counts and costs in the status chrome."""

# Max chars of a tool-call preview on a sub-agent card's activity line before it's
# ellipsized, so a long path/command can't run off the card's edge.
_PREVIEW_CAP = 60


def tool_preview(args: dict) -> str:
    """A short preview of a tool call's target for a sub-agent card's ``↳`` line —
    the first meaningful argument value (a path, command, or pattern, which by tool
    signature comes first). Clipped; empty when there's nothing useful to show."""
    items = [v for v in args.values() if v not in (None, "", [], {})]
    if not items:
        return ""
    preview = " ".join(str(items[0]).split())  # first value, whitespace collapsed
    return preview if len(preview) <= _PREVIEW_CAP else preview[: _PREVIEW_CAP - 1] + "…"


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
