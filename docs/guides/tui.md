# The interactive TUI

`marim` (or `marim-harness`) with no subcommand opens the interactive TUI: a
scrolling transcript, a multi-line prompt box, and a status bar, with inline
panels for approvals and questions. This guide covers the whole interactive
surface — keys, slash commands, approvals, the sub-agents screen, settings,
and shell passthrough.

> Provider note: under the `claude-cli` main-loop provider, marim acts as a
> launcher — Claude Code runs its own tools and its own approval loop, so
> marim's tools, approval modes, LSP, and MCP do not apply to those turns.
> Caveats are flagged inline where they matter.

## The screen at a glance

Top to bottom:

- **Header** — a working/idle mark plus the session name (also mirrored into
  your real terminal tab title, so a backgrounded tab shows when a turn
  finishes).
- **Transcript** — the conversation log. It stays scrollable at all times,
  even while an approval or question panel is up. A fresh session starts
  top-aligned and only pins to the bottom once output overflows; a resumed
  session opens at the bottom, where you left off. If you scroll up, new
  messages do not yank you back down.
- **Jobs / Tasks / Queue panels** — thin strips that appear only when there
  is something to show: running background jobs, the agent's task checklist
  (plus the plan title once a plan is presented), and messages you queued
  while a turn was running.
- **Status bar** — one line of live session facts (see below).
- **Prompt input** — a multi-line box that grows with your text (up to six
  rows, then scrolls internally).

### Reading the status bar

Fields are separated by `·`, left to right:

