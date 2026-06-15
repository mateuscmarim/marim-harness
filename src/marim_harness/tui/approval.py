from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ApprovalModal(ModalScreen[bool]):
    """Asks the user to approve or deny a tool call. Dismisses with True/False."""

    BINDINGS = [("a", "approve", "Approve"), ("d", "deny", "Deny")]

    def __init__(self, tool_name: str, args: dict) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.args = args

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            yield Static(f"Approve {self.tool_name}?")
            yield Static(str(self.args))
            yield Button("Approve (a)", id="approve", variant="success")
            yield Button("Deny (d)", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "approve")

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
