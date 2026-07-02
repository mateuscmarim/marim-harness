# `!` Shell Passthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user run shell commands from the TUI prompt with a leading `!` (Claude Code style); output renders in the transcript and is injected into the model's context on the next turn; leading-`sudo` commands get a masked modal password prompt fed via `sudo -S` stdin.

**Architecture:** Intercept `!` in `on_prompt_input_submitted` before slash dispatch and run the command via the existing `run_bash` (extended with an optional `stdin_data` pipe). Results queue on the `TurnController` and drain into the `<turn-context>` prompt prefix in `_assemble_prompt` — nothing synthetic enters history. A small `interfaces/tui/shell_passthrough.py` module holds the pure helpers + password modal, keeping `app.py` wiring thin (project convention: pure helpers unit-tested, I/O in the thin interface layer).

**Tech Stack:** Python (>=3.10), asyncio subprocess, Textual (ModalScreen, workers), pytest + pytest-anyio, Pilot for TUI tests.

**Spec:** `docs/superpowers/specs/2026-07-01-shell-passthrough-design.md` — read it before starting any task.

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax.
- Use `uv` for everything: `uv run pytest …`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pytest`/`pip`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- Verification order before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Single-test runs: `uv run pytest --no-cov tests/test_x.py::test_name -v` (skips coverage for speed). The full suite has a 90% coverage gate — run it without `--no-cov` at the end.
- The command policy is deliberately **not** applied to `!` commands (it gates the model, not the user).
- The sudo password must never be rendered, logged, persisted, or included in any prompt/context block.
- This codebase favors long explanatory *why* comments around invariants — preserve existing ones and match that style in new code.

---

### Task 1: `stdin_data` parameter on `run_bash`

**Files:**
- Modify: `src/marim_harness/tools/shell.py:99-114` (the `run_bash` signature and spawn)
- Test: `tests/test_shell.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `async def run_bash(root: Path, command: str, timeout: int = 30, stdin_data: bytes | None = None) -> str`. When `stdin_data is None` behavior is byte-for-byte identical to today (no stdin pipe). Task 4's `run_passthrough` relies on this exact signature.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_shell.py`:

```python
@pytest.mark.anyio
async def test_run_bash_stdin_data_reaches_the_process(tmp_path: Path):
    """stdin_data is piped to the command's stdin, written once, then closed
    (cat exits on EOF instead of hanging)."""
    out = await shell.run_bash(tmp_path, "cat", stdin_data=b"hello-stdin\n")
    assert out.startswith("exit 0")
    assert "hello-stdin" in out


@pytest.mark.anyio
async def test_run_bash_without_stdin_data_is_unchanged(tmp_path: Path):
    """Default None wires no stdin pipe — a command reading stdin sees EOF-ish
    inherited stdin, and plain commands behave exactly as before."""
    out = await shell.run_bash(tmp_path, "echo no-stdin")
    assert out.startswith("exit 0")
    assert "no-stdin" in out
```

- [ ] **Step 2: Run tests to verify the new one fails**

Run: `uv run pytest --no-cov tests/test_shell.py::test_run_bash_stdin_data_reaches_the_process -v`
Expected: FAIL — `TypeError: run_bash() got an unexpected keyword argument 'stdin_data'`

- [ ] **Step 3: Implement**

In `src/marim_harness/tools/shell.py`, change the `run_bash` signature and spawn (lines 99-114). The existing docstring stays; add the stdin sentence:

```python
async def run_bash(
    root: Path,
    command: str,
    timeout: int = _DEFAULT_TIMEOUT,
    stdin_data: bytes | None = None,
) -> str:
    """Run a shell command in the workspace root, capturing combined output.

    Runs in its own session so a timeout can signal the whole process group and
    take down any children the command spawned, not just the shell.

    ``stdin_data`` (when given) is piped to the command's stdin in one write and
    the pipe is closed immediately, so a reader sees the bytes then EOF. With the
    default ``None`` no stdin pipe is wired at all — identical to the historical
    behavior."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    if stdin_data is not None and proc.stdin is not None:
        # One small write (a sudo password, a heredoc-ish snippet), then close so
        # the child sees EOF. Suppress pipe errors: a command that exits without
        # reading stdin (or dies at spawn) must not crash the runner — its own
        # exit code / output is the signal the caller cares about.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.close()
    # Read stdout line-by-line instead of using proc.communicate() so we can
    ...  # (rest of the function is unchanged)
```

