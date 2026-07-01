"""Pure presentation helpers for the sub-agents screen — the per-agent list-row
cells and the session-summary aggregation. No Textual or app imports: this is
side-effect-free so it can be unit-tested directly (the I/O wiring lives in the
view widgets that call it)."""

from collections.abc import Callable
from dataclasses import dataclass

from .format import format_cost, human_tokens

STATUS_GLYPH = {"done": "✓", "denied": "✕", "failed": "✕"}


@dataclass(frozen=True)
class TreeRow:
    """One agent placed in the display tree: the agent, its nesting ``depth``
    (0 = a top-level spawn / list root), and whether it is the last of its
    siblings (drives the └─ vs ├─ connector)."""
    agent: object
    depth: int
    is_last: bool


def tree_order(agents: list) -> list[TreeRow]:
    """Depth-first ordering of ``agents`` by ``parent_id`` links: every agent is
    emitted immediately before its own descendants, so the flat list reads as a
    tree. An agent whose ``parent_id`` is falsy — or names an agent not in this
    list (an orphan) — is treated as a root, so nothing is ever hidden. Sibling
    order (and root order) preserves ``agents``' insertion order."""
    ids = {a.stream_id for a in agents}
    children: dict[str | None, list] = {}
    for a in agents:
        pid = a.parent_id if (a.parent_id and a.parent_id in ids) else None
        children.setdefault(pid, []).append(a)
    rows: list[TreeRow] = []

    def walk(parent_id: str | None, depth: int) -> None:
        kids = children.get(parent_id, [])
        for i, a in enumerate(kids):
            rows.append(TreeRow(a, depth, i == len(kids) - 1))
            walk(a.stream_id, depth + 1)

    walk(None, 0)
    return rows


def _row_prefix(depth: int, is_last: bool) -> str:
    """The tree connector/indent for the ``agent`` cell. Root rows get no prefix;
    a nested row gets two spaces of indent per ancestor level below the root plus
    a └─ (last sibling) or ├─ connector."""
    if depth == 0:
        return ""
    return "  " * (depth - 1) + ("└─ " if is_last else "├─ ")


def status_glyph(status: str) -> str:
    """The list glyph for a sub-agent status; running agents get a ▸ marker."""
    return STATUS_GLYPH.get(status, "▸")


def row_cells(agent, prefix: str = "") -> list[str]:
    """The six `DataTable` cells for one agent row: glyph, "{prefix}{type} —
    {title}", tool count, tokens, cost, duration. ``prefix`` carries the tree
    connector for a nested row (empty for a top-level spawn). A background
    (detached) agent keeps its quiet "bg · " tag between the prefix and label."""
    label = f"{agent.agent_type} — {agent.display_title()}"
    if agent.detached:
        label = f"bg · {label}"
    label = f"{prefix}{label}"
    tokens = human_tokens(agent.tokens) if agent.tokens else ""
    return [
        status_glyph(agent.status),
        label,
        str(agent.tool_count),
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
