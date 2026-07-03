# Design: job poll guard — stop models from busy-polling background jobs

**Date:** 2026-07-02
**Status:** Approved in discussion (user chose the layered design, deterministic
guard as the core)
**Scope:** `src/marim_harness/jobs.py` + tool docstrings/return text in
`src/marim_harness/tools/provider.py`, plus tests. No TUI changes, no new
callbacks, no wire-format changes.

## Problem

With detached fan-out and `after=` chains, models drift into a poll loop:
repeated `job("list")` / `jobs()` calls ("Still waiting. Let me check again."),
burning turns and tokens. Ending the turn is already safe in the TUI — the wake
loop re-invokes the agent when a job settles, and finished-job digests are
injected into the next turn — but the only nudge saying so is the spawn
handoff, many events earlier. The listing itself silently rewards another look.

Secondary observed stumble: a model composing an `after=` spawn in the same
parallel burst as its prerequisites guesses job ids (they don't exist yet),
takes the tool's fail-fast rejection, and recovers by listing — one wasted
round-trip that a docstring sentence can prevent.

## Design (three layers; layer 1 is the fix, 2–3 lower first-occurrence odds)

### Layer 1 — deterministic no-progress guard (JobRegistry)

`JobRegistry` gains a poll ledger:

- `note_poll(key: str, snapshot: str) -> int` — records that a read-only poll
  surface (`key` = `"list"` or `"output:<job_id>"`) produced `snapshot`, and
  returns the number of *consecutive identical* observations for that key
  (1 = first sight / changed since last time). Any state change — `register`,
  a settle, `cancel`, `clear_history` — clears the whole ledger, because the
  next poll genuinely has something new to see.
- Deliberately **no turn-boundary reset**: the ledger keys off job state, not
  turns. A first re-list in a fresh turn with nothing changed gets the gentle
  count-2 warning *with* the table — acceptable, and it avoids threading turn
  lifecycle into the registry.

Snapshot keys are **stable projections**, never elapsed-time renderings:

- list-polls: the `render_jobs` table (verified stable — glyph, id, kind/type,
  one-line title, `(status)` suffix; no durations).
- output-polls: the `job_output` text itself. A growing bash buffer changes
  every call → count stays 1 → never nagged. A static "(still running)" /
  "(waiting on …)" marker repeats → nagged. This is the progress boundary for
  free: only *zero-information* polls trigger the guard.

Tool behavior on the guard (in `tools/provider.py`, applied to `jobs()`,
`job("list")`, `job_output` / `job("output")` — **not** `wait_for_job`, which
is time-bounded blocking, not busy-polling):

- count 1: normal response, no guard text.
- count 2: normal response **plus** a warning line (see copy below).
- count ≥ 3: the table/output is **replaced** by the stop-polling instruction.
  Withholding the data matters: fresh-looking tables make warnings read as
  boilerplate.

The guard only fires while at least one listed/queried job is still `running`;
a settled-jobs listing is a result read, not a poll.

### Copy (exact; interactive vs headless)

The advice depends on whether a wake loop exists. `deps.ui.interactive` is the
existing signal (set by `bind_ui`; headless never calls it — and headless has
no wake loop, so "end your turn" would be *wrong* there).

- Interactive, count 2 — appended after the table/output:
  `⚠ No change since your last check. If you have no other work, end your turn`
  ` — finished jobs wake you and deliver their reports automatically.`
- Interactive, count ≥ 3 — the entire response:
  `No change since your last check (poll N). Stop polling: end your turn now —`
  ` finished jobs wake you and deliver their reports automatically. Use`
  ` wait_for_job(id) only if you must block on a result inside this turn.`
- Headless (no wake loop), count ≥ 2 — appended (never replaces; headless has
  no wake, so the data may still be needed):
  `⚠ No change since your last check. Use wait_for_job(id) to block until a`
  ` job finishes instead of polling.`

### Layer 2 — docstring guidance (tools/provider.py)

- `jobs()` and `job()` docstrings gain: "Never call this in a loop to wait —
  if you have no other work, end your turn; the harness wakes you when a job
  finishes and delivers its report."
- `spawn_agent`'s `after` parameter docs gain: "Prerequisite ids come from the
  spawn handoffs ("Started job-N …"); issue a dependent spawn in a later
  response, after those return — ids cannot be guessed."

### Layer 3 — standing wake note in the listing

The `jobs()`/`job("list")` tools append the note (`render_jobs` itself stays a
pure list→table helper): when any listed job is `running` **and** the session
is interactive, the response ends with:
`(running jobs wake you on completion — no need to check again)`

## Edge cases

- **Output-poll on a finished job** → job not `running` → guard never fires;
  reading a result repeatedly is harmless.
- **Two different poll surfaces interleaved** (list, output, list…) → keys are
  independent; each converges on its own count. The ledger is a small dict,
  cleared on every state change, so it cannot grow past the number of surfaces
  polled between changes.
- **Model alternates list with real work** → counts still rise (the *poll* is
  unchanged), warning appears — correct: the listing itself is still telling
  it nothing. Only the count ≥ 3 replacement could hide data the model wasn't
  using anyway; it can always `wait_for_job` or act.
- **`/clear` / session switch** → `clear_history` resets the ledger with the
  jobs it prunes.
- **Concurrency** — the registry is single-loop asyncio; `note_poll` is a
  plain dict update, no locking needed (same as the rest of JobRegistry).

## Testing

- Pure `JobRegistry.note_poll`: same key+snapshot counts up; changed snapshot
  resets to 1; `register`/settle/`cancel`/`clear_history` clear the ledger;
  independent keys don't interfere.
- Tool-level (existing jobs-tool test style, `_make_deps`/`_make_harness`):
  interactive count-2 append, count-3 replacement, headless append-only
  variant, no guard when nothing runs, growing bash output never nagged.
- Docstring assertions are not tested (prose), but the `render_jobs` standing
  line is: present with a running job + interactive, absent otherwise.

## Non-goals

- No enforcement on `wait_for_job` (bounded blocking is legitimate).
- No turn-boundary plumbing into JobRegistry.
- No TUI jobs-panel changes (the panel polls by design; the guard is a tool
  concern keyed to model-facing reads — the panel reads `jobs.list()`
  directly, never `note_poll`).
- No hard turn termination; the harness nudges deterministically but never
  force-ends a turn.
