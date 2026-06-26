# Resumable Sub-Agent Transcripts + Finished-Job History — Design

**Status:** Design (approved in brainstorming, pending spec review)
**Date:** 2026-06-25

## Goal

Make a resumed session show **what each sub-agent did**, not just its final
report — the full step-by-step transcript (tool calls, reasoning, text) of every
sub-agent. Also restore the **finished background jobs** history so the Jobs panel
survives a restart.

## Background: what resumes today

`SessionStore.save` persists the conversation message history (with images
externalized), plus `usage`, `tasks`, `model`, and `duration`. Because the history
includes each `spawn_agent` tool call **and its final report** (the tool result),
`replay_history` (`interfaces/tui/session_view.py`) rebuilds each foreground
sub-agent as a `SubAgentWidget` card showing the report.

What is **lost** on resume, documented at `session_view.py:100`:

> *"Its nested transcript was never persisted, so the resumed card carries only
> the final report — its resting state."*

The sub-agent's streamed steps live only in in-memory `SubAgentPane` widgets and
are never written to disk. Background jobs are process-scoped (`JobRegistry`) and
vanish entirely on restart.

## Scope

**In scope**

- Persist each sub-agent's full execution transcript (foreground, background, and
  CLI-backed) so a resumed pane shows its steps.
- Persist finished background-job results/digests so the Jobs history survives a
  restart.

**Out of scope (explicit non-goals)**

- Resuming a **running** background job across a process restart. A killed
  subprocess (CLI sub-agent) or a dead in-process model loop has no continuation
  point; only a *finished* job's result is meaningful. Running jobs are lost on
  restart, exactly as today.

## Decisions (locked during brainstorming)

1. **Fidelity:** full message history per sub-agent, but large tool *results*
   (file reads, bash output) are truncated with a `…(truncated, N chars)` marker.
   You see every step and the agent's reasoning; you don't re-store megabytes of
   re-readable tool output.
2. **Format:** a transcript is a `list[ModelMessage]` — the same type the main
   history uses — so persistence and replay reuse existing machinery.
3. **Storage:** sidecar files, **write-once** per sub-agent, **lazy-loaded** on
   pane open. Chosen because a transcript is immutable once its sub-agent
   finishes, while the session JSON is re-serialized every turn; embedding
   transcripts inline would re-dump megabytes per turn.

## Architecture

### 1. Transcript as `list[ModelMessage]`

Every transcript is stored as a `ModelMessage` list, regardless of backend:

- **Native sub-agents** (in-process pydantic-ai `Agent`): use the run result's
  `all_messages()` directly.
- **CLI sub-agents** (`backend: claude-cli`): the CLI path produces stream-json,
  not pydantic-ai messages. Synthesize a `ModelMessage` list from the same blocks
  `CliStreamTranslator` already parses:
  - an assistant message's `text` / `tool_use` blocks → a `ModelResponse` with
    `TextPart` / `ToolCallPart`;
  - a `tool_result` block → a `ModelRequest` with a `ToolReturnPart`.

  This keeps **one** persisted format and **one** replay path for both backends.
  Tool-name/arg normalization already applied for live rendering
  (`normalize_cc_tool`) is reused so the synthesized messages match native shape.

  *This synthesis is the largest new piece of work.*

### 2. Capping

A pure helper `cap_transcript(messages, cap) -> list[ModelMessage]` walks the
messages and truncates each `ToolReturnPart.content` longer than `cap`, appending
`…(truncated, N chars)`. Mirrors the existing `cap_subagent_output` philosophy
(which truncates a final report); here it truncates each tool *result* inside the
transcript. Applied once, just before the sidecar write. The per-tool-result cap
is configurable via `MARIM_SUBAGENT_TRANSCRIPT_CAP`, **default 2000 chars** —
enough to see the shape of a Read/Grep/bash result without storing whole files.
Only `ToolReturnPart.content` is capped; text/thinking/tool-call parts are kept
in full (they carry the reasoning and the actions, which are the point).

### 3. Sidecar store

New module `session/transcripts.py` exposing `TranscriptStore`, bound to a
session's directory:

```
sessions/<id>.json              # main history + finished-job summaries (small)
sessions/<id>.subagents/
    toolu_ab12.json             # one capped transcript, written once
    toolu_cd34.json
```

