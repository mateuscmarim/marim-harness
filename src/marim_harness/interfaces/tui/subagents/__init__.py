"""The sub-agents UI: screen controller, list, full-bleed view, inline card,
transcript pane, pure stats."""

from .card import SubAgentWidget
from .list import SubAgentList
from .pane import SubAgentDetailHost, SubAgentPane
from .screen import SubAgentsScreen
from .view import SubAgentSummary, SubAgentsView

__all__ = [
    "SubAgentWidget",
    "SubAgentList",
    "SubAgentPane",
    "SubAgentDetailHost",
    "SubAgentSummary",
    "SubAgentsView",
    "SubAgentsScreen",
]
