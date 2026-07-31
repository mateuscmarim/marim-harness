import json

from rich.text import Text
from textual import errors
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.dom import NoScreen
from textual.widgets import Button, Static

from .base import InteractionPanel
from .sanitize import safe_text

# Diff highlighting styles for the approval preview.
REMOVED_STYLE = "red"
ADDED_STYLE = "green"
HEADER_STYLE = "bold"
LABEL_STYLE = "dim"


def _append_diff(detail: Text, old_string: str, new_string: str) -> None:
    """Append an old/new block: removed lines in red, then added lines in green."""
    for line in safe_text(old_string).splitlines():
        detail.append(f"- {line}\n", style=REMOVED_STYLE)
    for line in safe_text(new_string).splitlines():
        detail.append(f"+ {line}\n", style=ADDED_STYLE)


def _append_workflow_script(detail: Text, args: dict) -> None:
    """Render the run_workflow script as readable Python (real newlines, not
    an escaped repr) followed by a compact JSON rendering of ``args`` when
    present, so the user can actually review the script they're approving."""
    for line in safe_text(args["script"]).splitlines():
        detail.append(f"{line}\n", style=ADDED_STYLE)
    workflow_args = args.get("args")
    if workflow_args is not None:
        detail.append("\nargs: ", style=LABEL_STYLE)
        detail.append(f"{safe_text(json.dumps(workflow_args))}\n")


# Every value below is model-authored. safe_text neutralizes terminal control
# sequences so the preview cannot be repainted into showing something other than
# what will execute — see sanitize.py for the attack this closes.
def format_detail(tool_name: str, args: dict) -> Text:
    """Build a styled preview of what a tool call will do, instead of dumping the
    raw args dict. Removed lines are red, added (or newly-written) lines green."""
    detail = Text()
    if tool_name == "edit_file" and isinstance(args.get("edits"), list):
        edits = args["edits"]
        detail.append(f"{safe_text(args.get('path', '?'))}\n\n", style=HEADER_STYLE)
        for i, edit in enumerate(edits, 1):
            if len(edits) > 1:
                detail.append(f"edit {i}:\n", style=LABEL_STYLE)
            _append_diff(detail, edit.get("old_string", ""), edit.get("new_string", ""))
            if i < len(edits):
                detail.append("\n")
        return detail
    if tool_name in ("run_command", "bash") and "command" in args:
        detail.append(f"$ {safe_text(args['command'])}", style=HEADER_STYLE)
        return detail
    if tool_name == "write_file" and "content" in args:
        detail.append(f"{safe_text(args.get('path', '?'))}\n\n", style=HEADER_STYLE)
        for line in safe_text(args["content"]).splitlines():
            detail.append(f"{line}\n", style=ADDED_STYLE)
        return detail
    if tool_name == "run_workflow" and "script" in args:
        _append_workflow_script(detail, args)
        return detail
    for k, v in args.items():
        detail.append(f"{safe_text(k)}: {safe_text(repr(v))}\n")
    return detail


