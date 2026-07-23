# Sessions, compaction, and rewind

Every conversation you have with marim is a **session**: a named, persisted
transcript you can resume, switch between, rename, and rewind. This guide
covers where sessions live, how resuming and model inheritance work, how the
context is kept under budget (compaction), and how checkpoints let you roll a
session — conversation *and* files — back to an earlier turn.

## Where sessions live

Sessions are stored under the XDG data directory:

```
$XDG_DATA_HOME/marim-harness/sessions/          # default: ~/.local/share/...
  <workspace-name>-<hash>/                      # one directory per workspace
    <session-id>.json                           # one file per session
    <session-id>.checkpoints.json               # checkpoint sidecar (see below)
```

The per-workspace directory is keyed by the workspace path (its basename plus a
short hash of the absolute path), so two checkouts with the same folder name
never share sessions, and sessions never leak across projects.

A session file persists:

- the full message history (pasted images are externalized to the image cache
  and rehydrated on load),
- cumulative token usage (input/output/cache/requests/tool calls),
- the session's **model**, **advisor model**, and **thinking level**,
- the display name and whether it is still an auto-generated placeholder,
- the agent's task checklist and finished background-job digests,
- total active time and a last-updated timestamp.

A session also owns sidecars keyed by its id: the checkpoint list, sub-agent
transcripts, cached images, the session scratchpad, and any shadow git refs its
checkpoints pinned. `marim sessions delete <id>` removes all of them.

## Starting, resuming, and switching

- `marim` starts a **new** session (unnamed, timestamp-titled until
  auto-titling kicks in).
- `marim --resume` reattaches to the **most recently updated** session for the
  current workspace and replays its history.
- `marim sessions list [--json]` and `marim sessions delete <id>` manage
  sessions from the shell.

Inside the TUI:

- `/sessions` lists saved sessions for this workspace, newest first.
- `/new [name]` starts a fresh session (named, or unnamed and auto-titled).
- `/switch <number|name>` switches to another session. A corrupt target fails
  loudly and leaves you on the current session — nothing is overwritten.
- `/name [title]` renames the session; with no title it generates one.

### Auto-titling

An unnamed session is titled automatically: after a turn completes, a small
tool-free "titler" agent (running on the session's model — or, under the
`claude-cli` provider, on an ephemeral clone that never touches your live
Claude session) reads the transcript and produces a short title of at most six
words. It runs in the background so it never delays your turn; headless runs
wait for it before exiting. An explicit `/name` always wins — once you set a
name yourself, auto-titling stops for that session.

## A new session inherits the last session's model

This surprises people, so read it twice: **when you start marim fresh (no
`--resume`), the new session inherits the model — and the advisor model and
thinking level — from your most recent session in this workspace.** That
inherited choice **overrides `MARIM_MODEL` and your `.env`**.

Concretely: if you set `MARIM_MODEL=some/new-model` in `.env` but your last
session used a different model (say, via `/model` or the picker), the next
`marim` launch keeps using the *old* model. `MARIM_MODEL` only decides the
model when there is no prior session to inherit from, or when the latest
session never recorded one.

To actually switch models:

- use `/model` (or the Settings screen) — the choice persists to the session
  and becomes what future sessions inherit, or
- delete/ignore the old sessions so nothing is inherited.

Inheritance also carries an explicit "off": if you disabled the advisor or set
thinking to `off`, the next session starts with them off too — a deliberate
disable is remembered, not just positive picks.

## Compaction: keeping the context under budget

Long sessions outgrow the model's context window. marim compacts proactively
so a turn never dies on a hard overflow.

### When auto-compaction triggers

The trigger threshold is:

```
threshold = min(budget, 0.8 × context window)
```

The 0.8 safety ratio applies only when the window is actually *known*
(discovered from the provider catalog / local probe, or stated via
`MARIM_CONTEXT_WINDOW`); otherwise the budget alone gates (default 100,000
tokens — `MARIM_CONTEXT_BUDGET`, with per-model overrides via
`MARIM_CONTEXT_BUDGETS`). The context size compared against it is the larger
of a chars/4 estimate and the provider's real input-token count from the last
request — dense code tokenizes worse than the estimate assumes, so the
measured number is trusted when available.

The check runs at the start of every turn and again right after a turn
completes; a genuine provider overflow mid-turn forces a compaction and
retries the request once.

### What compaction does

Compaction is a two-stage pipeline:

1. **Observation masking (micro-compact).** Older tool outputs are elided
   first: the most recent 4 tool returns are kept intact, and older returns of
   at least 200 rendered characters have their payload replaced with a short
   placeholder. When the session scratchpad is enabled, the full payload is
   saved there first and the placeholder points at the file, so the model can
   `read_file` the exact bytes back instead of re-running the tool. When old
   tool output *is* the bloat, this gets under threshold without any model
   call. Knobs: `MARIM_MASK_OBSERVATIONS`, `MARIM_MASK_KEEP_RECENT`,
   `MARIM_MASK_MIN_CHARS` — see
   [`reference/configuration.md`](../reference/configuration.md).

