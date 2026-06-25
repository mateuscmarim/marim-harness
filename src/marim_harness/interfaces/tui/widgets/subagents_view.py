"""The full-bleed sub-agents screen: a session summary bar, the agent list, and
the transcript detail host. (The container ``SubAgentsView`` is added in a later
step; this module starts with the summary bar so it can be tested on its own.)"""

from textual.content import Content
from textual.widgets import Static

from .subagent_stats import SummaryStats


class SubAgentSummary(Static):
    """The top roll-up bar: total agents (running/done/failed) + summed tokens and
    cost across the session's sub-agents."""

    def __init__(self) -> None:
        super().__init__(id="subagent-summary")

    def refresh_totals(self, stats: SummaryStats) -> None:
        left = (
            f"{stats.total} sub-agents · "
            f"{stats.running} running · {stats.done} done · {stats.failed} failed"
        )
        right = f"{stats.tokens:,} tokens"
        if stats.cost_text:
            right = f"{right} · {stats.cost_text}"
        self.update(Content(f"{left}    {right}"))