Only the signature, the `stdin=` line, and the new write/close block change — everything from the `chunks = _BoundedOutput(...)` line down is untouched.

- [ ] **Step 4: Run the shell tests**

Run: `uv run pytest --no-cov tests/test_shell.py -v`
Expected: ALL PASS (new tests plus every pre-existing `run_bash`/`BashProcess` test).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/tools/shell.py tests/test_shell.py
uv run pyright
git add src/marim_harness/tools/shell.py tests/test_shell.py
git commit -m "feat(shell): optional stdin_data pipe on run_bash"
```

---

### Task 2: `render_shell_results_block` context helper

**Files:**
- Modify: `src/marim_harness/runtime/context.py` (add one function; add `from collections.abc import Sequence` to imports if not already present)
- Test: `tests/test_context.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `def render_shell_results_block(results: Sequence[tuple[str, str]], dropped: int = 0) -> str` — returns `""` when `results` is empty (matching `render_checklist_block`'s falsy-when-empty contract so the controller can `if block:`). Task 3 imports it from `.context`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_context.py` (match the file's existing import style — it imports from `marim_harness.runtime.context`):

```python
from marim_harness.runtime.context import render_shell_results_block


def test_render_shell_results_block_empty_is_falsy():
    assert render_shell_results_block([]) == ""


def test_render_shell_results_block_formats_commands_and_output():
    block = render_shell_results_block(
        [("git status", "exit 0\nclean"), ("ls", "exit 0\nfoo.py")]
    )
    assert block.startswith("<user-shell-commands>")
    assert block.endswith("</user-shell-commands>")
    assert "$ git status" in block
    assert "exit 0\nclean" in block
    assert "$ ls" in block
    assert "elided" not in block  # no drop marker when nothing was dropped


def test_render_shell_results_block_notes_dropped_entries():
    block = render_shell_results_block([("ls", "exit 0\nfoo.py")], dropped=2)
    assert "2 earlier command(s) elided" in block
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_context.py -v -k shell_results`
Expected: FAIL — `ImportError: cannot import name 'render_shell_results_block'`

- [ ] **Step 3: Implement**

Add to `src/marim_harness/runtime/context.py` (near `render_checklist_block`; add the `Sequence` import at the top if missing):

```python
def render_shell_results_block(
    results: Sequence[tuple[str, str]], dropped: int = 0
) -> str:
    """The ``<user-shell-commands>`` block for the turn-context envelope, or
    ``""`` when there is nothing to show (falsy-when-empty, matching
    :func:`render_checklist_block` so callers can ``if block:``).

    Each entry is ``(command, output)`` from the TUI's ``!`` passthrough —
    commands the user ran themselves, whose output is already on their screen.
    The block exists so the model can see what the user saw. ``dropped`` counts
    entries the controller's budget cap elided; it is surfaced as a marker line
    so the model knows the list is incomplete rather than assuming it saw
    everything."""
    if not results:
        return ""
    lines = [
        "<user-shell-commands>",
        "The user ran these commands directly in their own shell (via the `!` "
        "prompt passthrough). The outputs are shown verbatim and are already "
        "visible to the user.",
    ]
    if dropped:
        lines.append(f"({dropped} earlier command(s) elided to fit the context budget)")
    for command, output in results:
        lines.append(f"$ {command}")
        lines.append(output)
    lines.append("</user-shell-commands>")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_context.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/runtime/context.py tests/test_context.py
uv run pyright
git add src/marim_harness/runtime/context.py tests/test_context.py
git commit -m "feat(context): render_shell_results_block for the turn envelope"
```

---

### Task 3: Pending queue on TurnController + Harness delegate

**Files:**
- Modify: `src/marim_harness/runtime/controller.py` (imports at line ~37-41, `__init__` at 234-260, `_assemble_prompt` at 310-374)
- Modify: `src/marim_harness/runtime/harness.py` (delegate method near the `steer` delegate at ~509)
- Test: `tests/test_turn_controller.py`

**Interfaces:**
- Consumes: `render_shell_results_block(results, dropped)` from Task 2 (import from `.context`).
- Produces: `TurnController.add_shell_result(command: str, output: str) -> None` and `Harness.add_shell_result(command: str, output: str) -> None` (a plain delegate). Task 5's app wiring calls `self.harness.add_shell_result(...)`. Internal state: `self._pending_shell_results: list[tuple[str, str]]`, `self._shell_results_dropped: int`, module constant `_SHELL_RESULTS_BUDGET = 20_000`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_turn_controller.py` (it already defines `_make_tc(model, tmp_path)` and imports `FunctionModel`, `ModelResponse`, `TextPart`, `pytest`):