2. **Summarization.** If still over threshold, the history is split into the
   first message (the original task anchor), a middle, and a recent tail of
   roughly the last 20 messages — always cut at a user-turn boundary so tool
   calls stay paired with their returns. The middle is condensed into one
   structured summary message (requests and intent, files touched, errors and
   fixes, pending work, next step) by a dedicated tool-free summarizer agent
   on the session's model. If the summarizer fails, the middle is dropped
   outright rather than breaking the turn.

A rapid-refill breaker guards against thrashing: if the context refills right
after each of three consecutive compactions (typically one oversized tool
output), auto-compaction pauses with a notice suggesting smaller reads or
`/clear`. Manual and overflow-forced compactions bypass the breaker.

### Manual `/compact`

`/compact [instructions]` compacts immediately, bypassing the size gate. The
optional instructions are handed to the summarizer verbatim, so you can steer
what the summary keeps: `/compact keep the API design decisions and open
bugs`. Refused while a turn is running.

### The PreCompact hook

A configured `PreCompact` hook fires before every compaction, while the
transcript is still full. It can **block only a manual `/compact`** (exit code
2 or a `{"decision": "block"}` verdict); on automatic or overflow-forced
compaction a block verdict is logged and ignored — a hook must never be able
to wedge a session into the hard context limit.

### Compaction and your checkpoints

A summarizing compaction restructures the history, which makes existing
checkpoints' conversation offsets meaningless — they are dropped rather than
left as corrupting rewind targets. A masking-only compaction moves no message
boundaries, so your checkpoints survive it.

## Checkpoints and rewind

At the start of every turn, marim captures a **checkpoint**: the conversation
length at that moment, a timestamp, a preview of the prompt (first ~80
characters), and — when the workspace is a git repository — a shadow snapshot
of the working tree. The most recent 50 checkpoints are kept per session, and
the list persists in the session's checkpoint sidecar across restarts.

### Shadow snapshots

Snapshots are ordinary git commits created through a throwaway index and
pinned under private refs (`refs/marim/checkpoints/<session-id>/<n>`). They
capture the whole working tree — tracked *and* untracked files — while
honoring `.gitignore`: ignored files (build output, `.env`, local databases)
are not captured and are never touched on restore. The one exception is a file
that is tracked despite being ignored (force-added): it is deliberately
included so a rewind cannot delete it. Chat-only turns on a clean tree reuse
the previous snapshot instead of re-staging everything, so checkpointing stays
cheap.

Your branch, staged index, and `HEAD` are **never modified** — snapshots and
restores work entirely through temporary index files and the private ref
namespace, and the only user-visible mutation is working-tree files on an
explicit restore.

### Rewinding

```
/rewind          # list this session's checkpoints
/rewind 3        # restore conversation and files to before checkpoint #3
/rewind undo     # reverse the last rewind
```

`/rewind <n>` does two things, in order:

1. **Files** (git workspaces, when the checkpoint has a snapshot): the working
   tree is restored to the snapshot — captured files get their old content
   back, and files created after the checkpoint are deleted (ignored files
   survive; nested repos and spawn worktrees are never deleted). Before
   touching anything, the *current* tree is itself snapshotted, so the rewind
   is undoable; if that safety snapshot cannot be captured, the file restore
   is refused rather than run without a recovery path. A failed or partial
   restore is always reported as failed, never dressed up as clean.
2. **Conversation**: the history is truncated to the checkpoint's length and
   persisted. Checkpoints later than the target are set aside (kept alive for
   undo), so the list matches the rewound state.

Outside a git repository (or for a checkpoint with no snapshot), rewind
restores the **conversation only**. Rewind is refused while a turn is running.

### Undo — a single slot

`/rewind undo` reverses the most recent rewind: the conversation, the working
tree (from the pre-rewind safety snapshot, stored at
`refs/marim/checkpoints/<session-id>/_pre_restore`), and the checkpoints the
rewind dropped all come back. Undo itself takes a safety snapshot first, so
work done *after* the rewind is not silently destroyed either.

It is a **single slot** with a bounded window: only the last rewind can be
undone, a second `/rewind undo` is a no-op, and the window closes as soon as
you move forward — a new turn, another rewind, or `/clear` — or the process
restarts or you switch sessions. When the window closes, the stashed snapshots
are released.

## Interrupted turns stay resumable

You can Ctrl-C a turn, lose your network, or have the provider fail mid-run
without losing the session. Chat APIs reject any history that ends with a tool
call missing its result, so an aborted turn would normally leave the session
unresumable. marim prevents this twice over:

- when a turn is aborted, it flushes a *repaired* history to disk,
  synthesizing an "interrupted, did not run" result for any unanswered tool
  call — the partial work is preserved and the next request is valid;
- at the start of every turn, it re-checks the loaded history and repairs any
  dangling tool call it finds (covering histories written by a crash or an
  older version).

One deliberate gap: while a tool approval is pending, the in-memory history
ends with unanswered tool calls, and that dirty state is *never* persisted.
Cancelling an approval rolls back to the last cleanly persisted point, so a
resumed session always starts from a coherent conversation.
