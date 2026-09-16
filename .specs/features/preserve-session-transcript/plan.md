# Preserve the session transcript through compaction

Date: 2026-09-16
Status: Approved by the user on 2026-09-16; checks and build authorized.
Profile: light (TLC Lean default).
Implementation branch: `fix/preserve-session-transcript`, based on `fb558301`.

## Sources

- Conversation: mobile transcript truncation report and screenshot; request to
  publish current work, babysit the PR, fix findings, and use TLC skills.
- Saved affected session: the first persisted message is a compaction summary;
  earlier commentary is no longer present. Latest response input usage is
  2,642,890, while its `cli_context.used` is 119,343 and window is 258,400.
  No private conversation content is copied into these artifacts.
- `compaction.last_request_input_tokens`, `session/ctrl.py`, `session/store.py`,
  `server/http.py:get_history`, TUI replay, and checkpoint code at `fb558301`.
- `.specs/STATE.md`: AD-001 preserves CLI mirror history across backend
  compaction; AD-002 in the working branch requires reuse of the upstream
  dependency boundary. This feature introduces no dependency change.
- [Upstream compaction](https://pydantic.dev/docs/ai/harness/compaction/) and
  [step persistence](https://pydantic.dev/docs/ai/harness/step-persistence/),
  checked against installed Harness 0.31.0 exports and its bundled README:
  compaction receipts identify stored runs, not an immutable original transcript.

## Problem

Mobile users lose earlier messages after server compaction and a history reload.
The server returns compacted model context as the displayed conversation. Mobile
does not render the system summary, so the missing messages appear silently
truncated. CLI turn-wide input usage also reaches the last-request size reader,
causing a long tool-running turn to look much larger than its actual context.

After this change, context accounting will distinguish current context from
aggregate spend, and compaction will not remove already-recorded conversation
messages from history replay. This prevents future loss; it cannot reconstruct
messages already removed from an old session file.

## Out of scope

| Excluded | Why |
| --- | --- |
| Claude cancellation implementation | Already isolated in PR #149; babysitting remains a separate obligation |
| Restoring previously discarded messages from CLI files or backups | Requires separate recovery sources and fidelity decisions |
| Mobile delta-cache pruning, layouts, and new screens | Saved server data explains this incident; no Android change is required for this fix |
| Changing upstream compaction algorithms or replacing the session engine | Reuse upstream reduction and existing resumability boundaries |
| Replacing session storage with StepPersistence | Adds a second lifecycle/migration and does not itself guarantee an unmodified transcript |
| New search, export, retention settings, or telemetry services | Not needed to preserve current history replay |
| Merge or deployment | User requested publication and babysitting, not merge or deployment |

## Assumptions

| Assumption | Chosen default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Transcript retention | Preserve recorded messages until explicit rewind, clear, or session deletion | Compaction is a context operation, not user-requested deletion | y |
| Persistence cost | Store a separate transcript array in the existing atomic session JSON | More disk/serialization work, but avoids a cross-file atomicity protocol | y |
| Existing sessions | Use saved model messages as their initial transcript; no bulk rewrite | Previously discarded messages cannot be recreated faithfully | y |
| TUI parity | Local and attached TUI replay use the same transcript semantics as mobile | Prevents two different definitions of displayed history | y |
| CLI report unavailable | Use the existing estimator without aggregate CLI usage as its floor | Unknown current size is preferable to a known-wrong aggregate | y |
| Verification profile | light, with focused red/green regressions and independent verification | Project default; does not claim fault-injection or exhaustive set-member verification | y |

Open questions: none — defaults above were approved.

## Criteria

### S1: Context measurement is not cumulative spend (P1)

**Acceptance criteria**

1. WHEN the newest response is from Claude CLI or Codex CLI and has a valid `cli_context.used` THEN the last-request measurement SHALL equal that value, including zero, regardless of aggregate `usage.input_tokens`.
2. IF that CLI response has no valid context report THEN the last-request measurement SHALL be absent, without substituting aggregate usage or an older response's measurement.
3. WHEN a native-provider response supplies request input usage THEN the measurement SHALL retain the existing native-provider behavior.
4. WHEN a CLI response reports 2,642,890 aggregate input tokens and 119,343 current-context tokens, with an estimate below a configured 150,000-token trigger, THEN the automatic compaction gate SHALL make zero reduction calls.
5. The usage ledger SHALL retain the aggregate CLI input-token total unchanged.

**Independent demonstration:** exercise both CLI provider names, native usage,
missing/malformed/zero reports, and the gate with deterministic messages.

### S2: Compaction preserves recorded conversation messages (P1)

**Acceptance criteria**

6. WHEN a committed reduction summarizes, trims, or clears tool results THEN the display transcript SHALL retain the same pre-reduction messages in the same order and with the same recorded content.
7. WHEN a clean turn follows a reduction THEN the display transcript SHALL
   append each newly committed message exactly once, without appending synthetic
   summary messages or duplicating retained context messages.
8. WHEN multiple reductions occur across turns THEN replay SHALL retain each
   committed original message exactly once in chronological order.
9. WHEN the next model request runs after reduction THEN its message history SHALL be the reduced context, not the preserved display transcript.
10. WHEN a session is persisted and resumed THEN its display transcript SHALL
    equal the pre-restart transcript.
11. WHEN an old session has no separate transcript THEN replay SHALL return its
    existing saved messages without inventing missing content.

**Independent demonstration:** use deterministic upstream reduction strategies,
continue for another turn, persist, reload, and compare displayed messages and
the model's actual input independently.

### S3: Session lifecycle and readers remain consistent (P1)

**Acceptance criteria**

12. WHEN the history endpoint is paginated THEN its pages SHALL partition the
    preserved transcript in order, with `message_count` equal to transcript length.
13. WHEN local or attached TUI history is replayed THEN it SHALL expose the same
    preserved transcript as the history endpoint.
14. WHEN a valid checkpoint is rewound THEN replay SHALL omit messages after
    that checkpoint's conversation boundary.
15. WHEN that rewind is undone THEN replay SHALL restore the pre-rewind transcript.
16. WHEN a session is cleared or a new session is created THEN its display transcript SHALL be empty.
17. WHEN switching sessions succeeds THEN replay SHALL contain only the target
    session's transcript.
18. IF loading a target session fails THEN the active session's context and display transcript SHALL remain unchanged.
19. IF a session save fails before atomic replacement THEN the on-disk file SHALL retain its previous context and transcript together.
20. WHEN approval is pending or cancellation persists a resumable baseline THEN the saved transcript SHALL contain no messages beyond that committed baseline.
21. IF a persisted transcript is malformed THEN session loading SHALL raise
    `SessionLoadError` and HTTP history SHALL return 500 `unreadable`, rather
    than silently hiding the damaged transcript behind compacted context.
22. WHEN repeated or delayed persistence writes complete THEN the saved transcript and context SHALL describe the same committed generation without duplicate messages or a stale writer replacing a newer generation.

**Independent demonstration:** exercise HTTP pagination, TUI history adapters,
checkpoint rewind/undo, session reset/switch, and injected persistence failures
using temporary sessions; no live session edits.

## Traceability

| ID | Slice | Criteria | Status |
| --- | --- | --- | --- |
| TRANSCRIPT-01 | S1 | 1–5 | Builder proofs passed; independent verification pending |
| TRANSCRIPT-02 | S2 | 6–11 | Builder proofs passed; independent verification pending |
| TRANSCRIPT-03 | S3 | 12–22 | Builder proofs passed; independent verification pending |

## Observable

| Surface | Decision | Landing |
| --- | --- | --- |
| Mobile and TUI history | Empty state | AC 16; existing - no new empty-state copy |
| Mobile and TUI history | Loading state | existing - current fetch/replay indicators remain |
| Mobile and TUI history | Error state | AC 18, 21; existing - current error presentation |
| Mobile and TUI history | Unauthorized state | existing - bearer authentication and current client handling |
| Mobile and TUI history | Density and ordering | AC 6–8, 12–13; existing - unchanged message rendering |
| Mobile and TUI history | Destructive-action confirmation | existing - clear, delete, and rewind confirmation policies remain |
| History API | Response shape | AC 12; same envelope and message serialization, transcript-backed contents |
| History API | Error shape and codes | AC 21; existing - 400 bad_request, 401 unauthorized, 404 not_found, 500 unreadable |
| History API | Who may call | existing - workspace/session lookup and bearer authentication |
| History API | Versioning | existing - `/v1` route retained; no required new client field |
| History API | Rate limit | existing - no new endpoint-specific limiter |
| Transcript collection | Grouping and naming | existing - recorded message/part types; one transcript per session |
| Transcript collection | Ordering and duplicates | AC 6–8, 12, 22 |
| Transcript collection | Legacy or malformed exception | AC 11, 21 |
| Commands and scheduled tasks | New flags, output, exit codes | n/a - no command or scheduled task is added |
| Documentation | Reader's next action | existing - session/state guide explains context versus transcript and old-session limits |

## Flow

Keep upstream compaction strategies and the existing session writer, image
externalization, approvals, and checkpoint lifecycle. Marim owns the displayed
session transcript; it does not introduce another reducer. Upstream persistence
was evaluated but is not a drop-in immutable transcript archive for this session
format or its explicit between-run reductions.

1. CLI/native response enters `compaction.last_request_input_tokens` (exists):
   choose a current-context measurement, preserving aggregate usage separately.
2. `session/ctrl.py` (exists) captures newly committed conversation messages;
   `session/compaction.py` (exists) reduces only model context. An explicit
   reduction transition preserves the transcript before installing reduced context.
   `runtime/controller.py` (exists) uses that context-only transition for pre-turn
   history repairs too, since repairs can change part counts without new conversation.
3. `session/store.py` (exists) writes context and transcript in one atomic session
   snapshot (door 1), using the existing lock, generation, and media conventions.
4. `server/http.py` (exists) reads the transcript for `/history` (door 2);
   `interfaces/tui/link.py` and `interfaces/tui/app.py` (exist) expose it for replay.
5. `session/checkpoints.py` and `session/ctrl.py` (existing) preserve the corresponding
   display boundary through rewind/undo and reset or load both views together.
   Structural reduction keeps its existing checkpoint invalidation; tool-only
   clearing does not invalidate otherwise valid checkpoints.
   `session/history.py` (new) slices by part positions so upstream request-envelope
   normalization cannot shift archive or checkpoint boundaries.

## Relations

One session owns one model context and one display transcript. The transcript
records committed conversation messages; synthetic reductions belong only to
context. Both views share the same persistence generation and deletion lifecycle.
Checkpoint boundaries refer to the same conversation position in both views.

## Surface

| Route | In | Out | Status |
| --- | --- | --- | --- |
| `GET /v1/workspaces/{ws}/sessions/{sid}/history` | Existing workspace/session identifiers and offset/limit | Existing id, name, model, message_count, offset, history_seq, messages envelope; messages now come from preserved transcript | 200, 400, 401, 404, 500 |

Local history adapters retain their existing snapshot/message types. Model-facing
`SessionController.history` retains its meaning as context; display readers use
an explicit transcript view rather than changing what model callers receive.

Additive response field: `/history` may include `context_tokens`, estimated from
saved context, so attached TUI gauges do not estimate the larger display transcript.
The field is optional for clients; old-server fallback remains supported.

## Landing

| One-way door | Literal shape | Alternative rejected |
| --- | --- | --- |
| 1: Separate persisted transcript | Optional session JSON `"transcript": [...]`, serialized with the same message adapter as `"messages": [...]`; absent means legacy messages are the initial transcript | A separate sidecar needs cross-file crash recovery; repurposing messages would feed full archives to existing model consumers |
| 2: History API semantics | Existing `/v1/.../history` `messages` and `message_count` describe the display transcript; `history_seq` retains its existing resync watermark meaning | A second endpoint requires mobile changes and leaves existing clients on the lossy path |
| 3: Checkpoint display boundary | Optional checkpoint JSON `"transcript_len": <integer>` records the display position alongside `history_len`; absent legacy values use `history_len` | Inferring the position from cleared or summarized content is ambiguous and loses valid tool-only rewind points |
| 4: Context gauge with transcript replay | Optional history response `"context_tokens": <integer>` estimates saved model context; new remote clients prefer it and old-server replies fall back to transcript estimation | Estimating the archived transcript inflates the existing remote native-provider context gauge after compaction |
| 5: Normalization-stable checkpoint context boundary | Optional checkpoint JSON `"context_parts": <integer>` records context position independent of request-envelope merges; absent values retain legacy history_len behavior | A message index captured before upstream merges adjacent requests can rewind transcript correctly but leave later prompts in model context |

Keep both large arrays after the cheap metadata header. No new dependency or
bulk migration. Unknown-field preservation in metadata/job updates must retain
the transcript. Nothing else here is hard to reverse; internal boundary tracking
and naming remain implementation decisions.

The public `result.new_messages()` boundary was evaluated. It exists only after
a successful run, not the `capture_run_messages()` failure path; using it for the
archive would duplicate transcript rollback across retries, approvals, and cancellation.
The archive therefore tracks represented context parts, which survive upstream request
envelope normalization. Explicit context-only transitions rebase it for compaction,
load sanitation, and pre-turn repairs. No upstream private normalizer is imported.

## Impact

| Front | What changes |
| --- | --- |
| domain | Context remains the reduced model input. New explicit transcript is the recorded conversation used by HTTP and local/attached TUI replay |
| domain | History API previously reflected context; its display-oriented clients now receive the retained conversation. Model callers continue to use context |
| stored data | Add transcript on normal saves; old sessions start from available messages. No background rewrite, guessed recovery, or edits to live sessions during development |
| storage and privacy | Session JSON grows with retained messages; existing session deletion removes both views. Existing media lifetime and offload expiration remain unchanged; no resurrection of external payloads |
| upstream | Harness 0.31.0 continues to own reduction; Marim adds only transcript persistence/reader integration and measurement selection |
| verification | TLC checks will cover all nine sweep dimensions before build. Independent verifier runs after the feature's final implementation commit; validator must pass before completion |