```python
def _ok_model():
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_assemble_prompt_injects_pending_shell_results(tmp_path):
    """A queued `!` result rides the next turn's injected prefix, is consumed
    by that drain, and never re-injects on the following turn."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.add_shell_result("git status", "exit 0\nclean")
    prompt = await tc._assemble_prompt("what changed?")
    assert "<user-shell-commands>" in prompt
    assert "$ git status" in prompt
    assert "exit 0\nclean" in prompt
    assert "what changed?" in prompt
    prompt2 = await tc._assemble_prompt("and now?")
    assert "<user-shell-commands>" not in prompt2


@pytest.mark.anyio
async def test_shell_results_budget_drops_oldest_with_marker(tmp_path):
    """The pending queue is bounded: oldest entries fall off past the character
    budget and the block says how many were elided."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.add_shell_result("first-command", "x" * 15_000)
    tc.add_shell_result("second-command", "y" * 15_000)
    prompt = await tc._assemble_prompt("hi")
    assert "$ first-command" not in prompt
    assert "$ second-command" in prompt
    assert "1 earlier command(s) elided" in prompt


@pytest.mark.anyio
async def test_shell_results_keep_newest_even_if_oversized(tmp_path):
    """A single oversized entry is never dropped to zero — run_bash already caps
    individual outputs, so the newest result is always kept."""
    tc = _make_tc(_ok_model(), tmp_path)
    tc.add_shell_result("big", "z" * 50_000)
    prompt = await tc._assemble_prompt("hi")
    assert "$ big" in prompt


def test_harness_add_shell_result_delegates(tmp_path):
    from pydantic_ai.models.test import TestModel

    deps = _make_deps(tmp_path)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="t"
    )
    harness.add_shell_result("echo hi", "exit 0\nhi")
    assert harness.turn_controller._pending_shell_results == [("echo hi", "exit 0\nhi")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_turn_controller.py -v -k "shell_result"`
Expected: FAIL — `AttributeError: 'TurnController' object has no attribute 'add_shell_result'`

- [ ] **Step 3: Implement the controller side**

In `src/marim_harness/runtime/controller.py`:

(a) Extend the existing `.context` import block (lines ~37-41) to also import `render_shell_results_block`:

```python
from .context import (
    plan_mode_preamble,
    render_checklist_block,
    render_shell_results_block,
    wrap_turn_context,
)
```

(b) Add a module constant near `logger = logging.getLogger(__name__)`:

```python
# Total character budget for pending `!` passthrough results awaiting the next
# turn. run_bash caps each individual output, but a burst of `!` commands could
# still stack an unbounded prefix onto one prompt — the queue drops oldest
# entries past this, and the rendered block notes how many were elided.
_SHELL_RESULTS_BUDGET = 20_000
```

(c) In `__init__` (after the `self._consumed_this_turn` line at ~256), add:

```python
        # `!` passthrough results awaiting the next turn's prompt (see
        # add_shell_result). Not restored on turn failure: the outputs are still
        # on the user's screen and re-runnable, unlike hook context / digests.
        self._pending_shell_results: list[tuple[str, str]] = []
        self._shell_results_dropped = 0
```

(d) Add the method after `clear_pending_jobs_digest` (~line 268):

```python
    def add_shell_result(self, command: str, output: str) -> None:
        """Queue a user-run `!` passthrough result for the next turn's prompt.

        Bounded: once the pending set exceeds the character budget the oldest
        entries are dropped (and counted, so the rendered block can say so) —
        a burst of `!` commands must not stack an unbounded prefix onto the
        next prompt. The newest entry is always kept even if it alone exceeds
        the budget; run_bash already caps any single output."""
        self._pending_shell_results.append((command, output))
        total = sum(len(c) + len(o) for c, o in self._pending_shell_results)
        while total > _SHELL_RESULTS_BUDGET and len(self._pending_shell_results) > 1:
            c, o = self._pending_shell_results.pop(0)
            total -= len(c) + len(o)
            self._shell_results_dropped += 1
```

