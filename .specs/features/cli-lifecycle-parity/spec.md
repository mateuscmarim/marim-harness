# CLI backend lifecycle parity — Phase 3 specification

Date: 2026-09-14
Status: Approved by Mateus on 2026-09-14; implemented and independently verified locally.
Harness branch: `feat/cli-lifecycle-parity`, based on `origin/master` at
`ff9c58d7` (v0.8.0).
Roadmap: `docs/plans/cli-backend-parity-roadmap.md`, Phase 3.

## Problem Statement

The CLI backends perform compaction, reroute models, issue warnings and run
background work without consistently exposing that activity in marim. Codex
already translates some notices, but its streaming adapter discards them;
Claude drops most system subtypes. Users need these events in the local
transcript, attached TUI, headless output and mobile transcript without
changing turn execution or billing.

## Goals

- Show a backend compaction completion or model reroute during the same turn,
  before that turn's completion event reaches the client.
- Preserve background-agent routing, approval ownership, interrupt behavior
  and the context/cost corrections shipped in phases 1 and 2.
- Account for every Phase 3 roadmap item with tested behavior or recorded
  evidence that its transport does not expose the capability.
- Keep notices separate from assistant prose and executable tool calls.

## Out of Scope

| Feature | Reason |
| --- | --- |
| Codex push quota, plan, diff and name synchronization | Phase 4. |
| Conversation rollback, rewind and forks | Phase 5. |
| Skill roots, context injection and workspace sharing | Phase 6. |
| New Android inventory, MCP, stats or background-jobs screens | Existing transcript rendering is the mobile scope; new screens need their own feature. |
| Replacing either CLI's tools or permissions | The launcher architecture and approval broker remain authoritative. |
| Private or `@internal` protocol dependencies | The roadmap excludes them. |
| Triggering actual compaction, commits or paid turns as routine tests | Replay fixtures and fake transports provide deterministic CI checks. |
| Publishing, merging or deploying | Separate authorization after local verification. |

## Assumptions & Open Questions

| Assumption / decision | Chosen default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Scope of "start phase 3" | Prepare the full Phase 3 spec first, then implement approved slices locally | Phase 3 changes both execution lifecycle and multiple clients. | Approved 2026-09-14. |
| Mobile parity | Include notice persistence and rendering in the existing Android transcript | The current mobile repository filters out `session.notice` before Room insertion. | Approved 2026-09-14. |
| Notice transport | Extend existing `session.notice` additively with optional backend, kind, severity and stable notice identity | The local and remote TUI already consume its required `message` field. | Approved 2026-09-14. |
| Compaction semantics | Backend compaction uses a backend notice and context-report update, never marim's `compaction.finished` | Mobile uses `compaction.finished` to clear cached history; backend compaction does not compact marim's mirror. | Approved 2026-09-14. |
| Durable transcript notes | Store selected visible notices with response metadata and replay them with a stable identity | Reopening history should retain the explanation for a compaction or reroute without adding model-facing prose. | Approved 2026-09-14. |
| Volatile readings | Thinking counters, progress updates and inventory snapshots replace state rather than append permanent transcript rows | These values change frequently; replaying every update would obscure the transcript. | Approved 2026-09-14. |
| Backend state signals | Treat them as observations; retain result/completion and process-closure rules for finishing turns | Recent background-agent fixes depend on explicit ownership and completion handling. | Approved 2026-09-14. |
| Events not present headlessly | Verify each roadmap event against installed protocol evidence; record unsupported items explicitly and preserve existing behavior | The roadmap already records that Claude `system/status` is absent on this transport. A schema name alone does not prove delivery. | Roadmap rule; individual capability findings pending. |
| Backend slash commands | Expose verified callable commands without shadowing marim commands; display unsupported invocation forms as inventory only | Discovery must not imply that marim can execute a command it cannot forward. | Approved 2026-09-14. |
| Account-global warnings | Deliver only to sessions using the matching backend connection; never associate them with an unrelated thread | Codex shares an app-server among sessions. | Approved 2026-09-14. |

