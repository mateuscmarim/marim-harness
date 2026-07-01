# `!` Shell Passthrough — Design

**Date:** 2026-07-01
**Status:** Approved

## Summary

Let the user run shell commands directly from the TUI prompt by prefixing input with
`!` (Claude Code CLI style). The command executes locally and immediately — no agent
turn, no tokens. Output renders in the transcript, and the command + output are
injected into the model's context on the user's *next* real turn, so the agent can
reference what the user ran. Commands invoking `sudo` get a masked modal password
prompt, with the password fed via `sudo -S` stdin and never persisted or shown to
the model.

## Scope

- **TUI only.** Headless one-shot mode is untouched — it has no ongoing prompt loop.
- Output is **model-visible** (via the next turn's `<turn-context>`), matching
  Claude Code's behavior. No local-only variant in v1.
- Sudo support covers a **leading `sudo` token** only; `sudo` mid-pipeline
  (`foo | sudo tee`) fails with sudo's own "no tty" error — acceptable for v1.

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Where `!` works | TUI only |
| Model visibility | Command + output injected into next turn's context |
| Context mechanism | Pending queue drained into `<turn-context>` (Approach A) |
| Privileged commands | Modal password prompt + `sudo -S` |

Rejected alternatives: synthetic history messages (fights resumability invariants,
fabricates messages the model never produced) and routing through the agent loop as a
pre-approved bash call (burns a model turn per command).

## Design

### 1. Interception (TUI)

In `on_prompt_input_submitted` (`interfaces/tui/app.py`), before slash-command
dispatch: input whose first non-whitespace character is `!` routes to a new
passthrough handler instead of `run_turn`. Both `! git status` and `!git status`
work (strip the `!` and surrounding whitespace). A bare `!` shows a usage hint via
`post_system`.

If an agent turn is currently running, the command is refused with a notice
("finish or stop the current turn first") — running it would interleave its output
with a streaming response and race the busy-state plumbing.

### 2. Execution

Reuse `run_bash` from `tools/shell.py` — same new-session spawn, process-group kill,
line-bounded reads, and middle-truncation — with:

- **Timeout:** 120s for user-run commands (the model's tool default stays 30s).
- **Working directory:** the workspace root, same as the bash tool.
- **Command policy: bypassed.** `CommandPolicy` gates the *model*; the user typing a
  command into their own terminal-adjacent UI needs no gate.

The result renders in the transcript as a distinct block: the command echoed as
`! cmd`, then fenced output with the exit code. Rendering uses the existing
non-agent output path (`post_system`-style), which does not touch session history.

### 3. Model context: pending queue → `<turn-context>`

A pending list of `(command, output)` results lives on the turn controller. On the
next real user turn, `_assemble_prompt` (`runtime/controller.py`) drains it into the
injected turn-context prefix as a clearly labeled block alongside the existing
hook/jobs/error blocks, e.g.:

```
<user-shell-commands>
The user ran these commands in their own shell via the ! passthrough;
the outputs are shown verbatim.
$ git status
exit 0
...
</user-shell-commands>
```

Because the block lives in the injected prefix inside the `wrap_turn_context`
envelope, `strip_turn_context` resumability recovers the user's typed text
untouched, and nothing synthetic enters persisted history.

**Cap:** the pending list keeps the most recent results up to a total-character
budget (reusing the existing truncation helpers); older entries are dropped with a
marker line so the model knows output was elided. If the user never sends another
message, the pending results are simply never sent — that is correct behavior, not
a leak.

### 4. Sudo password support

- **Detection:** the command's leading token is `sudo`.
- **Prompt:** a masked Textual modal (styled like the existing approval/ask-user
  modals) collects the password. Cancelling the modal cancels the command.
- **Execution:** the command is rewritten to `sudo -S -p '' <rest>` and the password
  (+ newline) is fed via stdin. This requires extending `run_bash` with an optional
  `stdin_data: bytes | None` parameter (today stdin is not wired at all); when
  provided, stdin is a pipe, written once, then closed.
- **Password hygiene:** never rendered, never persisted, never included in the
  turn-context block (the echoed command is what the user typed; the password only
  transits the stdin pipe), and the local reference is dropped immediately after
  spawn. A wrong password surfaces sudo's error in the output; the user re-runs to
  retry.

### 5. Testing

- **Unit:** `!` prefix parsing (with/without space, bare `!`); pending-queue
  formatting, drain-on-next-turn, and cap behavior in `_assemble_prompt`; sudo
  detection and `-S` rewrite; `stdin_data` plumbing in `run_bash` (feed a command
  that reads stdin, assert it receives the bytes).
- **TUI (Pilot):** `!` input routes to the passthrough handler, renders output in
  the transcript, and does not start an agent turn; refusal notice while a turn is
  running.
- Existing bash-tool tests must stay green (the `stdin_data` parameter defaults to
  `None`, preserving current behavior).