- `write(stream_id, messages)` — atomic write of
  `ModelMessagesTypeAdapter.dump_json(capped)` to
  `<dir>/<sanitized stream_id>.json`. Called once when a sub-agent finishes.
- `read(stream_id) -> list[ModelMessage] | None` — lazy, on pane open; returns
  `None` when absent.
- `delete_all()` — remove the `.subagents/` dir on session delete / `/clear`.
- Sanitization of `stream_id` (a tool_call_id) into a safe filename mirrors
  `pane_id`'s existing approach.
- Orphaned transcripts (e.g. spawns after a checkpoint rewind point) are harmless
  and simply never read.

The store is built in `build_collaborators` alongside the session and handed to
`SubagentRunner`.

### 4. Capture wiring (`subagents.py`)

When a sub-agent finishes — in the foreground spawn lifecycle, the background
lifecycle, and the CLI lifecycle — the runner calls
`transcript_store.write(stream_id, transcript)`. Best-effort: a write failure logs
a warning and never affects the returned report or the turn.

### 5. Resume rendering (`session_view.py`)

- Generalize `replay_history`'s per-part rendering into a routine that can mount
  into either the log or a `SubAgentPane`.
- On resume, each foreground `spawn_agent` call mounts its `SubAgentWidget` card
  immediately (report-only, as today) and registers the pane for lazy load.
- The first time the user **opens** that pane (`open_subagents_at` / pane show),
  if its transcript isn't loaded yet, `TranscriptStore.read(stream_id)` is called
  and the messages are replayed into the pane. Capped tool outputs render with
  their truncation markers.
- No sidecar (old session, or a failed write) → graceful fallback: the pane shows
  a short "transcript unavailable" note; the card's report is unaffected.

### 6. Finished-job history

- Extend the session payload with a `jobs` field: a small list of settled
  background-job summaries `{id, label, kind, status, result}` (no transcripts —
  those are in sidecars).
- `SessionStore.save` / `load` round-trip it; `_render_jobs` shows the settled
  history on resume. Running jobs are not restored.

## Data flow

```
sub-agent finishes
  ├─ native: result.all_messages()      ┐
  └─ cli:    synthesize from stream      ┘ → cap_transcript → TranscriptStore.write(id)
                                              (write-once sidecar)

session save (per turn)
  └─ main history + tasks + usage + finished-job summaries  (sidecars untouched)

resume
  ├─ replay_history → SubAgentWidget cards (report-only) + register panes
  ├─ _render_jobs   → settled-job history
  └─ open a pane    → TranscriptStore.read(id) → replay into pane (lazy)
```

## Error handling

- All persistence is **best-effort**; a broken file can never break a turn
  (codebase rule). Sidecar write failure → warn + continue. Corrupt/missing
  sidecar on read → fallback note.
- **Backward compatible:** old sessions have no `.subagents/` dir and no `jobs`
  field; resume behaves exactly as today.

## Testing

- `TranscriptStore` write/read round-trip; `read` returns `None` when absent;
  `delete_all` removes the dir.
- `cap_transcript` truncates over-cap `ToolReturnPart.content` and leaves small
  content and non-tool parts untouched.
- CLI stream → `ModelMessage` synthesis: assistant text/tool_use and tool_result
  blocks produce the expected `ModelResponse` / `ModelRequest` parts.
- Replay-into-pane renders steps; lazy-load triggers on first pane open and not
  before.
- Finished-job summaries persist and re-render on resume; running jobs do not.
- Backward compatibility: a session file with no sidecar dir / no `jobs` field
  resumes without error and shows report-only cards.

## Components & boundaries

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `cap_transcript` | pure: truncate over-cap tool results | `ModelMessage` types |
| `TranscriptStore` | sidecar write-once / lazy read / delete | session dir, `ModelMessagesTypeAdapter` |
| CLI synth helper | stream-json → `list[ModelMessage]` | `CliStreamTranslator` blocks, `normalize_cc_tool` |
| `subagents.py` capture | call `write` on completion (3 paths) | `TranscriptStore` |
| `session_view` replay | mount steps into log or pane; lazy-load | `TranscriptStore`, existing renderers |
| `SessionStore` jobs field | persist/restore finished-job summaries | — |
