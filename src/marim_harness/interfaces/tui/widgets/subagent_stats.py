"""Pure presentation helpers for the sub-agents screen — the per-agent list-row
cells and the session-summary aggregation. No Textual or app imports: this is
side-effect-free so it can be unit-tested directly (the I/O wiring lives in the
view widgets that call it)."""

from collections.abc import Callable
from dataclasses import dataclass

from .format import format_cost, human_tokens

STATUS_GLYPH = {"done": "✓", "denied": "✕", "failed": "✕"}


def status_glyph(status: str) -> str:
    """The list glyph for a sub-agent status; running agents get a ▸ marker."""
    return STATUS_GLYPH.get(status, "▸")


def row_cells(agent) -> list[str]:
    """The six `DataTable` cells for one agent row: glyph, "{type} — {title}",
    tool count, tokens, cost, duration. A detached agent's tool tally is unknown
    (it never streamed its steps), so it shows "—" rather than a misleading "0"."""
    tools = "—" if agent.detached else str(agent.tool_count)
    tokens = human_tokens(agent.tokens) if agent.tokens else ""
    return [
        status_glyph(agent.status),
        f"{agent.agent_type} — {agent.display_title()}",
        tools,
        tokens,
        agent.cost_text or "",
        agent._duration(),
    ]


@dataclass
class SummaryStats:
    total: int
    running: int
    done: int
    failed: int
    tokens: int
    cost_text: str


def aggregate(agents: list, cost_of: Callable[[object], float]) -> SummaryStats:
    """Roll up the session's sub-agents for the summary bar. ``cost_of`` maps an
    agent to its dollar cost (injected so this stays free of usage/model wiring).
    A failed *or* denied agent counts as failed; everything not terminal is
    running. Cost is blank until at least one agent is metered."""
    running = done = failed = tokens = 0
    cost = 0.0
    for a in agents:
        tokens += a.tokens
        cost += cost_of(a)
        if a.status == "done":
            done += 1
        elif a.status in ("failed", "denied"):
            failed += 1
        else:
            running += 1
    return SummaryStats(
        total=len(agents),
        running=running,
        done=done,
        failed=failed,
        tokens=tokens,
        cost_text=format_cost(cost) if tokens else "",
    )