**Open questions:** none at the product-decision level — defaults are recorded
above for review. Protocol availability remains an explicit implementation
investigation: unavailable capabilities must be reported, not silently marked
implemented. A narrower deliverable requires a recorded scope decision.

## Existing Code Evidence

- `config/claude_cli_model.py`: `_event_chunks` reads init identity but drops
  other system subtypes. `_is_subagent_noise` filters several task events.
- `claude/process.py`: `_on_event` and background tracking already manage
  Claude-initiated turns; preserve these ownership rules.
- `codex/translate.py`: `Notice` exists for warnings and a
  `contextCompaction` item start. Start is not evidence of successful completion.
- `config/codex_cli_model.py`: non-streaming requests log notices;
  `_events_for` omits them in streaming requests.
- `codex/turn.py`: `_fold` retains retry failure text but suppresses the
  retry notification from callers.
- `runtime/harness.py`: `wire_cli_model` is the shared late-binding seam for
  model callbacks, including rebinding after model switches.
- `server/host.py`, `server/wire_events.py`, `interfaces/tui/app.py`:
  `session.notice` already has publishing, parsing and rendering support.
- Mobile `domain/TranscriptMapper.kt`: `RENDERED_LIVE_TYPES` omits notices;
  `data/repo/TranscriptRepository.kt` uses it to filter persisted events.
- Mobile `TranscriptRepository.kt`: backend notices must not enter the
  `compaction.finished` resync path.

## User Stories

### P1: See backend compaction, reroutes and warnings

As a session user, I want backend lifecycle notices beside the work they
explain, whether I watch locally, remotely or on my phone.

**Acceptance Criteria**:
1. WHEN a supported Claude compaction completion or Codex compaction completion arrives THEN marim SHALL emit a notice identifying the backend and completed compaction before publishing completion of that same turn. (LIFE-01)
2. WHEN a supported model reroute or fallback arrives THEN marim SHALL emit a notice naming the old and new model when those names are supplied. (LIFE-02)
3. WHEN a supported notification, permission denial, refusal fallback, warning, config warning, deprecation or guardian warning arrives THEN marim SHALL emit a notice with its normalized kind and available message. (LIFE-03)
4. WHEN Codex reports an error with retry enabled THEN marim SHALL show a retry notice while continuing to await the turn's existing terminal signal. (LIFE-04)
5. WHEN a notice is emitted THEN the local and attached TUI SHALL render the same message as a notice separate from assistant text. (LIFE-05)
6. WHEN a notice is emitted in headless mode THEN the CLI SHALL write its human-readable message to stderr without adding it to assistant stdout. (LIFE-06)
7. WHEN a server publishes a notice THEN marim SHALL use a sequenced `session.notice` event whose `message` remains readable by an older TUI client. (LIFE-07)
8. WHEN mobile receives a valid `session.notice` THEN mobile SHALL persist it through `insertAndPrune` before rendering it as a transcript note. (LIFE-08)
9. WHEN mobile receives the same notice event sequence twice THEN mobile SHALL display one copy of that notice. (LIFE-09)
10. WHEN a backend compaction notice arrives THEN marim SHALL leave the mirrored conversation and mobile history cache intact. (LIFE-10)

**Independent Test**: Fake each backend delivering text, a lifecycle event,
more text and completion. Assert notice delivery occurs before completion,
assistant text is unchanged and the attached event stream carries the same
message. Replay that stream through Room and the Android mapper.

### P1: Preserve truthful context and turn state

As a session user, I want telemetry to reflect backend activity without
prematurely ending my turn or altering its bill.

