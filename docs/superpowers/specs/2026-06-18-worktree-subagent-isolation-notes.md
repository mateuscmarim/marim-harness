# Git Worktree Support — Sub-project B: Subagent Worktree Isolation — Forward Notes

**Date:** 2026-06-18
**Status:** Not started — pre-spec design notes (no code, no approved spec yet)

These are forward-design notes for sub-project **B**, to be promoted into a full
design spec (via brainstorming) if/when B is actually built. They exist so the
decisions reached while shipping sub-project A are not re-derived from scratch.

See sub-project A (shipped): `2026-06-18-worktree-workflow-design.md`. B reuses
A's `workspace/worktree.py` git plumbing. A's Out of Scope section already names
B as a separate future spec.

## What B is

`spawn_agent` gains an `isolation` option so a subagent can run in its own git
worktree under `<repo>/.worktrees/` instead of the parent's shared workspace.

## Build gate (do not build on spec)

Build B **only after confirming marim actually spawns multiple subagents that
mutate files in parallel.** Worktrees solve exactly one problem — concurrent
writers corrupting the same checkout. If marim's subagents are read-only/research
or run one-at-a-time, the value is near zero; a branch suffices. Confirm the real
need first.

## The `isolation` param guidance (ready to drop into `spawn_agent`)

Embed the restraint **inline in the parameter description** — at the callsite
where the agent decides — not in the system prompt. This is the proven-reliable
placement (it mirrors how Claude Code's `Agent` tool carries its worktree
guidance). Verbatim text to ship with B:

> `isolation`: Run this subagent in its own git worktree under
> `<repo>/.worktrees/` instead of the shared workspace. Use ONLY when you are
> spawning multiple subagents that edit files in parallel and would otherwise
> corrupt each other's writes in the same checkout — it is the fix for
> concurrent-writer conflicts and nothing else. Do NOT use it for a
> read-only/research subagent, or for a single subagent with no sibling writers;
> those share the workspace (reach for a branch if you want isolation without
> concurrency). It is not free: each worktree costs a git checkout plus disk, and
> the subagent sees the repo's committed state, not the parent's uncommitted
> edits. When the subagent finishes, its worktree is removed automatically if it
> made no changes; if it produced commits, the worktree and its branch are kept
> for you to review and merge. Omit to run in the shared workspace (the default).

### Code obligations behind the prose

The last two sentences are **promises the implementation must keep**, not
decoration. B must actually implement:

- **Auto-remove if unchanged** — when the subagent finishes having made no
  changes, remove its worktree (mirror Claude Code's "auto-removed if unchanged").
- **Keep if commits** — if the subagent produced commits, leave the worktree and
  branch in place for the user to review/merge.

If B does not implement these, delete the corresponding sentences from the param
text — a description that lies is worse than one that says less.

### Known footgun to document for B

A worktree is cut from the repo's committed state (HEAD), so it does **not**
contain the parent's uncommitted edits. A subagent told "fix the function I just
changed" will read stale code unless the parent committed first. B's spec should
address whether/how to surface this (e.g. warn, or auto-commit/stash the parent
working tree before isolating).

### Per-worktree environment caveat for B

A fresh worktree of a Python project has no `.venv`. A subagent that lands there
and runs `uv run pytest` may fail or use the wrong interpreter. B needs an
explicit story for what tooling exists in the new worktree before isolated
subagents can run/verify code.

## Guidance placement principle (applies beyond B)

- **System prompt:** do not add worktree rules. marim already serves the
  `superpowers:using-git-worktrees` skill on demand via its per-turn skill index
  — lazy-loaded, no per-turn context cost. Don't duplicate it statically.
- **Tool description:** yes — the `isolation` param text above, inline at the
  callsite, only once B exists.
- **Sub-project A (`--worktree` / `/worktree`):** human-driven; the agent never
  chooses it, so it needs no agent-facing guidance.

## Enter/exit worktree (live session switching) — deferred, with rationale

A live `EnterWorktree`/`ExitWorktree` (moving a running session's workspace into
a worktree mid-conversation) stays **out of scope**, consistent with A's Out of
Scope entry ("In-process or sibling-process session switching"). Recorded here so
it is not re-litigated:

- **Why not:** `Deps.workspace_root` is immutable by design. Live switching is a
  re-bootstrap of LSP + MCP + bash + session state, with worse failure modes
  (partial-failure hybrid state) than a launch-time failure. The decisive blocker
  is session identity: sessions are keyed by `sha256(workspace)[:12]`
  (`session/store.py:22-25`), so switching workspace changes the session's
  identity mid-conversation — there is no clean answer to "which session am I
  now?"
- **The only thing it would buy:** carrying a live conversation into the
  worktree. The shipped `--worktree` flag starts a fresh session there (different
  hash), so it does not carry context.
- **Escape hatch (if the need ever materializes):** implement it as **persist +
  `os.execv` relaunch with session handoff**, never live `workspace_root`
  mutation. That gives a clean, correct re-bootstrap, stays in the same terminal
  (execv replaces the process, sidestepping the "second TUI fights over the
  terminal" objection), and only loses un-persisted in-memory state since session
  history is persisted.
- **Revisit trigger:** do not reopen on speculation. Reopen only after hitting
  "I'm mid-conversation and need this isolated" in real use enough times that the
  friction is concrete (≥3). Until then, `--worktree` + a fresh session covers it.
