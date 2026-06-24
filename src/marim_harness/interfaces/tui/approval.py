from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

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


class ApprovalModal(ModalScreen[bool]):
    """Asks the user to approve or deny a tool call. Dismisses with True/False."""

    CSS = """
    ApprovalModal {
        align: center middle;
    }
    #approval-box {
        width: 70%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $warning;
        background: $surface;
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

    # Esc denies — backing out of an approval is a deny, and it keeps the modal
    # consistent with every other modal (model picker, ask-user, settings), which
    # all bind Esc to cancel. Without it a reflexive Esc does nothing and traps you.
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
        with Vertical(id="approval-box"):
            yield Static(f"Approve  {self.tool_name}?", id="approval-title")
            yield Static(format_detail(self.tool_name, self.args), id="approval-detail")
            with Horizontal(id="approval-buttons"):
                yield Button("Deny (d)", id="deny", variant="error")
                yield Button("Approve (a)", id="approve", variant="success")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
