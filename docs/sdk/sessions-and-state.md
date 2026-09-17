# Sessions & state

The SDK's contract: **a bare build touches nothing outside your process**
except the workspace files its tools legitimately edit. Session persistence
is opt-in; runtime diagnostics and oversized tool results can write under
the workspace's `.marim/` directory during a run. This page enumerates the
state and its storage boundaries.

## Sessions

The bare build's session is **in-memory only** — `harness.session.store is
None`, no transcript is written, and history survives only as long as the
`Harness` object. Turns still accumulate history within the process
(`run_turn` #2 sees turn #1).

`with_sessions()` opts into persistence:

```python
# CLI-default location: $XDG_DATA_HOME/marim-harness/sessions/<ws>-<hash>/
builder.with_sessions()

# ...or scoped under a directory you own — never touches XDG:
builder.with_sessions(dir=Path("./.myapp/sessions"))
```

One JSON file per session. An unusable sessions dir (permissions, not a
directory, …) is a `BuilderError` at `build()`, not a surprise at first
persist. Persisted histories are kept resumable across process kills — see
[Turns § Resumability](turns.md#resumability-persisted-sessions).

`harness.session.history` is model context: compaction may summarize or trim it.
`harness.session.transcript` is the recorded conversation for replay. Compaction
preserves that transcript, including recorded tool results; mobile and local/attached
TUI history read it instead of the reduced context. Both views are saved together
in the session JSON (`messages` and `transcript`) at the existing resumable boundary.
Rewind, clear, and session deletion still remove the corresponding conversation.

Older session files without `transcript` start with their available `messages`.
This prevents future compaction loss, but cannot reconstruct messages already
discarded by an older release. Retained transcripts increase session-file size;
external image caches and expired tool-output files keep their existing lifetimes.
CLI context gauges/compaction use the backend's current-context report, while
aggregate tool-running-turn usage remains unchanged in the spend ledger.

## Memory

Off by default. `with_memory()` enables the `remember`/`recall` tools:

- **No `dir`:** the CLI's default scopes — XDG global memory plus
  `<workspace>/.marim/memory` for project memory.
- **`dir=`:** both scopes rehomed under `dir/global` and `dir/project`;
  nothing touches XDG.

## Skills

Off by default. `with_skills()` enables `activate_skill`/`read_skill_file`:

- **No `dirs`:** CLI-style discovery (project, plugin, and global skill
  roots).
- **`dirs=[...]`:** only those directories are scanned; all discovery is
  skipped.

## The XDG boundary

A bare `build()` performs **no XDG reads at all**: the instruction closures
that would advertise a tool group (sub-agent roster, skill index, memory
index) only register when the matching `with_*` call loaded that group, and
the user-level `AGENTS.md` / installed-plugin instructions only register
when you opt in (`global_instructions=True` via `with_config_overrides`, or
`with_defaults()`, which turns it on along with every group).

`with_defaults()` is therefore the one builder call that performs XDG reads
(global `AGENTS.md`, skills, plugins, memory index) — everything else stays
workspace-scoped.

## What a bare build reads inside the workspace

Workspace-scoped is not the same as "anything in the workspace is trusted".
A bare build reads workspace files **only through the tools the model
calls** — it does not, on its own, read the workspace's `AGENTS.md` /
`CLAUDE.md` into the system prompt. That is opt-in via
`with_instructions(project=True)` (or `with_defaults()`), because for an
embedder whose workspace is an untrusted checkout (a review bot over a
contributor's branch) those files are attacker-controlled text that would
otherwise land straight in the model's instructions. Opt in when the
workspace is yours; leave it off when it isn't.

## Workspace-local runtime files

Provider-error payloads spill best-effort to
`<workspace>/.marim/last-provider-error.json` on hard failures (e.g. a 4xx
from your model provider), regardless of session config. That's
workspace-local, not XDG, and it exists so a headless failure leaves
something diagnosable behind.

Ordinary tool results at or above 10,000 characters also spill during a run.
They use `<scratchpad>/tool-results/` when a scratchpad is available, otherwise
`<workspace>/.marim/output/upstream/<session-key>/`. A sessionless harness uses
an ephemeral key stable for that harness instance. This writes lazily when a
large tool result is produced; bare `build()` remains write-free. See
[large tool output](../guides/tool-output.md) for retrieval, fallback behavior,
media handling, and file lifetime.

**If your workspace is a git repo, add `.marim/` to its `.gitignore`** —
otherwise diagnostics and large tool results can become untracked files.

## Stats ledger

Usage-per-turn deltas are collected into a dual JSONL ledger stored under
`{stats_base}/{global,<workspace-slug>}/turns.jsonl`. By default `stats_base`
is a **sibling** of the sessions dir, not a child of it: a sessions base of
`…/marim-harness/sessions` puts the ledger at `…/marim-harness/stats`
(any other sessions base gets a `stats/` subdirectory instead). Override it
with `with_sessions(stats_dir=…)`. Query via `marim_harness.stats.load_overview()` and `load_models()` to get
aggregated stats (per-model token counts, cost, usage streaks). The ledger
is never backfilled from old sessions; deltas come from `SessionController.add_usage`,
which is invoked automatically at each turn's end during normal operation.

To disable stats collection entirely, pass `stats=False` to `with_sessions()`
or set `MARIM_STATS=0` in the environment. Stats files stay inside the data
directory your sessions base lives in and can be wiped by deleting the
`stats/` directory — no prompts or tool content is ever stored.

## Quick reference: what can touch disk, and when

| State | Bare build | Opt-in | Scopable to your dir? |
| --- | --- | --- | --- |
| Session history | in-memory only | `with_sessions()` | `with_sessions(dir=...)` |
| Memory | off | `with_memory()` | `with_memory(dir=...)` |
| Skills (read-only scan) | off | `with_skills()` | `with_skills(dirs=[...])` |
| Global instructions / plugins (XDG read) | off | `with_defaults()` or `global_instructions=True` | no (XDG by definition) |
| Workspace `AGENTS.md` / `CLAUDE.md` into the system prompt | off | `with_defaults()` or `with_instructions(project=True)` | n/a — workspace-local by definition |
| Workspace file edits (`write_file`/`edit_file`) | on, gated | `with_files_write(False)` turns them off | confined to `workspace` root |
| Stats ledger | off | `with_sessions()` (default on; `stats=False` / `MARIM_STATS=0` offs) | sibling `stats/` of sessions base (`with_sessions(dir=...)`) |
| Workspace file edits | via gated tools | — | confined to `workspace` root |
| `.marim/last-provider-error.json` | on hard provider failure | always (best-effort) | no — workspace-local |