(e) In `_assemble_prompt`, insert the drain right after the plan-mode preamble block (after line 322's `prompt = f"{plan_mode_preamble()}\n\n{prompt}"` block, before the checklist block). It follows the same `f"{block}\n\n{prompt}"` shape every other prepend uses, preserving the typed-is-a-suffix invariant:

```python
        # Commands the user ran via the `!` passthrough since the last turn.
        # Their outputs are already on the user's screen; this drain makes them
        # model-visible. Consumed here (not restored on failure — the user can
        # re-run a ! command, unlike hook context).
        shell_block = render_shell_results_block(
            self._pending_shell_results, self._shell_results_dropped
        )
        if shell_block:
            prompt = f"{shell_block}\n\n{prompt}"
            self._pending_shell_results = []
            self._shell_results_dropped = 0
```

- [ ] **Step 4: Implement the Harness delegate**

In `src/marim_harness/runtime/harness.py`, next to the existing `steer` delegate (~line 509):

```python
    def add_shell_result(self, command: str, output: str) -> None:
        """Queue a user-run `!` passthrough result for the next turn's context.
        Delegates to the turn controller's pending queue."""
        self.turn_controller.add_shell_result(command, output)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest --no-cov tests/test_turn_controller.py -v`
Expected: ALL PASS (new tests plus all pre-existing controller tests — the drain must not disturb the existing `_assemble_prompt` tests).

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/runtime tests/test_turn_controller.py
uv run pyright
git add src/marim_harness/runtime/controller.py src/marim_harness/runtime/harness.py tests/test_turn_controller.py
git commit -m "feat(runtime): pending !-passthrough results drain into turn context"
```

---

### Task 4: `shell_passthrough` TUI module (helpers + sudo modal)

**Files:**
- Create: `src/marim_harness/interfaces/tui/shell_passthrough.py`
- Test: `tests/test_shell_passthrough.py`

**Interfaces:**
- Consumes: `run_bash(root, command, timeout=..., stdin_data=...)` from Task 1 (import `from ...tools.shell import run_bash`).
- Produces (Task 5 imports all of these from `.shell_passthrough`):
  - `parse_bang(text: str) -> str | None` — command for a `!` submission (`""` for bare `!`), `None` for non-`!` text.
  - `needs_sudo_password(command: str) -> bool`
  - `rewrite_sudo(command: str) -> str`
  - `format_transcript_block(command: str, output: str) -> str`
  - `async run_passthrough(root: Path, command: str, password: str | None = None) -> str`
  - `PASSTHROUGH_TIMEOUT = 120`
  - `class SudoPasswordModal(ModalScreen[str | None])` — dismisses with the password, or `None` on cancel.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_shell_passthrough.py`:

```python
from pathlib import Path

import pytest
from textual.app import App

from marim_harness.interfaces.tui.shell_passthrough import (
    PASSTHROUGH_TIMEOUT,
    SudoPasswordModal,
    format_transcript_block,
    needs_sudo_password,
    parse_bang,
    rewrite_sudo,
    run_passthrough,
)


def test_parse_bang_with_and_without_space():
    assert parse_bang("! git status") == "git status"
    assert parse_bang("!git status") == "git status"


def test_parse_bang_bare_bang_is_empty_string():
    assert parse_bang("!") == ""


def test_parse_bang_non_bang_text_is_none():
    assert parse_bang("git status") is None
    assert parse_bang("/help") is None
    assert parse_bang("") is None


def test_needs_sudo_password_matches_leading_token_only():
    assert needs_sudo_password("sudo systemctl restart nginx")
    assert not needs_sudo_password("echo sudo")
    assert not needs_sudo_password("sudoedit /etc/hosts")
    # Mid-pipeline sudo is out of scope (spec): it fails with sudo's own error.
    assert not needs_sudo_password("foo | sudo tee /etc/x")


def test_rewrite_sudo_inserts_stdin_flags():
    assert rewrite_sudo("sudo apt update") == "sudo -S -p '' -k apt update"


def test_format_transcript_block_echoes_command_and_fences_output():
    block = format_transcript_block("echo hi", "exit 0\nhi")
    assert "! echo hi" in block
    assert "```" in block
    assert "exit 0\nhi" in block


@pytest.mark.anyio
async def test_run_passthrough_runs_plain_command(tmp_path: Path):
    out = await run_passthrough(tmp_path, "echo pass-through")
    assert out.startswith("exit 0")
    assert "pass-through" in out


@pytest.mark.anyio
async def test_run_passthrough_password_feeds_stdin_never_output(
    tmp_path: Path, monkeypatch
):
    """Real sudo can't run in tests: capture the run_bash call instead and
    assert the rewrite + stdin plumbing, and that the password can't leak into
    the returned text."""
    captured = {}

    async def fake_run_bash(root, command, timeout=30, stdin_data=None):
        captured["command"] = command
        captured["timeout"] = timeout
        captured["stdin"] = stdin_data
        return "exit 0\nroot"

    monkeypatch.setattr(
        "marim_harness.interfaces.tui.shell_passthrough.run_bash", fake_run_bash
    )
    out = await run_passthrough(tmp_path, "sudo whoami", password="hunter2")
    assert captured["command"] == "sudo -S -p '' -k whoami"
    assert captured["stdin"] == b"hunter2\n"
    assert captured["timeout"] == PASSTHROUGH_TIMEOUT
    assert "hunter2" not in out


class _ModalHost(App):
    """Bare host app so the modal can be driven with Pilot."""


@pytest.mark.anyio
async def test_sudo_password_modal_submits_password():
    app = _ModalHost()
    async with app.run_test() as pilot:
        results: list = []
        app.push_screen(SudoPasswordModal("sudo whoami"), results.append)
        await pilot.pause()
        await pilot.press(*"hunter2", "enter")  # Input is focused on mount
        await pilot.pause()
        assert results == ["hunter2"]


@pytest.mark.anyio
async def test_sudo_password_modal_escape_cancels():
    app = _ModalHost()
    async with app.run_test() as pilot:
        results: list = []
        app.push_screen(SudoPasswordModal("sudo whoami"), results.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert results == [None]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_shell_passthrough.py -v`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.shell_passthrough'`

- [ ] **Step 3: Implement**

Create `src/marim_harness/interfaces/tui/shell_passthrough.py`:

```python
"""The `!` prompt passthrough: run a shell command locally, Claude-Code style.

Pure helpers (parse, sudo detection/rewrite, transcript formatting) live here so
they're unit-testable without an app; `app.py` keeps only the thin wiring. The
sudo path exists because the TUI's subprocesses have no controlling terminal —
sudo cannot prompt on its own, so the modal collects the password and
:func:`run_passthrough` feeds it via ``sudo -S`` on stdin."""

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ...tools.shell import run_bash

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
    ``exit N`` + output fenced verbatim."""
    return f"`! {command}`\n\n```text\n{output}\n```"


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
            yield Static(f"$ {self.command}", id="sudo-command")
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest --no-cov tests/test_shell_passthrough.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/interfaces/tui/shell_passthrough.py tests/test_shell_passthrough.py
uv run pyright
git add src/marim_harness/interfaces/tui/shell_passthrough.py tests/test_shell_passthrough.py
git commit -m "feat(tui): shell passthrough helpers and sudo password modal"
```

---

### Task 5: Wire `!` interception into the app

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (imports; `on_prompt_input_submitted` at ~868-888; two new methods next to it)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: everything Task 4 produces (`parse_bang`, `needs_sudo_password`, `run_passthrough`, `format_transcript_block`, `SudoPasswordModal`) and Task 3's `Harness.add_shell_result`. Also existing app members: `self.turn_busy` (property: `self._turn_worker is not None or self._turn_starting`), `self.post_system(markdown)`, `self._append_log(widget)`, `NoticeMessage` / `ErrorMessage` (already imported in app.py), `self.push_screen_wait` (valid only inside a worker — hence `run_worker`).
- Produces: end-user behavior only; nothing downstream consumes this task.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py` (it already defines `_app(tmp_path)` and imports `NoticeMessage`):

```python
@pytest.mark.anyio
async def test_bang_submission_runs_command_not_a_turn(tmp_path: Path):
    """A `!` submission executes locally: the result lands in the pending
    shell-results queue and no agent turn starts (history stays empty)."""
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.on_prompt_input_submitted(
            PromptInput.Submitted("!echo bang-marker")
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        pending = app.harness.turn_controller._pending_shell_results
        assert len(pending) == 1
        assert pending[0][0] == "echo bang-marker"
        assert "bang-marker" in pending[0][1]
        assert list(app.harness.session.history) == []  # no turn ran


@pytest.mark.anyio
async def test_bare_bang_shows_usage_hint(tmp_path: Path):
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.on_prompt_input_submitted(PromptInput.Submitted("!"))
        await pilot.pause()
        assert app.harness.turn_controller._pending_shell_results == []


@pytest.mark.anyio
async def test_bang_refused_while_turn_busy(tmp_path: Path):
    """A `!` command mid-turn is refused with a notice — running it would
    interleave its output with the streaming response."""
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_starting = True  # the turn_busy property's spawn-gap term
        await app.on_prompt_input_submitted(PromptInput.Submitted("!echo hi"))
        await pilot.pause()
        assert app.harness.turn_controller._pending_shell_results == []
        notices = [str(w.render()) for w in app.query(NoticeMessage)]
        assert any("shell command" in n for n in notices)


@pytest.mark.anyio
async def test_bang_sudo_prompts_for_password_and_cancel_skips_run(
    tmp_path: Path,
):
    """A leading-sudo command opens the password modal; cancelling it (None)
    skips the run entirely."""
    from marim_harness.interfaces.tui.shell_passthrough import SudoPasswordModal
    from marim_harness.interfaces.tui.widgets.prompt import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        seen: list = []

        async def fake_wait(screen):
            seen.append(screen)
            return None  # user cancelled

        app.push_screen_wait = fake_wait  # type: ignore[method-assign]
        await app.on_prompt_input_submitted(
            PromptInput.Submitted("!sudo whoami")
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(seen) == 1
        assert isinstance(seen[0], SudoPasswordModal)
        assert app.harness.turn_controller._pending_shell_results == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_app.py -v -k bang`
Expected: FAIL — the `!` text is submitted as a normal turn (first test: `pending` is empty / history non-empty; sudo test: no modal seen).

- [ ] **Step 3: Implement**

In `src/marim_harness/interfaces/tui/app.py`:

(a) Add to the existing relative imports near the other `interfaces.tui` imports:

```python
from .shell_passthrough import (
    SudoPasswordModal,
    format_transcript_block,
    needs_sudo_password,
    parse_bang,
    run_passthrough,
)
```

(b) In `on_prompt_input_submitted` (~line 868), add the `!` branch right after the slash-command branch:

```python
        if text.startswith("/"):
            await dispatch(self, text)
            return
        if (command := parse_bang(text)) is not None:
            await self._handle_bang(command)
            return
```

(c) Add the two methods next to `on_prompt_input_submitted`:

```python
    async def _handle_bang(self, command: str) -> None:
        """Route a `!` submission: usage hint for a bare `!`, refusal mid-turn,
        otherwise run in a worker. A worker (not this handler) because sudo's
        modal needs push_screen_wait — invalid outside a worker, the same
        constraint the model picker documents — and because the command may
        legitimately run for up to PASSTHROUGH_TIMEOUT."""
        if not command:
            await self.post_system(
                "Usage: `! <command>` — run a shell command here; its output is "
                "shared with the model on your next message."
            )
            return
        if self.turn_busy:
            self._append_log(NoticeMessage(
                "Can't run a shell command while a turn is running. "
                "Press Esc first."
            ))
            return
        self.run_worker(self._run_shell_passthrough(command), exclusive=False)

    async def _run_shell_passthrough(self, command: str) -> None:
        """Execute a `!` command, render its output into the transcript, and
        queue it for the next turn's context. Leading-sudo commands collect a
        password first; it only ever transits the subprocess stdin pipe."""
        password: str | None = None
        if needs_sudo_password(command):
            password = await self.push_screen_wait(SudoPasswordModal(command))
            if password is None:
                self._append_log(NoticeMessage("sudo command cancelled"))
                return
        try:
            output = await run_passthrough(
                self.harness.deps.workspace.root, command, password
            )
        except OSError as exc:
            self._append_log(ErrorMessage(f"! {command} failed to start: {exc}"))
            return
        await self.post_system(format_transcript_block(command, output))
        self.harness.add_shell_result(command, output)
```

- [ ] **Step 4: Run the app tests**

Run: `uv run pytest --no-cov tests/test_app.py -v`
Expected: ALL PASS (the four new tests plus every pre-existing app test).

- [ ] **Step 5: Full verification (CI order) and commit**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
Expected: no lint errors, no type errors, full suite green with the coverage gate (≥90%) satisfied.

```bash
git add src/marim_harness/interfaces/tui/app.py tests/test_app.py
git commit -m "feat(tui): ! prompt passthrough runs shell commands locally"
```

---

## Manual smoke check (after all tasks)

Run `uv run marim` in a scratch project and verify by hand:
1. `! git status` → output appears in the transcript; next message: ask "what did my git status say?" — the model should answer from the injected block.
2. `!` alone → usage hint.
3. `! sleep 1 && echo done` → completes and renders.
4. `! sudo true` → password modal appears; Esc cancels with a notice; correct password runs (`exit 0`); wrong password shows sudo's failure output.
