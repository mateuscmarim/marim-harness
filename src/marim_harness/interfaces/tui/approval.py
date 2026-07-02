from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Static

from .interaction_panel import InteractionPanel

# Diff highlighting styles for the approval preview.
REMOVED_STYLE = "red"
ADDED_STYLE = "green"
HEADER_STYLE = "bold"
LABEL_STYLE = "dim"


def _append_diff(detail: Text, old_string: str, new_string: str) -> None:
    """Append an old/new block: removed lines in red, then added lines in green."""
    for line in str(old_string).splitlines():
        detail.append(f"- {line}\n", style=REMOVED_STYLE)
    for line in str(new_string).splitlines():
        detail.append(f"+ {line}\n", style=ADDED_STYLE)


def format_detail(tool_name: str, args: dict) -> Text:
    """Build a styled preview of what a tool call will do, instead of dumping the
    raw args dict. Removed lines are red, added (or newly-written) lines green."""
    detail = Text()
    if tool_name == "edit_file" and isinstance(args.get("edits"), list):
        edits = args["edits"]
        detail.append(f"{args.get('path', '?')}\n\n", style=HEADER_STYLE)
        for i, edit in enumerate(edits, 1):
            if len(edits) > 1:
                detail.append(f"edit {i}:\n", style=LABEL_STYLE)
            _append_diff(detail, edit.get("old_string", ""), edit.get("new_string", ""))
            if i < len(edits):
                detail.append("\n")
        return detail
    if tool_name in ("run_command", "bash") and "command" in args:
        detail.append(f"$ {args['command']}", style=HEADER_STYLE)
        return detail
    if tool_name == "write_file" and "content" in args:
        detail.append(f"{args.get('path', '?')}\n\n", style=HEADER_STYLE)
        for line in str(args["content"]).splitlines():
            detail.append(f"{line}\n", style=ADDED_STYLE)
        return detail
    for k, v in args.items():
        detail.append(f"{k}: {v!r}\n")
    return detail


class ApprovalPanel(InteractionPanel):
    """Asks the user to approve or deny a tool call, inline above the status
    bar so the transcript stays readable. Resolves with True/False."""

    # The panel itself takes focus on mount so the a/d/Esc bindings are live
    # immediately (the modal got this from the screen's focus scope).
    can_focus = True

    DEFAULT_CSS = """
    ApprovalPanel {
        border: round $warning;
    }
    #approval-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    #approval-detail {
        height: auto;
        max-height: 20;
        margin-bottom: 1;
    }
    #approval-buttons {
        height: auto;
        align-horizontal: right;
    }
    #approval-buttons Button {
        margin-left: 2;
    }
    """

    # Esc denies — backing out of an approval is a deny, and it keeps the panel
    # consistent with ask-user, which binds Esc to cancel. Without it a
    # reflexive Esc would fall through to the app binding and cancel the whole
    # turn.
    BINDINGS = [
        ("a", "approve", "Approve"),
        ("d", "deny", "Deny"),
        ("escape", "deny", "Cancel"),
    ]

    def __init__(self, tool_name: str, args: dict) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args = args

    def compose(self) -> ComposeResult:
        yield Static(f"Approve  {self.tool_name}?", id="approval-title")
        yield Static(format_detail(self.tool_name, self.args), id="approval-detail")
        with Horizontal(id="approval-buttons"):
            yield Button("Deny (d)", id="deny", variant="error")
            yield Button("Approve (a)", id="approve", variant="success")

    def on_mount(self) -> None:
        self.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.resolve(event.button.id == "approve")

    def action_approve(self) -> None:
        self.resolve(True)

    def action_deny(self) -> None:
        self.resolve(False)
