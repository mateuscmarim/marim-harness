# Workflow Resumability Journal — Design

Date: 2026-07-12. Branch: `feat/workflow-resume-journal` off master (2d050b8,
after the deep-research batch merged).

## Problem

A workflow run that times out, raises, or is interrupted loses every completed
`agent()` result: the only recovery is re-running the whole script, re-spending
every sub-agent. Deep-research runs make this expensive — a 25-minute,
ten-researcher run that dies in round 2 must re-run round 1 from scratch.

A recorded Minor from the deep-research batch folds in: `resume_spawn` rebuilds
an interrupted spawn without its `output_schema` (the sidecar meta never
recorded it), so a resumed schema'd spawn silently loses structured output.

## Decisions (user-confirmed)

1. **Explicit resume.** `run_workflow` gains a `resume: str | None` parameter
   carrying a prior run's id. No automatic/silent reuse.
2. **Content-addressed matching.** Journal entries are keyed by a hash of the
   call's content, not by call position — robust to `asyncio.gather`
   reordering (cached hits return instantly and shuffle later invocation
   order) and to script edits.
3. **Survives restart.** The journal persists in the session store beside the
   spawn-transcript sidecars. The run id IS the run's `tool_call_id`, which
   already persists in session history, so post-restart resume needs no new
   plumbing beyond reading the file.

## Architecture

One new module, `src/marim_harness/workflows/journal.py`, owning keying and
persistence. `engine.py` grows a journal append in the spawn path and a replay
lookup before spawning. `tools/workflow_tools.py` gains the `resume` parameter
with pre-VM validation. No changes to the Monty host API — scripts are unaware
of journaling.

### journal.py

- `entry_key(task, type, model, schema, isolation, max_output_chars) -> str` —
  pure. SHA-256 over `json.dumps` of the six fields with `sort_keys=True`
  (so dict ordering inside `schema` cannot split keys). Unit-tested directly.
- `Journal` — per-run in-memory recorder: `append(key, type, task, report)`,
  serialization to/from the file payload below. Holds the run's meta
  (tool_call_id, script title, created/updated timestamps).
- `ReplayCache` — built from a loaded journal: `dict[key, deque[report]]`;
  `take(key) -> str | None` pops the oldest unconsumed entry for the key.
  Duplicate identical calls consume entries in journal order — deterministic
  because appends happen on the single-threaded event loop.
- `JournalStore` — sibling of `TranscriptStore`
  (`session/transcripts.py`): one file per run at
  `<session_path.parent>/<session_id>.workflows/t-<safe tool_call_id>.json`
  (same `_safe` sanitization rule), written with `atomic_write_text`,
  all methods best-effort (log a warning and degrade, never raise into a
  run). `save(journal)`, `load(run_id) -> Journal | None` (None on missing
  or unreadable/corrupt).

### File format

```json
{"v": 1,
 "meta": {"tool_call_id": "...", "script_title": "...",
          "created": "<iso8601>", "updated": "<iso8601>"},
 "entries": [{"key": "<sha256 hex>",
              "type": "researcher",
              "task_preview": "first 120 chars of the task",
              "report": "<full report string>"}]}
```

`task_preview` is display/debug only; matching uses `key` alone. The `report`
is always the raw string the spawn returned — schema'd calls journal the raw
report, and replay re-runs `validate_report`, so validation semantics are
identical live and cached and the format stays uniform.

### What is journaled