**Acceptance Criteria**:
1. WHEN a backend supplies a valid post-compaction context count THEN marim SHALL replace the previous used count while retaining the last valid context window if the event omits it. (LIFE-11)
2. WHEN compaction completes without a post-compaction count THEN marim SHALL invalidate the prior backend used count until a fresh backend reading arrives. (LIFE-12)
3. WHEN a supported thinking-token estimate arrives THEN marim SHALL expose that estimate as current-turn display telemetry without adding it to billed token totals. (LIFE-13)
4. WHEN a supported session state change arrives THEN marim SHALL expose the backend's idle, running or requires-action observation without synthesizing a terminal result. (LIFE-14)
5. WHILE a brokered ask is unresolved, marim SHALL retain its ask identifier and resolution ownership regardless of backend state observations. (LIFE-15)
6. WHEN a stale terminal or state event from a previous turn arrives THEN marim SHALL preserve the current turn's existing completion and approval state. (LIFE-16)
7. WHEN a supported backend VCS-change event arrives THEN marim SHALL refresh the existing git/checkpoint view for that workspace without creating or restoring a checkpoint. (LIFE-17)

**Independent Test**: Feed compaction with and without token counts, a thinking
estimate, state transitions around a pending ask, a stale completion and a
VCS-change event. Assert exact context, billing and state outcomes; verify
existing autonomous-turn and interrupt tests still pass.

### P2: See background shells and backend inventory

As a session user, I want background work and the tools loaded by the CLI to
be visible without duplicate agent cards or misleading command suggestions.

**Acceptance Criteria**:
1. WHEN a supported background-shell start arrives with a stable task identifier THEN marim SHALL open one activity card identified by backend session and task identifier. (LIFE-18)
2. WHEN progress or completion arrives for that task THEN marim SHALL update the same card with the reported status instead of appending another card. (LIFE-19)
3. WHEN a task event belongs to an already-demuxed agent THEN marim SHALL preserve the existing agent card without creating a background-shell duplicate. (LIFE-20)
4. WHEN the owning CLI process closes THEN marim SHALL settle its still-running shell cards as interrupted or failed according to the closure outcome. (LIFE-21)
5. WHEN Claude init supplies valid tools, MCP statuses, slash commands, agents or permission mode THEN marim SHALL expose a backend-labelled inventory snapshot through session detail and the corresponding existing TUI views. (LIFE-22)
6. WHEN a new CLI process supplies an inventory THEN marim SHALL replace that backend process's prior inventory instead of merging stale entries into it. (LIFE-23)
7. WHEN a backend command name conflicts with a marim command THEN marim SHALL preserve the marim command's existing dispatch. (LIFE-24)
8. WHEN a verified invocable backend slash command is selected THEN marim SHALL forward its invocation to that CLI session using the verified transport path. (LIFE-25)

**Independent Test**: Interleave shell and agent task lifecycles, including
duplicate progress and process closure. Feed two different init inventories
and a command-name collision; assert one card per task, replacement semantics,
session scoping and unchanged built-in command dispatch.

### P2: Retain useful lifecycle history and result details

As a returning session user, I want important notices and backend result
details to survive a normal save and resume.

**Acceptance Criteria**:
1. WHEN a turn containing visible lifecycle notices is persisted THEN marim SHALL store their normalized records outside model-facing text. (LIFE-26)
2. WHEN persisted history is reopened THEN the local TUI, attached TUI and mobile SHALL restore each stored notice once in its recorded order relative to that turn's visible activity. (LIFE-27)
3. WHEN live replay overlaps restored history THEN clients SHALL deduplicate a notice by its stable identity while preserving distinct occurrences with identical messages. (LIFE-28)
4. WHEN Claude result supplies valid permission-denial, turn-count, API-duration or stop-reason details THEN marim SHALL retain normalized details in the turn's persisted stats metadata without changing cumulative cost-delta accounting. (LIFE-29)
5. WHEN an older ledger entry lacks lifecycle metadata THEN marim SHALL load and query it with the same token and cost totals as before this feature. (LIFE-30)

