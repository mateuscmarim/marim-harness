"""TUI log/input widgets.

Split into focused modules — diff rendering, syntax highlighting, formatting,
tool/message/panel widgets, and the prompt input — but re-exported here
so callers keep importing ``from .widgets import X`` unchanged.
"""

from .autocomplete import CommandAutocomplete
from .diff import (
    EditDiff,
    _reverse_edits,
    compute_diff_rows,
    render_edit_diff,
    render_file_diff,
)
from .format import format_cost, format_token_split, human_tokens
from .highlight import _highlight_lines, _strip_bg, strip_line_numbers
from .messages import (
    AssistantMessage,
    ErrorMessage,
    NoticeMessage,
    SummaryWidget,
    ThinkingWidget,
    TurnMeta,
    UserMessage,
)
from .panels import JobPanel, QueuePanel, TaskPanel
from .prompt import PromptInput
from .tools import ToolCallWidget, ToolGroupWidget

__all__ = [
    # diff rendering
    "EditDiff",
    "compute_diff_rows",
    "render_edit_diff",
    "render_file_diff",
    "_reverse_edits",
    # syntax highlighting
    "strip_line_numbers",
    "_highlight_lines",
    "_strip_bg",
    # formatting
    "format_cost",
    "format_token_split",
    "human_tokens",
    # tool widgets
    "ToolCallWidget",
    "ToolGroupWidget",
    # log messages
    "AssistantMessage",
    "ErrorMessage",
    "NoticeMessage",
    "SummaryWidget",
    "ThinkingWidget",
    "TurnMeta",
    "UserMessage",
    # panels
    "JobPanel",
    "QueuePanel",
    "TaskPanel",
    # input
    "PromptInput",
    "CommandAutocomplete",
]
