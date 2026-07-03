# Claude-CLI sub-agent resume — session-id capture, mid-run checkpoints, `--resume` relaunch

**Date:** 2026-07-03
**Status:** Approved design, pending implementation plan
**Builds on:** `2026-07-03-subagent-resume-design.md` (native spawn resume: v2 sidecar
envelope, interrupted scan, `resume_spawn`, `r`-key screen action),
`2026-07-02-cli-subagent-demux-design.md` (CLI backend + demux)

## Problem

Native spawns are now resumable, but `backend: claude-cli` spawns are excluded twice
over: their sidecar is written only at completion (a CLI spawn killed mid-run leaves no
trail, so it never shows as interrupted), and `resume_spawn` continues transcripts
through the native pydantic-ai loop — the wrong engine for an external `claude` process.
The prior spec scoped this out as "the CLI owns its own resume story."

## Insight

That's exactly why it's easy: the Claude CLI persists its own session locally and
supports `claude -p --resume <session_id>`. `build_cli_argv` already accepts
`resume_session_id` (the main-loop `ClaudeCliModel` provider uses it), the stream
translator already accumulates the transcript incrementally, and the init event we
already parse (for the model name) carries the session id. Marim shouldn't replay
transcripts at the CLI — it should record the session id and relaunch.

## Goals

- A CLI spawn killed mid-run leaves a checkpointed v2 sidecar (parent transcript +
  meta), so it surfaces as a ⏸ interrupted card through the existing scan, unchanged.
- Pressing `r` on it resumes the underlying Claude session via `--resume`, as a
  background job, streaming into the same card through the existing demux.

## Non-goals

- Mid-run checkpointing of demuxed *children* (Claude's inner Agent/Task spawns).
  Their partial work is visible inside the parent's checkpointed transcript, their
  sidecars stay completion-time, and resuming the parent session restores them.
  (Decided: parent-only checkpoints.)
- Independently resuming a demuxed child — Claude Code doesn't expose resuming an
  inner Task; the parent session is the unit of resume.
- Resuming across machines. CLI sessions are local state.
- Pre-checking that the CLI session file still exists. The on-disk path scheme is
  CLI-internal; a stale session surfaces as the CLI's own error on the card.

## Design decisions

1. **CLI-native resume, not transcript replay.** Rejected alternatives: replaying the
   demuxed transcript through the native loop (swaps engines mid-task; the demuxed
   transcript is a *rendering* of the session, not the session) and restart-with-summary
   (lossy, re-spends completed work).
2. **The session id is the resume key**, stored in the sidecar meta as
   `cli_session_id` alongside `backend: "claude-cli"`. A spawn killed before the init
   event has no session id and is refused with a clear reason.
3. **`append_system=False` on resume** — the session already carries its system prompt
   from creation; re-appending would duplicate it. This mirrors the main-loop
   `ClaudeCliModel`'s resumed-turn behavior exactly.
4. **Everything downstream reuses the native resume surface**: the interrupted scan,
   the ⏸ card, the `r` action, the `_resuming` double-press guard, background-job
   registration with `stream_id`, `adopt_resumed_card`, the finished-job digest.

## 1. Session-id capture + checkpointing (`subagents/cli_backend.py`)

`ClaudeCliRunner.run` gains two things:

- **Session-id capture.** The first top-level `session_id` seen on a stream-json object
  (the system/init event carries it; we already read `model` off the same event) is
  remembered for checkpoints and returned on the result. `CliResult` gains
  `session_id: str | None`.
- **A `checkpoint` callback** (optional; mirrors the native `build(checkpoint=)`
  pattern): `checkpoint(transcript: list, session_id: str | None)`, invoked after each
  translated stream object. The translator's `transcript()` already accumulates the
  run's messages, so a checkpoint is a read + write, no new bookkeeping. Checkpoint
  failures follow the established discipline: log, never kill the spawn (the callback
  the runner supplies is already best-effort via `_save_transcript`). CLI checkpoints
  apply the same `cap_reasoning` clipping as native ones so the per-request payload
  stays bounded — safe here because the translator's `ThinkingPart`s carry no
  provider signature.
- `run(...)` also gains `resume_session_id: str | None = None`, threaded into
  `build_cli_argv(resume_session_id=..., append_system=not resume_session_id)`.

## 2. Meta extension (`subagents/runner.py::_execute_cli_spawn` / `_run_cli`)

The CLI spawn's meta template gains `"backend": "claude-cli"` and
`"cli_session_id": None` (filled by the first checkpoint once the init event arrives;
the final completion write carries it too, so *finished* CLI spawns also record their
session id — useful for debugging, harmless otherwise). `_execute_cli_spawn` builds the
checkpoint closure (meta template + `_save_transcript`, status `running`, updating
`cli_session_id` from the callback argument) and threads it through `_run_cli` into
`ClaudeCliRunner.run`.

Native spawns' meta is untouched (`backend` absent ⇒ native — old sidecars keep
resuming through the native branch with no migration).