**Independent Test**: Save a turn with two different same-text notices,
interleaved activity and result details; reload it through history and replay
the live tail. Assert both distinct notices survive exactly once, keep their
order and never enter the assistant response text or increase billed cost.

### P1: Tolerate optional and evolving backend events

**Acceptance Criteria**:
1. IF a lifecycle payload is malformed or its subtype is unknown THEN marim SHALL ignore that optional payload and continue processing the next valid event. (LIFE-31)
2. IF a new optional callback or inventory refresh fails THEN marim SHALL preserve normal turn execution and record a diagnostic containing the operation and backend identity without the raw payload. (LIFE-32)
3. WHEN a child-thread or child-agent lifecycle notice arrives THEN marim SHALL route it to that child's existing stream instead of presenting it as the parent session's own activity. (LIFE-33)
4. WHEN a supported account-global warning arrives THEN marim SHALL deliver it only to sessions bound to that backend connection. (LIFE-34)
5. WHERE a roadmap capability is unavailable on the supported headless transport, the capability report SHALL name the unavailable event, evidence, version and retained fallback before Phase 3 is reported complete. (LIFE-35)

**Independent Test**: Mix unknown and malformed payloads, callback failures,
two parent sessions and a child stream. Assert valid turns complete, routing
stays scoped and diagnostic output excludes payload content.

## Edge Cases

- Compaction started followed by an error: no success notice or fabricated
  post-compaction count.
- Two compactions in one turn: two occurrences, not message-text deduplication.
- Root model switches and process adoption: callbacks follow the new adapter;
  inventory and notice identity retain the actual owning process/session.
- Cancellation before final response: persist notices already observed wherever
  the existing resumable partial-response path persists the turn.
- A replayed notice with no optional metadata: render its valid message;
  missing metadata does not make older `session.notice` events invalid.
- Invalid numeric telemetry includes booleans, negative numbers, strings and
  non-finite values; none becomes a valid token count or duration.
- Backend progress does not convert a user-denied tool into successful work.

## Implicit Requirement Dimensions

| Dimension | Resolution |
| --- | --- |
| Validation and bounds | LIFE-31; typed normalized fields, invalid numeric values excluded. |
| Failure and partial failure | LIFE-04, LIFE-21, LIFE-32; existing cancellation persistence retained. |
| Idempotency and retries | LIFE-09, LIFE-19, LIFE-20, LIFE-28. |
| Auth boundaries and rate limits | LIFE-33, LIFE-34; existing authenticated serve routes, no new polling or credentials. |
| Concurrency and ordering | LIFE-01, LIFE-16, LIFE-27; existing turn ownership and stream ordering remain. |
| Data lifecycle and expiry | LIFE-23, LIFE-26 through LIFE-30; existing history retention and Room cap, no new retention policy. |
| Observability | LIFE-32, LIFE-35; diagnostics name backend and operation, never prompts or raw payloads. |
| External-dependency failure | LIFE-31, LIFE-32, LIFE-35; unsupported events never become required for turn completion. |
| State transitions | LIFE-10 through LIFE-17, LIFE-21; observation is separate from completion and permission ownership. |

## Requirement Traceability

