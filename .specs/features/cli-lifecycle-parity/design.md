# CLI lifecycle parity design

Status: Implementation design under the approved spec (2026-09-14).

## Architecture

Reuse the approved notice path and existing activity ledger. A normalized notice
is display-only, never a callable tool or assistant prose. Adapters publish it
through the same optional activity callback that already connects CLI tools to
the session host. The host maps it to `session.notice`. Without a callback,
non-ephemeral models print the notice to stderr.

`BackendNotice` contains message, backend, kind, severity, and a stable id.
Claude ids include its process/session identity and event uuid. Codex ids use
its thread/turn and item identity when present; otherwise assign one occurrence
id at receipt. Identical text is never a deduplication key.

The activity ledger records a notice in order with tool and response-part
entries. At persistence, expansion inserts an empty TextPart whose
`provider_details.backend_notice` is the normalized record and whose
`provider_name` is `marim`. It contains no model-facing notice prose. This
keeps notice order relative to tool calls/results and survives the existing
history serializer. Renderers recognize that metadata before ignoring empty
text, and deduplicate history/live overlap by notice id. The normal model
input path excludes empty notice-only parts if a provider requires it.

Compaction completions update or invalidate the adapter context report. They
never emit the mirror-compaction resync event. Optional telemetry and inventory
are adapter state exposed through additive session-detail fields. Observed
backend state cannot end a turn or resolve an ask. Existing result, closure,
stale-turn and background-agent ownership rules remain authoritative.

## Components and reuse

| Component | Existing seam | Change |
| --- | --- | --- |
| Notice value and ledger | config/external_cli, runtime/cli_activity | Ordered normalized notice metadata and best-effort delivery |
| Claude adapter | consume_cli_stream and stream response | Public system events, init inventory, result details |
| Codex adapter | ItemTranslator, turn_events, server notification routing | Notices, completed compaction, retry visibility, scoped global warnings |
| Session/TUI | stream_events, wire_events, SessionView | Live and history notice rendering; session telemetry |
| Android transcript | TranscriptMapper and Room repository | Whitelist notices, map metadata markers, preserve merge invariants |
| Stats | TurnEvent and recorder | Optional normalized backend details; existing totals unchanged |

## Protocol findings

Installed Claude Code is 2.1.270, Codex CLI is 0.154.0. Codex schema generated
locally with `codex app-server generate-json-schema`. Claude stream-json schema
and emission call sites inspected in the installed binary, not user sessions.
These establish schema and emission support, not a live observed test for each
rare event. Fixtures must be labelled synthetic examples of those shapes.

Claude `model_fallback` is explicitly @internal and is excluded. The public
`model_refusal_fallback` includes original_model, fallback_model and content.
`compact_boundary` uses nested compact_metadata. Session-state events are
conditional on the CLI's own emission option; do not enable private flags.
Codex `thread/compacted` is deprecated but supported; completed
contextCompaction items are preferred. Deduplicate the two representations of
one completion while retaining distinct compactions.

Sources: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Claude streaming](https://code.claude.com/docs/en/agent-sdk/streaming-output),
and installed versioned schemas. Versioned local schemas take precedence over
examples for a different installed CLI version.

## Risks & Concerns

| Concern | Impact | Mitigation |
| --- | --- | --- |
| Claude background process ownership | Premature turn completion or lost agent reports | Keep process termination rules; test existing fake background scenarios |
| Codex app-server shared across sessions | Warning leakage or duplicate child delivery | Route by owning handle, broadcast global notices once per root connection |
| History activity expansion | Duplicate cost or reordered notices | Metadata marker in existing ledger; one usage-bearing terminal response |
| Mobile compaction resync | Lost cached transcript | Notices never emit compaction.finished |
| Optional fields evolve | A warning could break a turn | Strict type checks on used fields, lenient unknown fields |
| Notice callback exceptions | Rendering failure could abort work | Best-effort boundary and payload-free diagnostics |
| Existing tests expect dropped notices | Incorrect regression expectation | Add spec tests; explicitly identify old expectation conflicts before changing them |