- **mode** — the approval mode: `ask`, `auto`, or `plan`.
- **model** — the active model's label.
- **ctx N/M (P%)** — estimated context size versus the compaction threshold
  (the smaller of your context budget and 80% of the model's window). The
  field turns yellow at 75% and red at 90%; 100% means compaction is
  imminent.
- **token split** — `1k↑ 55k⚡ 2k↓`: uncached input, cached input
  (read + write), and output tokens for the session. While a turn streams, a
  live `+N` delta shows the in-flight run's tokens before they are folded in.
- **cost** — `$0.0042`-style session spend: the provider's billed figure when
  reported, otherwise a price-table estimate. Omitted when no price data
  exists for the model (`/usage` says so explicitly).
- **session Nm** — wall-clock time since the app started.
- **ttft N.Ns** — time-to-first-token of the latest model request: how snappy
  the provider feels right now. It lingers while idle (it describes the last
  request) and clears on a session reset.
- **working… Nm** — appears only while a turn runs, with the turn's elapsed
  time and an animated spinner in the header/tab title.

### Key bindings

| Key | Does |
|---|---|
| `enter` | Send the message |
| `shift+enter` / `ctrl+j` | Insert a newline |
| `up` / `down` | Recall prompt history (only at the first/last line of a draft, so arrows still move the cursor inside a multi-line message) |
| `esc` | Cancel the running turn (or dismiss the open panel/menu) |
| `ctrl+g` / `alt+enter` | Steer: inject the box's text into the running turn |
| `ctrl+t` | Cycle the approval mode: ask → auto → plan |
| `ctrl+r` | Run the next queued message (resumes a paused queue) |
| `ctrl+o` | Reveal all tool output in full (expand groups, uncap diffs); press again to restore the default view |
| `ctrl+x` | Toggle the sub-agents screen |
| `ctrl+p` | Open the command palette (change theme, show keys, quit) |
| `ctrl+v` | Attach a copied image from the clipboard (see Images) |
| `ctrl+c` | Quit — requires a second press within 2 seconds to confirm |
| `/` | Open the slash-command menu — `up`/`down` to move, `tab` to complete, `esc` to dismiss |

A first `ctrl+c` (or `/exit`) shows "Quit again to confirm", including how
many queued messages would be discarded; a second press within two seconds
quits. After the window elapses, the warning resurfaces.

## Sending and steering

Type and press `enter`. Every submission — slash commands included — is added
to prompt history, which persists across restarts and is recalled
shell-style with `up`/`down`.

**While a turn is running**, plain `enter` does not interrupt it: the message
is *queued* and shown in the queue panel, with per-item `edit` and `✕`
(remove) links. `edit` pops the message (text and image attachments) back
into the prompt box. When the turn finishes cleanly, queued messages drain
automatically, one turn each, in order.

**If a turn is cancelled or errors**, the queue *pauses* so nothing runs
against a broken state. Press `ctrl+r` to resume draining, or edit/remove
items first.

**Steering** (`ctrl+g` or `alt+enter`) is different from queueing: it injects
the box's text (and any attached images) *into the turn that is already
running*. The message reaches the model at its next request boundary — use it
to redirect the agent without waiting for the turn to end. The transcript
echoes `↪ steering: …`. A steer that lands in the gap just as the turn
finishes is not lost: it falls back to the front of the queue and runs next.
When no turn is running, the steer keys simply submit, exactly like `enter`.

**Cancelling**: `esc` cancels the running turn. The turn is flushed in a
resumable state, the queue pauses, and `turn cancelled` appears in the log.

Large pastes (over 3 lines or 600 characters) collapse into a
`[Pasted text #N +…]` marker to keep the box readable; the full text is
restored when you submit. Backspacing any part of a marker removes the whole
marker and its stashed content.

## Approvals

The approval mode governs marim's *gated* tools — `write_file`, `edit_file`,
and `bash` (and `run_workflow` when workflows are enabled). Cycle modes with
`ctrl+t`, or set one with `/mode`:

- **ask** — each gated call pauses the turn and asks you. Writes into the
  session scratchpad directory are pre-approved (that's what it is for).
- **auto** — every gated call is approved automatically.
- **plan** — mutations are denied; read-only `bash` commands are approved.
  The agent researches and presents a plan instead of editing.

Under the `claude-cli` main-loop provider none of this applies — Claude Code
runs its own tools and its own permission prompts.

### The approval panel

In ask mode, a bordered panel mounts *inline above the status bar* — not a
modal — so the transcript stays scrollable while you decide (`pageup`/
`pagedown` and `ctrl+up`/`ctrl+down` scroll it from the panel). The panel
shows what the call will do, not a raw args dump: an `edit_file` renders a
red/green diff, a `write_file` shows the new content, a `bash` call shows the
command, a `run_workflow` shows the script.

- `a` (or the Approve button) — approve the call
- `d` (or the Deny button) — deny it; the agent is told "denied by user"
- `esc` — same as deny (backing out of an approval is a deny; it will not
  cancel the whole turn)

A desktop notification fires when an approval is needed (if notifications
are enabled), so a backgrounded terminal still pings you.

### Questions and plan cards

Two more inline panels share the same above-the-status-bar spot:

- **Ask-user panel** — when the agent calls its `ask_user` tool, questions
  appear one at a time: a highlighted option list (single-select), a
  checkbox list with a Confirm button (multi-select), and always a free-text
  input so "type your own answer" is available on every question. `esc`
  cancels the whole prompt.
- **Plan card** — in plan mode, a finished plan appears as a card: summary,
  numbered steps, then execution choices (e.g. run it in auto, run in ask,
  keep planning). Picking a choice can flip the mode and start execution.
  Typing feedback into the input instead sends the plan back for revision;
  `esc` means "keep planning".

## Slash commands

Type `/` to open the command menu (fuzzy-filtered as you type; `tab`
completes the highlighted entry). Anything starting with `/` that is not a
known command reports an error instead of being sent to the model.

| Command | Aliases | Does |
|---|---|---|
| `/help` | `/?` | List all commands with one-line summaries. |
| `/clear` | — | Wipe the conversation and re-show the welcome screen. Refused mid-turn. |
| `/compact` | — | Compact the session now to free context: mask stale tool output, then summarize. Optional argument: `/compact [summary instructions]` steers the summarizer. Refused mid-turn. |
| `/sessions` | `/ls` | List saved sessions (messages, tokens, last-updated, active marker). |
| `/new` | — | Start a fresh session: `/new [name]`. Existing sessions stay on disk. Refused mid-turn. |
| `/switch` | — | Switch sessions: `/switch <number|name>` (number from `/sessions`, exact id, or name). Refused mid-turn. |
| `/rewind` | — | Bare `/rewind` lists checkpoints (one per turn, with the prompt that started it). `/rewind <number>` restores the conversation to that point and restores the working tree from the checkpoint's snapshot. `/rewind undo` reverses the last rewind, restoring the pre-rewind conversation and files. Refused mid-turn. |
| `/name` | — | Rename the session: `/name <title>`, or bare `/name` to auto-title it from the conversation. |
| `/mode` | — | Set the approval mode: `/mode [ask|auto|plan]`; bare `/mode` cycles. |
| `/model` | — | Switch the model: `/model <id>` applies it directly; bare `/model` opens the model picker. Refused mid-turn. |
| `/advisor` | — | Set the advisor model (a second model the agent can consult mid-task): `/advisor <id>`, `/advisor off`, or bare for a picker. Applies to the next consultation; persisted per session. |
| `/think` | `/effort` | Set the thinking (reasoning-effort) level: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`; bare opens a picker. Applies from the next turn; persisted per session. No effect under the `claude-cli` main provider. |
| `/theme` | — | List color themes or switch: `/theme [name]`. The choice persists as the startup theme. |
| `/remember` | — | `/remember <fact>` starts a turn that has the agent save the fact to persistent memory (it picks scope, type, and title). |
| `/skill` | — | Bare `/skill` lists discovered skills; `/skill <name> [context]` starts a turn that activates the skill and carries out its instructions. |
| `/mcp` | — | Bare `/mcp` lists MCP servers with connection state; `/mcp enable <name|all>` / `/mcp disable <name|all>` toggle them live. |
| `/usage` | `/cost` | Show the session's token split (uncached input, cache read, cache write, output, total) and cost — billed when the provider reports it, otherwise estimated. |
| `/worktree` | — | Manage git worktrees: `/worktree` (or `list`) shows branches/paths with a marker on the current one; `/worktree create <branch>` creates or reuses one (launch into it with `marim --worktree <branch>` in a new terminal); `/worktree remove <branch>` removes it. |
| `/jobs` | — | Background jobs: bare or `list` shows them; `/jobs output <id>` prints a job's output; `/jobs cancel <id>` cancels one; `/jobs wake [on|off]` shows or toggles autonomous wake — when on, a background job finishing while the app is idle fires a digest-only turn so the agent reacts without waiting for you. |
| `/plugin` | — | Bare or `list` shows installed plugins (scope, enabled/disabled, trusted/untrusted); `/plugin enable <name>` / `/plugin disable <name>` toggle one. Hook/MCP changes take effect on next launch; skills and sub-agents refresh next turn. |
| `/settings` | `/config` | Open the full-bleed settings screen. |
| `/exit` | `/quit` | Quit the harness (same confirm-to-quit guard as `ctrl+c`). |

Commands that rebuild or swap session state (`/clear`, `/new`, `/switch`,
`/model`, `/rewind`, `/compact`) refuse to run while a turn is in flight —
press `esc` first. They also wait for an in-progress compaction.

## Sessions, compaction, and usage

Sessions persist automatically (under `$XDG_DATA_HOME/marim-harness/sessions`)
and reopen where you left off. `/sessions`, `/new`, `/switch`, and `/name`
manage them. Untitled sessions are auto-titled in the background after some
conversation.

Compaction runs automatically when context approaches the threshold shown in
the status bar; `/compact` triggers it on demand, with optional instructions
for the summary. While it runs, a `compacting conversation…` notice shows,
and the result appears as `compacted history: N → M messages` plus the new
summary in a collapsed block. Submissions are refused (not silently queued)
during a manual compaction.

## Rewind and checkpoints

A checkpoint is captured at the start of every turn — conversation position
plus a git-based snapshot of the working tree (honoring `.gitignore`). Bare
`/rewind` lists them newest-last with each turn's prompt preview. `/rewind
<number>` restores the conversation and files to that point; the log notes
whether files were restored, and warns if the file restore failed partway
(in which case `/rewind undo` recovers the pre-rewind state). `/rewind undo`
also works after a successful rewind you regret.

## Models, advisor, and thinking

- `/model` switches the turn model — with a picker (searchable catalog,
  degrades to free-text on a slow provider) or directly by id. The choice is
  persisted with the session, and a session's saved model overrides your
  `.env` default the next time it loads.
- `/advisor` configures a second model the agent may consult mid-task for
  strategic guidance via its `advisor` tool. `off` disables it. Switchable
  mid-turn; each change applies to the next consultation.
- `/think` sets the reasoning-effort level for the main model (and is
  inherited by sub-agents unless their spec overrides it). Levels:
  `off minimal low medium high xhigh`. Unsupported models ignore it; under
  the `claude-cli` main provider it is a no-op.

If a model is known to be text-only, submitting an image is blocked with a
hint to switch models rather than failing mid-turn.

## The sub-agents screen (`ctrl+x`)

`ctrl+x` toggles a full-bleed screen over the transcript showing every
sub-agent spawned this session. (With no spawns yet, it just posts a notice.)
Layout:

- **Summary bar** — totals: `N sub-agents · waiting · running · done ·
  failed`, plus summed tokens and cost across all spawns.
- **Master list** (left) — one row per spawn with status glyph (`▸` running,
  `⧗` waiting on prerequisites, `✓` done, `✕` failed/denied, `⏸`
  interrupted), agent name, and live tools/tokens/cost/duration columns.
  Nested spawns render as an indented tree (`├─`/`└─`) under their parent.
- **Transcript pane** (right) — the selected spawn's live stream. Panes stay
  mounted whether or not the screen is open, so opening mid-run shows an
  already-current transcript; a resumed session's transcripts load lazily on
  first view.

Keys inside the screen: `esc` or `ctrl+x` to go back, `up`/`down` to select
a spawn, `tab` to switch focus between the list and the transcript pane, `t`
to expand/collapse the spawn's full task text, and `r` to resume the
selected *interrupted* spawn as a background job.

Sub-agent activity also renders as inline cards in the main transcript;
clicking through and the screen show the same data. Claude CLI-backed spawns
(and Claude's own Task sub-agents under the `claude-cli` provider) appear
here as first-class cards too.

## The settings screen (`/settings`)

A full-bleed screen with a left rail of sections: Session, Providers, Theme,
MCP servers, Context & Memory, Tools, Notifications, Advanced. Navigation is
rail-first: `up`/`down` switch sections, `enter` moves focus into the active
section's first field, `esc` steps back to the rail and then closes. Changes
save automatically — checkboxes and radios on change, text/number inputs on
`enter` or blur; the footer status line confirms each save.

Two kinds of settings live here:

**Apply immediately** (same mutations as the slash commands):

- Mode (this session), model, theme
- MCP server enable/disable
- Provider credentials (Providers section)
- Autonomous wake (session-only; mirrors `/jobs wake`)
- Dynamic workflows (persists `MARIM_WORKFLOWS` *and* flips the live seam
  when possible)
- Sub-agent model tiering master switch (persists and applies to new spawns
  live)

**Saved to the global `~/.config/marim/.env`, applied on next launch** (these
are consumed when the harness is constructed): LSP and LSP navigation tools,
job-tool mode, context budget, observation masking and its knobs, proactive
memory, tool search mode/threshold, sub-agent request limit, autonomous-wake
turn cap, default mode for new sessions, notification settings, per-tier
sub-agent models, and the global advisor/thinking defaults (the live
per-session switches remain `/advisor` and `/think`).

The Advanced section is read-only: command deny/allowlist, project-hooks
trust, and the config file path.

## Shell passthrough (`!command`)

Prefix a message with `!` to run a shell command yourself, in the workspace
root, without involving the model: `! git status` (or `!git status`). A bare
`!` shows usage. Output renders in the transcript as an `exit N` code block,
and — importantly — is queued as context for the model's *next* turn, so you
can run a command and then ask about its output.

Details:

- Commands get a 120-second timeout (more generous than the model's own
  tool default).
- A leading `sudo` opens a password modal (the TUI's subprocesses have no
  terminal for sudo to prompt on); the password only ever transits the
  subprocess's stdin pipe and is never echoed, logged, or persisted. `esc`
  or an empty submit cancels.
- Refused while a turn is running (press `esc` first). A turn *starting*
  mid-command is fine — the output still lands and queues normally.

## Images

Two ways to attach an image to a message:

- **`ctrl+v`** — attaches the image currently on the system clipboard. This
  is the app's own clipboard read; your terminal's native paste
  (middle-click, terminal menu) delivers text only and cannot carry image
  bytes.
- **Paste a file path** — pasting text that is a path to an image file
  attaches that file.

Either way an `[Image #N]` marker appears in the text; markers are atomic
(backspace removes the marker and its attachment together, and survivors
renumber). Attachments ride along when a message is queued, steered, or
edited back into the box. If the active model is known not to support
images, the submission is blocked with a hint instead of failing.

## Background jobs and autonomous wake

Long-running work the agent starts in the background (detached commands,
background sub-agent runs) shows in the jobs panel and is managed with
`/jobs`. Jobs are process-scoped: they survive session switches but are
killed when the app exits. Each completion fires a desktop notification
(when enabled).

With autonomous wake on (`/jobs wake on`, or the Settings checkbox), a job
finishing while no turn is running wakes the agent with a digest-only turn —
`⏰ Resumed — background job(s) finished` — so it can react to the results
without waiting for your next message. The wake chain is bounded by a
configurable turn cap so finished jobs cannot ping-pong the agent forever.