| Requirement ID | Story | Phase | Status |
| --- | --- | --- | --- |
| LIFE-01 | Compaction notice | Execute | Verified |
| LIFE-02 | Reroute notice | Execute | Verified |
| LIFE-03 | Backend warnings | Execute | Verified |
| LIFE-04 | Retrying errors | Execute | Verified |
| LIFE-05 | Local and attached TUI | Execute | Verified |
| LIFE-06 | Headless stderr | Execute | Verified |
| LIFE-07 | Serve notice contract | Execute | Verified |
| LIFE-08 | Android Room path | Execute | Verified |
| LIFE-09 | Android sequence deduplication | Execute | Verified |
| LIFE-10 | Compaction cache isolation | Execute | Verified |
| LIFE-11 | Post-compaction count | Execute | Verified |
| LIFE-12 | Unknown post-compaction count | Execute | Verified |
| LIFE-13 | Thinking telemetry | Execute | Verified |
| LIFE-14 | Backend state observations | Execute | Verified |
| LIFE-15 | Approval ownership | Execute | Verified |
| LIFE-16 | Stale events | Execute | Verified |
| LIFE-17 | VCS refresh | Execute | Verified |
| LIFE-18 | Shell task start | Execute | Verified |
| LIFE-19 | Shell progress and completion | Execute | Verified |
| LIFE-20 | Agent demux compatibility | Execute | Verified |
| LIFE-21 | Process closure | Execute | Verified |
| LIFE-22 | Init inventory | Execute | Verified |
| LIFE-23 | Inventory replacement | Execute | Verified |
| LIFE-24 | Built-in command precedence | Execute | Verified |
| LIFE-25 | Backend command invocation | Execute | Verified |
| LIFE-26 | Notice persistence | Execute | Verified |
| LIFE-27 | History order | Execute | Verified |
| LIFE-28 | History/live deduplication | Execute | Verified |
| LIFE-29 | Backend result stats | Execute | Verified |
| LIFE-30 | Older ledger compatibility | Execute | Verified |
| LIFE-31 | Optional parsing | Execute | Verified |
| LIFE-32 | Callback failure and diagnostics | Execute | Verified |
| LIFE-33 | Child routing | Execute | Verified |
| LIFE-34 | Shared backend isolation | Execute | Verified |
| LIFE-35 | Capability evidence | Execute | Verified |

Coverage: all 35 requirements verified. Exact acceptance evidence and the
independent discrimination sensor are recorded in [validation.md](validation.md).

## Proposed Implementation Sequence

1. Establish a versioned capability matrix and sanitized protocol fixtures for
   every Phase 3 roadmap event. Verify: each event has schema/recording evidence
   or an explicit unavailable disposition, including headless-delivery limits.
2. Deliver compaction, reroute, warning and retry notices from both adapters
   through the common notice path. Verify: same-turn local/remote/headless
   delivery, non-streaming requests and callback-failure isolation.
3. Persist normalized notices and support history replay. Verify: stable
   identities, activity ordering, partial turns and replay deduplication.
4. Add context invalidation, thinking/state observations and VCS refresh.
   Verify: no premature completion, invented usage or checkpoint writes.
5. Add background shell lifecycle and backend inventory to existing surfaces.
   Verify: task identity, agent routing, command precedence and process replacement.
6. Retain backend result stats and complete Android notice rendering through
   Room. Verify: old ledger compatibility, Android mapper/repository tests,
   notice replay and no compaction-triggered history reload.
7. Run integration and regression checks, independently verify the acceptance
   criteria, and update the roadmap with supported and unavailable capabilities.
   Verify: protocol fixture → adapter → serve → client evidence and adversarial
   checks for notice loss, wrong-session routing and duplicate replay.

These are implementation slices, not the final atomic task breakdown. Design
and tasks will name individual files and executable gates after spec approval.

## Success Criteria

- Recorded Claude compaction and Codex reroute fixtures produce notices before
  completion over the shared serve path and render on TUI and mobile.
- Existing autonomous-turn, sub-agent, approval, interrupt, usage and cost
  regression tests remain green.
- Every Phase 3 item is covered or explicitly reported unavailable with evidence.
- Harness checks run in CI order: `uv run ruff check src tests`,
  `uv run ruff format --check src tests`, `uv run pyright`, `uv run pytest`;
  package check: `uv build`.
- Mobile checks: `./gradlew ktlintCheck detekt`,
  `./gradlew testDebugUnitTest`, `./gradlew :app:assembleDebug`.
- A fresh independent verifier records acceptance evidence and the results of
  deliberately breaking notice delivery, identity or routing in isolated scratch.
