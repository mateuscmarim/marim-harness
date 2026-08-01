"""The `!` prompt passthrough: run a shell command locally, Claude-Code style.

Pure helpers (parse, sudo detection/rewrite, transcript formatting) live here so
they're unit-testable without an app; `app.py` keeps only the thin wiring. The
sudo path exists because the TUI's subprocesses have no controlling terminal —
sudo cannot prompt on its own, so the modal collects the password and
:func:`run_passthrough` feeds it via ``sudo -S`` on stdin."""

import re
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ...tools.impl.shell import run_bash
from .interactions.sanitize import safe_text

# Human-run commands get more room than the model's 30s tool default: a user
# knowingly kicks off installs/builds and watches them, so a short leash only
# annoys. Still bounded — a wedged command must not hold the worker forever.
PASSTHROUGH_TIMEOUT = 120


def parse_bang(text: str) -> str | None:
    """The shell command in a `!`-prefixed submission, or ``None`` when ``text``
    isn't a passthrough. ``! git status`` and ``!git status`` both parse; a bare
    ``!`` returns ``""`` so the caller can show a usage hint instead of running
    an empty command."""
    if not text.startswith("!"):
        return None
    return text[1:].strip()


def needs_sudo_password(command: str) -> bool:
    """True when the command's leading token is exactly ``sudo`` — the case the
    password modal covers. ``sudo`` mid-pipeline is deliberately out of scope
    (spec): it fails with sudo's own "no tty" error, which is honest and safe."""
    parts = command.split(None, 1)
    return bool(parts) and parts[0] == "sudo"


def rewrite_sudo(command: str) -> str:
    """Rewrite a leading-``sudo`` command to take its password on stdin.

    ``-S`` reads the password from stdin; ``-p ''`` suppresses the prompt string
    so it can't pollute the captured output; ``-k`` forces re-authentication so
    sudo ALWAYS consumes the password we pipe — with a cached credential sudo
    would skip reading stdin and the password line would fall through to the
    wrapped command's stdin (imagine ``sudo tee``), which must never happen."""
    parts = command.split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    return f"sudo -S -p '' -k {rest}".rstrip()


def format_transcript_block(command: str, output: str) -> str:
    """Markdown for the transcript: the command echoed as typed, then the
    ``exit N`` + output fenced verbatim. The fence is sized to exceed the
    longest backtick run in the output, so output that itself contains ```
    (a README, a markdown file catted to the terminal) can't break out of
    the code block and render as markdown."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", output)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"`! {command}`\n\n{fence}text\n{output}\n{fence}"


async def run_passthrough(
    root: Path, command: str, password: str | None = None
) -> str:
    """Execute a `!` command in the workspace root and return run_bash's
    ``exit N\\n<output>`` text. A ``password`` (the sudo case) rewrites the
    command via :func:`rewrite_sudo` and feeds it through the stdin pipe; it
    never appears in the returned text or anywhere else."""
    to_run = command
    stdin_data = None
    if password is not None:
        to_run = rewrite_sudo(command)
        stdin_data = (password + "\n").encode()
    return await run_bash(root, to_run, timeout=PASSTHROUGH_TIMEOUT,
                          stdin_data=stdin_data)


class SudoPasswordModal(ModalScreen[str | None]):
    """Collects the sudo password for a `!` passthrough command. Dismisses with
    the password, or ``None`` when cancelled (Esc / Cancel / empty submit). The
    password is never echoed, logged, or persisted — it only transits the
    subprocess's stdin pipe."""

    CSS = """
    SudoPasswordModal {
        align: center middle;
    }
    #sudo-box {
        width: 60%;
        max-width: 80;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    #sudo-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    #sudo-command {
        margin-bottom: 1;
    }
    #sudo-buttons {
        height: auto;
        align-horizontal: right;
        margin-top: 1;
    }
    #sudo-buttons Button {
        margin-left: 2;
    }
    """

    # Esc cancels, consistent with every other modal (approval, ask-user,
    # model picker) — a reflexive Esc must never trap the user.
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    def compose(self) -> ComposeResult:
        with Vertical(id="sudo-box"):
            yield Static("sudo password required", id="sudo-title")
            # markup=False: the command is user-typed. A '[/]' in it raises
            # MarkupError during render (killing the app), and a '[b]…[/b]' would
            # render as styling while the bracketed text is what actually runs —
            # a display that disagrees with the command being authorized.
            # safe_text: this is a consent surface too (the user is about to
            # authorize a sudo command), so it gets the same ANSI-neutralizing
            # treatment as the approval preview — defense-in-depth, since the
            # command is user-typed today but nothing structural stops a future
            # caller from routing model-influenced text through this modal.
            yield Static(f"$ {safe_text(self.command)}", id="sudo-command", markup=False)
            yield Input(password=True, placeholder="password", id="sudo-password")
            with Horizontal(id="sudo-buttons"):
                yield Button("Cancel (esc)", id="sudo-cancel", variant="error")
                yield Button("Run", id="sudo-run", variant="success")

    def on_mount(self) -> None:
        self.query_one("#sudo-password", Input).focus()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sudo-run":
            self._submit()
        else:
            self.dismiss(None)

    def _submit(self) -> None:
        # An empty submit is a cancel, not an empty password — sudo would just
        # fail, and None lets the caller skip the doomed run entirely.
        self.dismiss(self.query_one("#sudo-password", Input).value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)
