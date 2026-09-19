# CLI lifecycle capabilities

> Historical protocol evidence: Codex CLI execution is removed. Codex rows
> describe old records only; Claude lifecycle support remains active.

Phase 3 baseline: Claude Code **2.1.270**, Codex CLI **0.154.0**.
Protocol inspection: 2026-09-14. Fixtures are synthetic and derived from the
installed schemas; they are not recordings of paid live turns. No compaction,
commit or paid model turn was triggered for these checks.

## Evidence sources

- Claude public stream-json zod schema and emitting code in the installed
  `~/.local/share/claude/versions/2.1.270` binary, inspected with bounded
  `rg -a -o` searches for each subtype. No user transcripts or credentials.
- Codex `codex app-server generate-json-schema --out <scratch>` output,
  especially `ServerNotification.json` and v2 notification definitions.
- [Claude SDK commands](https://code.claude.com/docs/en/agent-sdk/slash-commands):
  discover session commands in init, invoke by sending their slash prompt.
- [Codex app-server](https://learn.chatgpt.com/docs/app-server): notification
  routing and public versioned protocol.
- Executable fixtures and assertions: `tests/test_claude_lifecycle.py`,
  `tests/test_codex_lifecycle.py`, `tests/test_lifecycle_delivery.py`,
  `tests/test_lifecycle_inventory.py`, `tests/test_lifecycle_stats.py`.

## Supported and conditional behavior

| Backend/event | Evidence and disposition | Retained fallback |
| --- | --- | --- |
| Claude `compact_boundary` | Public schema: nested `compact_metadata`, optional post count. Ordered completion notice and context replacement/invalidation. | No post count means unknown until next valid usage report. |
| Claude `notification`, `permission_denied` | Public text/message fields become notices. | Missing or malformed optional message ignored. Approval broker still owns asks. |
| Claude `model_refusal_fallback` | Public original/fallback model names, direction/scope. Child identifiers route through child stream. | No synthetic model switch or billing change. |
| Claude `model_fallback` | Schema explicitly `@internal`; excluded. | Public refusal fallback and normal model reporting remain. |
| Claude `system/status`, compact result/error | Earlier Phase 2 live probe found no emission on marim's headless transport. Not depended on. | `compact_boundary`, result and process failure paths. |
| Claude `thinking_tokens` | Public estimated_tokens; parsed if emitted. Not billed usage. | No estimate displayed when absent. |
| Claude `session_state_changed` | Public schema, but installed emitter conditional on CLI environment flag. Marim does not enable that flag. Opportunistic display only. | Existing result/closure/ask state machine; no fabricated completion. |
| Claude `vcs_state_changed` | Public kind/cwd hint. A new revision refreshes an existing attached `/worktree` output widget from the session workspace. Backend cwd never selects another workspace. | With no displayed local git view, only telemetry changes. No checkpoint creation/restoration; unavailable remote `/worktree` remains unavailable. |
| Claude background tasks | Public task_started/progress/notification with local_bash and stable task_id. One card updated through completion, requested interruption or unexpected failed exit. | Agent/Task demux remains separate; unidentified tasks ignored. |
| Claude init inventory | Public tools, mcp_servers, slash_commands, agents, permissionMode; replacement per process. Optional terminal_slash_commands schema excludes terminal-bound choices. | Older/malformed fields omitted; declared tools do not imply MCP connectivity. |
| Claude slash invocation | Official SDK documents slash_commands as headless invocable and normal prompt dispatch. Current public schema additionally names terminal-only subset. Exact declared nonterminal prompt forwarded. | Marim commands/aliases win; unknown commands and other backends are not forwarded. |
| Claude result details | Public permission_denials, num_turns, duration_api_ms, stop_reason. Only normalized fields stored in response/stats metadata. | Old ledger entries keep prior token/cost totals. Tool inputs are not copied. |
| Codex compaction | Public ContextCompaction item and deprecated ContextCompactedNotification. Complementary formats matched per turn; repeated item IDs ignored. | Unknown post count invalidates gauge. Start does not claim success. |
| Codex reroute | ModelReroutedNotification fromModel/toModel/reason, scoped by threadId/turnId. | Unknown reason string retained; stale turn ignored. |
| Codex warnings | WarningNotification.message, GuardianWarningNotification.message (`guardianWarning`), ConfigWarningNotification.summary, DeprecationNoticeNotification.summary. | Thread-scoped events never broadcast; global events fan out once per shared session queue on the same connection. |
| Codex retry | ErrorNotification.willRetry (camelCase), separate from terminal failure. | Retry displays notice and continues; failed terminal behavior unchanged. |

## Persistence and clients

`session.notice` carries message plus optional identity/backend/kind/severity/data.
Notices travel on the existing activity stream before turn completion. Headless
formats keep notices on supplied stderr; auxiliary model clones remain silent.

History uses blank `TextPart` markers with `provider_details.backend_notice`.
A notice between parallel tool results rides on the preceding tool return's
`metadata.backend_notices_after` list, preserving real tool responses. Neither
representation inserts notice prose into future model input. Clients deduplicate
by identity, not message text. Android reads all events through Room and its
existing `insertAndPrune` path. Backend compaction never emits marim's
`compaction.finished` and never clears the mirrored transcript.

Volatile inventory, thinking/state observations and shell cards describe the
current backend process. They are not replayed as executable tool calls. The
session detail snapshot and `session.backend_state` carry inventory/telemetry;
`backend.task` carries shell card updates. Android phase 3 scope is transcript
notices; inventory, stats and background-job screens remain separate work.

## Verification limits

Schema-backed support does not establish that every optional event will be
emitted by a particular account or headless CLI run. Public optional events are
handled when present; absent telemetry never blocks execution. Final check and
independent-verifier results are recorded in the feature's `.specs` directory.
