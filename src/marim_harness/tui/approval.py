from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


def format_detail(tool_name: str, args: dict) -> str:
    """Render a human-readable preview of what a tool call will do, instead of
    dumping the raw args dict."""
    if tool_name == "edit_file" and isinstance(args.get("edits"), list):
        path = args.get("path", "?")
        edits = args["edits"]
        blocks = []
        for i, edit in enumerate(edits, 1):
            old = "\n".join(
                f"- {line}" for line in str(edit.get("old_string", "")).splitlines()
            )
            new = "\n".join(
                f"+ {line}" for line in str(edit.get("new_string", "")).splitlines()
            )
            header = f"edit {i}:\n" if len(edits) > 1 else ""
            blocks.append(f"{header}{old}\n{new}")
        return f"{path}\n\n" + "\n\n".join(blocks)
    if tool_name in ("run_command", "bash") and "command" in args:
        return f"$ {args['command']}"
    if tool_name == "write_file" and "content" in args:
        path = args.get("path", "?")
        return f"{path}\n\n{args['content']}"
    return "\n".join(f"{k}: {v!r}" for k, v in args.items())


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

    BINDINGS = [("a", "approve", "Approve"), ("d", "deny", "Deny")]

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
