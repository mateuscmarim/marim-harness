# Sub-agent spawn resume — checkpointed sidecars, job history, interrupted-spawn resume

**Date:** 2026-07-03
**Status:** Approved design, pending implementation plan
**Builds on:** `2026-06-25-resumable-subagent-transcripts-design.md` (sidecar store, lazy
replay), `2026-07-02-cli-subagent-demux-design.md`, `2026-07-02-job-poll-guard-design.md`

## Problem

Sub-agent transcript persistence and card replay already exist for *finished foreground*
spawns: `TranscriptStore` writes a sidecar per spawn at completion, and `replay_history`
rebuilds cards from `spawn_agent` tool calls with lazy transcript load. Three gaps remain:

1. **Background spawns replay as plain tool rows.** A background `spawn_agent` returns a
   job id, so on session resume it never repopulates the sub-agents screen.
2. **Jobs are process-scoped.** `JobRegistry` is explicitly in-memory; the jobs panel is
   empty after restart, and the finished-job history item (§6 of the 2026-06-25 spec) was
   designed but never implemented.
3. **A spawn interrupted mid-run leaves nothing behind.** `_save_transcript` runs only at
   completion, so a process death mid-spawn loses the transcript entirely, and there is
   no way to continue interrupted work.

## Goals

- Every spawn — foreground, background, CLI-demuxed — repopulates the sub-agents screen
  as a real card on session resume, with its final status and report.
- Settled job summaries survive restart (implements §6 of the 2026-06-25 spec).
- A spawn interrupted mid-run appears on the screen as **interrupted** with its partial
  transcript, and can be **manually resumed** from the sub-agents screen.

## Non-goals

- Auto-resume on session open (no surprise spend).
- Resuming *live* across processes (a running spawn handed between two processes).
- Resurrecting a lost isolation worktree — an isolated spawn whose branch is gone is
  reported as non-resumable, never silently continued un-isolated.

## Design decisions

1. **Checkpoint cadence = per model response.** The runner flushes the sidecar after each
   model response during the run, not only at completion. Natural, bounded, and each
   checkpoint ends at a message boundary.
2. **Resumed spawns always continue as background jobs**, even if originally foreground.
   A foreground spawn's owning turn is gone after a restart (the main history's dangling
   `spawn_agent` call gets repaired with a synthetic return), so the digest path is the
   only report consumer that still exists — and it already works.
3. **Sidecar format v2 is an envelope, read-compatible with v1.** A top-level JSON list
   is a v1 file (messages only, no meta); a top-level object is v2.
4. **Meta lives in the sidecar, not a new registry.** The card/resume state is derived
   from things that already exist (history + jobs history + sidecar meta); no second
   authoritative record of spawns to drift out of sync.

## 1. Checkpointed sidecar envelope

### Format (`session/transcripts.py`)

```json
{
  "v": 2,
  "meta": {
    "name": "explorer",
    "task": "<capped task text>",
    "parent_id": "toolu_...",          // null for a top-level spawn
    "depth": 1,
    "granted": ["read_file", "grep"],  // tool names granted at spawn time
    "isolation": "marim/agent-3",      // worktree branch, or null
    "status": "running",               // running | finished | failed | interrupted
    "usage": {"input_tokens": 0, "output_tokens": 0, ...},
    "updated": "2026-07-03T12:00:00Z"
  },
  "messages": [ ... ]                  // ModelMessagesTypeAdapter payload, capped
}
```

`TranscriptStore` changes:

- `write(stream_id, messages, cap, meta=None)` — when `meta` is given, wrap in the v2
  envelope; without it, keep writing v1 (callers migrate incrementally). Atomic write,
  best-effort, as today.
- `read(stream_id)` — detect list (v1) vs object (v2); return messages either way.
  Existing sidecars keep loading unchanged.
- `read_meta(stream_id) -> dict | None` — meta only, no message validation (cheap).
- `scan_meta() -> dict[str, dict]` — stream_id → meta for every sidecar in the dir.
  Used once at resume to find interrupted spawns. Corrupt files are skipped with a
  warning, never raised.

### Runner checkpointing (`subagents/runner.py`)

The runner already captures in-flight messages per attempt via `_fresh_capture()`. After
each model response inside a run, flush the captured messages plus meta with
`status="running"`. On completion, a final flush sets `finished` (or `failed`). The meta
values (`name`, `task`, `parent_id`, `depth`, `granted`, `isolation`) are all known at
`_prepare_spawn` time — thread them from `_SpawnPrep` into the flush closure.

- Checkpoint failures log and continue; a checkpoint must never kill a spawn.
- `cap_transcript` applies to every checkpoint, same cap as today. For checkpoints it
  runs with `cap_reasoning=True`, so oversized text/thinking parts are clipped to the
  per-part cap alongside tool results — otherwise the mid-run payload (re-serialized
  before every model request) would grow unbounded with the reasoning stream. Final
  writes keep `cap_reasoning=False`, so a completed sidecar preserves its full reasoning.
- CLI-demuxed children keep their completion-time write (the CLI backend does not stream
  per-response boundaries we control); the parent CLI spawn gains meta on that final write;
  children stay v1 (their card state replays from the parent transcript, and
  `child_transcripts()` carries no type/task to build meta from). A CLI child interrupted
  mid-run stays lost — acceptable: the CLI owns its own resume story.