class ApprovalPanel(InteractionPanel):
    """Asks the user to approve or deny a tool call, inline above the status
    bar so the transcript stays readable. Resolves with True/False."""

    # The panel itself takes focus on mount so the a/d/Esc bindings are live
    # immediately (the modal got this from the screen's focus scope).
    can_focus = True

    # Kept beside the CSS's #approval-detail max-height so the two can't drift.
    _MAX_DETAIL_ROWS = 20

    DEFAULT_CSS = """
    ApprovalPanel {
        border: round $warning;
    }
    #approval-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    /* Scrollable, not clipped: a Static at max-height silently truncates, so the
       user approves content they were never shown. The panel's own overflow only
       scrolls BETWEEN children — it cannot reveal rows inside a clamped Static.
       #approval-detail is a VerticalScroll (not the Static directly) because a
       leaf Static's virtual_size is pinned to its own allocated box — the
       compositor only grows virtual_size from *arranging children*, so only a
       container actually gets scrollable content; overflow-y on a bare Static
       is a no-op. _MAX_DETAIL_ROWS below must match the max-height here. */
    #approval-detail {
        height: auto;
        max-height: 20;
        overflow-y: auto;
        margin-bottom: 1;
    }
    #approval-detail-content {
        height: auto;
    }
    #approval-more {
        color: $text-muted;
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
        # tool_name can come from an untrusted MCP server's advertised tool
        # name (see mcp/config.py's `display = f"{label}_{name}"`), so it gets
        # the same safe_text treatment as everything in format_detail. markup=
        # False is the other half: unlike Text.append (used by format_detail),
        # a bare Static(str) parses Rich console markup, so a tool name like
        # "evil[/bold]" would otherwise raise MarkupError and crash the panel
        # before it ever mounts — leaving the approval unresolved instead of
        # just spoofed.
        yield Static(
            f"Approve  {safe_text(self.tool_name)}?", id="approval-title", markup=False
        )
        with VerticalScroll(id="approval-detail"):
            yield Static(format_detail(self.tool_name, self.args), id="approval-detail-content")
        # A scrollbar alone is easy to miss on a panel that authorizes shell
        # commands, so say how much is below the fold — same rationale as
        # AskUserPanel's "+N more options" line.
        yield Static("", id="approval-more")
        with Horizontal(id="approval-buttons"):
            yield Button("Deny (d)", id="deny", variant="error")
            yield Button("Approve (a)", id="approve", variant="success")

    def on_mount(self) -> None:
        self.focus()
        self.call_after_refresh(self._update_more_hint)

    def on_resize(self) -> None:
        # A width change re-wraps #approval-detail's content, changing its
        # virtual_size, and can also change how much of it the hosting
        # InteractionPanel clips (see _update_more_hint) — both feed the
        # hint, so redo it. Layout is already settled by the time Resize is
        # delivered (mirrors prompt.py's on_resize, which re-fits on the
        # same event for the same reason), so no call_after_refresh needed.
        #
        # This can recurse: _update_more_hint's more.display/.update() are
        # themselves layout changes, and the panel is `height: auto`, so a
        # False->True hint can itself trigger another on_resize. It
        # terminates rather than oscillating because showing the hint can
        # only ever *shrink* #approval-detail's visible rows (never grow
        # them back), so `hidden` is monotonically non-decreasing across
        # these self-triggered passes — it converges instead of flapping
        # between "hidden" and "not hidden" (same shape as prompt.py's
        # on_resize note that re-setting an unchanged height is a no-op).
        self._update_more_hint()

    def _update_more_hint(self) -> None:
        """Say how many rows are currently NOT on screen. Runs after refresh
        (mount) and on every resize — see on_resize — because the answer
        depends on layout that isn't known any earlier.

        This is deliberately not just detail.max_scroll_y. #approval-detail
        sits inside InteractionPanel, which is itself `max-height: 50%` and
        scrollable (base.py) — so #approval-detail's own 20-row box can be
        further clipped by its ancestor, and content can be hidden with
        detail.max_scroll_y == 0 (nothing to scroll to *within* detail) while
        most of that box is off-screen. Measured: a 13-row #approval-detail
        with max_scroll_y == 0 had only 8 of those rows actually painted at
        80x24 — reporting "nothing hidden" there would tell the user their
        content is fully visible when 5 rows of it are not.

        find_widget(...).visible_region is #approval-detail's on-screen
        region intersected with whatever ancestor clip applies — it already
        accounts for both detail's own scroll position and the panel's clip
        in one measurement. virtual_size.height (the full, unclamped content
        height) minus visible_region.height is exactly "rows not currently
        painted," regardless of which of the two is clipping them."""
        detail = self.query_one("#approval-detail", VerticalScroll)
        more = self.query_one("#approval-more", Static)
        try:
            visible_rows = self.screen.find_widget(detail).visible_region.height
        except (NoScreen, errors.NoWidget):
            # NoScreen: run_panel's finally calls panel.remove() without
            # awaiting it, so a Resize can still be delivered to a panel
            # that's mid-teardown and no longer on a screen. NoWidget: not
            # yet mapped by the compositor (shouldn't happen once this runs
            # post-refresh/resize, but the same degrade applies). Widget.region
            # (widget.py) guards this exact pair for the same reason. Either
            # way: degrade to detail's own scroll rather than raise out of a
            # consent surface over a hint.
            visible_rows = detail.size.height
        hidden = max(0, detail.virtual_size.height - visible_rows)
        more.display = hidden > 0
        if hidden > 0:
            # "rows", not "lines": the count is of rendered, wrapped rows —
            # a single long line that wraps to 10 rows is 1 line but 10 rows,
            # and "lines" would misstate what's actually hidden.
            more.update(f"+{hidden} more row{'s' if hidden > 1 else ''} — scroll ↓")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.resolve(event.button.id == "approve")

    def action_approve(self) -> None:
        self.resolve(True)

    def action_deny(self) -> None:
        self.resolve(False)