With checkpoints in place, a killed CLI spawn rests at `status: "running"` and the
existing interrupted scan, card synthesis, and refusal machinery apply verbatim —
section 3 of the native-resume spec needs no changes.

## 3. Resume branch (`subagents/runner.py::resume_spawn`)

After the shared guards (store present, meta present, status resumable, `_resuming`
double-press guard, no live job on the stream_id), branch on
`meta.get("backend") == "claude-cli"`:

1. **Skip transcript read/repair** — the CLI owns its history; marim's sidecar is a
   display copy.
2. Refuse (renderable reasons, existing seam) when: `cli_session_id` is missing
   ("killed before the CLI session started — nothing to resume; spawn it again");
   the agent type no longer resolves via `find_agent`; the resolved definition's
   backend is no longer `claude-cli` (the definition changed under us); the isolation
   branch named in meta no longer exists (shared guard).
3. Relaunch as a background job (same registration, label, `stream_id=` as native
   resume): the CLI tail runs with the *continuation prompt* as `-p`,
   `resume_session_id=meta["cli_session_id"]`, `append_system=False`, cwd = the
   reopened worktree for an isolated spawn (shared `create_or_reuse_worktree` path) or
   the workspace root. Hooks bracket the run as any CLI spawn; `subagent_stop`
   receives the original task from meta (native-resume parity).
4. **No pre-flight session check.** If the CLI can't resume the session (deleted,
   pruned, wrong machine), the process fails, the job settles failed, and the CLI's
   error renders on the card — the same containment every CLI spawn already has.

The resumed run reads the previously persisted transcript before relaunching and
prepends it to every checkpoint *and* the final write — the CLI's `--resume` stream
carries only the continuation, not the prior history, so without this prefix the
resume would overwrite the sidecar with tail-only content and destroy the pre-interrupt
segment (including the demuxed-children entries) the pane replays. The resumed run
checkpoints too (same closure), so a resume interrupted *again* remains resumable; the
final write stamps `finished` with the CLI's usage
(`synth_usage`/`sum_result_usages`, unchanged).

## 4. What the user sees

Identical to native resume: the killed spawn's card shows ⏸ interrupted with the
`r resume` hint; pressing `r` flips it live via `adopt_resumed_card`; new events stream
through the existing demux into the same card (new inner Agent/Task children get cards
as usual); prior children replay from the parent's checkpointed transcript when the
pane opens; on completion the report arrives via the finished-job digest and the jobs
panel shows the settled job.

## Error handling

- Every refusal is a user-renderable string through the existing `(job_id, message)`
  seam.
- Checkpoint write failures: warn and continue (never kill the spawn).
- A malformed/absent init event ⇒ no session id ⇒ the spawn still runs and completes
  normally; only resumability is lost, and the refusal says so.
- CLI resume failure (stale session, missing binary): the job fails with the CLI's
  error text on the card; the sidecar stays `running`, so the user may retry after
  fixing the cause.

## Testing

Fake-CLI script pattern (as in `tests/test_subagent_transcript_capture.py`):

- Init event with `session_id` → mid-run checkpoints write a v2 envelope with
  `backend`, `cli_session_id`, `status: "running"`; final write stamps `finished`.
- Fake CLI that emits init + an assistant message then exits without a result
  (simulated kill) → `CliRunError`, sidecar rests at `running` with the session id.
- `resume_spawn` on that sidecar → the fake CLI is re-invoked with
  `--resume <session_id>`, without `--append-system-prompt`, with the continuation
  prompt as `-p`; job registered with the stream_id; final meta `finished`.
- Refusal matrix: missing `cli_session_id`; agent type gone; backend changed;
  isolation branch gone; double-press (shared guard).
- `build_cli_argv` unit: `append_system=False` + `resume_session_id` argv shape
  (extends the existing argv tests).

Live verification against the real `claude` binary runs on the user's Claude
subscription — opt-in only, never part of the default gate.