**Interrupted detection is passive:** nothing marks a sidecar `interrupted` at crash
time (there's no one alive to do it). A sidecar whose meta says `running` while no live
job owns its stream_id *is* interrupted — the resume scan makes that determination. Note
this means a *permanently-failed* native spawn also leaves its sidecar at `running` (the
terminal-status write only happens on success; a crash never runs it), so it too replays
as interrupted/resumable — the intended "retry it" semantic. The terminal `failed` meta
arm is therefore reachable only via the CLI backend, which writes its own final meta.

## 2. Finished-job history (spec §6 of 2026-06-25)

### Persistence (`jobs.py`, `session/store.py`)

`JobRegistry` gains:

- `export_settled() -> list[dict]` — summaries of terminal jobs:
  `{id, kind, label, status, result_tail, stream_id, finished_at}`. `result_tail` reuses
  the digest's tail discipline (`_DIGEST_RESULT_CHARS`-style cap) — the session payload
  must not balloon.
- `import_history(entries)` — loads prior-session summaries into a separate read-only
  `history` list (NOT into `_jobs`; they are not live, not killable, not pollable).

`Job` gains an optional `stream_id: str | None` field, set when `kind == "agent"`, so a
settled job summary can be joined back to its spawn card.

`SessionStore.save` adds a `jobs` key (the export); `load` returns it; the session
controller feeds it through on resume. Absent key → empty history (old sessions load
unchanged).

### Jobs panel

On resume the panel renders history entries read-only (dimmed, no kill action),
below/behind live jobs. History is capped (keep the most recent N=50 settled entries
across saves) so a long-lived session doesn't accrete unboundedly.

## 3. Replay: every spawn becomes a card

`replay_history` (`interfaces/tui/session_view.py`) today special-cases background
spawns into plain tool rows (session_view.py:82-84). Change:

- A background `spawn_agent` `ToolCallPart` rebuilds a `SubAgentWidget` card exactly like
  a foreground one (name/task from the call args, `stream_id` = tool_call_id). Its final
  status and report come from the jobs history joined by `stream_id`; if no history entry
  exists the sidecar meta's `status` is the fallback.
- After replay, `scan_meta()` runs once. Any sidecar with `status == "running"` belongs
  to a spawn that died mid-run: its card flips to **interrupted**. If no card exists for
  that stream_id (the owning turn never persisted before the crash), a card is
  synthesized from meta alone — no work silently vanishes.
- Transcript loading stays lazy and unchanged (`subagents_viewer._load_transcript`).

## 4. Manual resume from the screen

### Runner API (`subagents/runner.py`)

`SubagentRunner.resume_spawn(stream_id) -> str` (returns a job id):

1. Read the v2 envelope; refuse (with reason) if missing, corrupt, or v1 (no meta).
2. Refuse unless status is `running`/`interrupted` and no live job already owns this
   stream_id (one live resume per spawn).
3. **Repair the tail.** If the transcript ends with an unanswered `ToolCallPart`, patch
   it the same way the main harness does — share/extract the repair helper rather than
   duplicating the invariant ("a history must never end with a ToolCallPart lacking its
   ToolReturnPart").
4. Rebuild the sub-agent with the persisted `granted` names re-resolved against the
   current tool set. A granted name that no longer exists is dropped and noted in the
   continuation prompt. `spawn_agent` nesting follows the current depth ceiling using the
   persisted `depth`.
5. Isolation guard: if `meta.isolation` names a branch that no longer exists, refuse
   with that reason. If the branch exists, reuse its worktree — re-creating the worktree
   from the branch if it was pruned.
6. Continue the run with `message_history=<repaired transcript>` and a short continuation
   user message ("You were interrupted; continue the task."), registered as a background
   job (`kind="agent"`, same `stream_id`). Checkpointing (§1) applies to the resumed run;
   the report lands via the existing finished-job digest.

### TUI wiring (`interfaces/tui/subagents_viewer.py`)

A key action on a focused **interrupted** card calls `resume_spawn` through the existing
deps/callback seam (no interface-layer poking at runner internals, per `bind_ui`
convention). On success the card flips to running and streams live through the normal
`on_subagent_event` path; on refusal the reason renders on the card.

## Error handling

- Corrupt envelope → card shows "transcript unavailable", not resumable.
- Checkpoint write failure → warning log, spawn continues.
- Resume refusals are always surfaced with a reason on the card (missing worktree,
  corrupt sidecar, already resuming, v1 sidecar).
- `scan_meta` skips unreadable files; resume detection degrades to "fewer interrupted
  cards", never a crash.

## Testing

- `TranscriptStore`: v2 round-trip, v1 back-compat read, `read_meta`/`scan_meta`,
  corrupt-file skip.
- Runner: checkpoint after each model response (fake model, assert sidecar growth and
  status transitions `running → finished`/`failed`); meta contents match `_SpawnPrep`.
- `JobRegistry`: `export_settled`/`import_history` round-trip; history is read-only and
  capped; `stream_id` joins.
- `SessionStore`: `jobs` key round-trip; absent-key back-compat.
- Replay: background spawn builds a card; jobs-history join sets status/report;
  `running` meta flips a card to interrupted; meta-only card synthesis.
- `resume_spawn`: tail repair on a dangling ToolCallPart; grant re-resolution drops a
  vanished tool; isolation refusal on a missing branch; single-live-resume guard;
  resumed run finishes into the digest (TestModel end-to-end).
- TUI: resume action wiring and refusal rendering.