Only **successful** spawn reports. Failures are never journaled (they must
re-run). `log()` lines are never journaled (live progress only). For schema'd
calls the journal captures the raw report of the attempt that passed
validation (including a retry's), one entry per successful `agent()` call.

## Data flow

**Live run (no resume):** `run()` creates a `Journal` bound to the run's
`tool_call_id`. After each successful `_spawn_child` return in `_agent_call`
— for schema'd calls, after validation passes — the engine appends an entry
and `JournalStore.save` rewrites the file atomically. A crash loses at most
the in-flight spawns, never corrupts the file.

**Resumed run:** `run_workflow(script, resume="<old id>")` threads `resume`
into `engine.run()`. Before announcing the run, the engine loads the old
journal into a `ReplayCache`. In `_agent_call`, before spawning: compute the
key; on a cache hit, pop the entry, re-validate if `schema` was given, append
it to the NEW run's journal, and return — no child task is created and no
spawn card is announced. A miss (or a cached report that now fails schema
validation) falls through to a live spawn. The old journal file is left
untouched — it is the rollback baseline; the new run's file supersedes it, so
chained resumes always pass the newest run's id.

**Timing note:** the cache lookup happens in `_agent_call` before
`_spawn_child`, so cached hits also skip the abort re-check/child bookkeeping
— correct, since there is no child to track.

## Tool surface (`workflow_tools.py`)

- New parameter: `resume: str | None = None`, threaded through the
  `services.run_workflow` seam into `engine.run()` (the callable signature
  gains `resume`, exactly as it gained `timeout_secs` last batch).
- The tool layer stays thin: its only validation remains `_bad_timeout`.
  The no-journal check needs the `JournalStore`, so it lives in the engine
  as a pre-announce guard — like parse and type-check failures, it returns a
  correctable error string before `_announce_start`, so no card is ever
  claimed:
  `No journal found for run '<id>' — it may predate journaling or belong to
  another session. Re-run without resume.`
- Docstring additions (model-facing product copy):
  - `resume` semantics: pass a prior run_workflow call's tool_call_id to
    reuse its completed agent() results; unchanged calls (same task and
    options) return cached reports, changed or new calls run live.
  - The interrupted case: "if a workflow was interrupted before returning,
    you may pass that call's own tool_call_id as resume."

## Outcome messages (`engine.py`)

Failure outcomes that return text gain a resume hint when at least one entry
was journaled:

- Timeout: `Workflow timed out after {n}s; in-flight sub-agents were
  cancelled. Resume with resume="<tool_call_id>" to reuse the {k} completed
  sub-agent result(s).`
- Script raise: same suffix appended after the traceback block.

The abort path cannot return text (the turn is cancelled); the docstring
carries the instruction for that case (above). The journal file itself is
already on disk — the abort path only needs to NOT delete it, which is the
default (no cleanup is performed on abort).

## UI treatment

Minimal, via the existing workflow-card log channel (`on_workflow_log`):

- At resume start: `journal: loaded {n} cached result(s) from <old id>`.
- At run end (resumed runs only): `journal: reused {k} cached result(s),
  {m} ran live` — emitted just before the outcome is announced.

No per-hit spawn cards: a cached hit never spawns, so nothing renders in the
sub-agents tree for it. Cross-session card replay stays out of scope
(deferred roadmap).

## Fold-in: resume_spawn output_schema

- `_prepare_spawn`'s sidecar meta template gains `"output_schema":
  output_schema` (the resolved schema dict or None).
- `resume_spawn` passes `meta.get("output_schema")` into its
  `_prepare_spawn` call, so a resumed schema'd spawn rebuilds with
  structured output. Closes the recorded Minor from the deep-research batch.

## Error handling summary

| Condition | Behavior |
|---|---|
| `resume` id has no journal file | Correctable tool error before execution |
| Journal file corrupt/unreadable | `load` returns None → same correctable error |
| Cached report fails current schema | Treated as a miss; spawn live |
| Journal write fails | `logger.warning`, run continues (best-effort) |
| Abort mid-run | Journal keeps entries written so far; resumable |
| No session store (headless without sessions) | Journaling silently off; `resume` returns the no-journal error |

## Out of scope (deferred roadmap)

Detachable/background workflows, saved named workflows, cross-session card
replay, resuming interrupted *children* from their transcript sidecars (an
incomplete child re-runs live from scratch).

## Testing

- **Pure keying:** identical args → same key; each field differing → different
  key; schema dict-ordering irrelevant; None vs absent options normalized.
- **Store round-trip** against a tmp session dir: save → load equality;
  corrupt file → None; missing → None; atomic overwrite on repeated saves.
- **Engine replay** with a fake spawn callable:
  - full-hit resume spawns zero children and returns the same final result;
  - partial hit — an edited task runs live, unchanged ones are cached;
  - duplicate identical calls consume distinct entries in order;
  - schema'd cached report re-validates on replay; a now-invalid one falls
    through to a live spawn;
  - abort mid-gather journals completed results; resume completes with only
    the missing calls run live;
  - timeout and script-raise outcomes advertise the resume id and count;
  - bad resume id → correctable error, nothing executed, no card announced.
- **Tool layer:** `resume` threading through the seam; validation ordering
  (unavailable → bad timeout at the tool, bad resume in the engine before
  announce); docstring anchors for the new copy.
- **resume_spawn:** meta records `output_schema`; rebuild passes it through
  (extends the existing resume tests).
- **Live smoke** (controller-run, free local model, no paid models):
  interrupt a two-round workflow mid-round-1, resume with the same script and
  the advertised id, verify cached round-1 hits (journal log lines, no
  re-spawned round-1 children) and a completed run.
