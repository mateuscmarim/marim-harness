# CLI backend parity roadmap

What marim and its CLI backends (`claude-cli`, `codex-cli`) could share that
they don't yet. Drafted 2026-09-13 against Claude Code 2.1.270 and
codex-cli 0.154.0. Like `ROADMAP.md`, this is direction, not a queue: phases
are ordered by payoff over cost, items can move, and anything marked
*verify* is a question to answer with one live handshake before building on
it.

## Why

Under a CLI backend marim is a launcher: the CLI runs its own tools, LSP and
MCP, while marim keeps the session, the approval panel, `ask_user`, steer,
interrupt and the sub-agent screen. That split leaves marim blind to things
the CLI knows and the CLI blind to things marim decides. Today's gaps line
up with limitations the docs already state (`/think` is a no-op under
claude-cli; the context gauge is an estimate; plan mode is enforced by
denying tools rather than telling the CLI it is planning). Both wires expose
the missing pieces already. This roadmap is about reading and sending what
is already there, not about extending either protocol.

## Ground truth

How the wire shapes below were established, so they can be re-checked when
either CLI moves:

- **Claude Code.** The stream-json schema lives inside the CLI's own ELF
  bundle. `grep -a -o` over `~/.local/share/claude/versions/<v>` for a
  subtype name shows the zod schema and its `.describe()` text. marim today
  sends `initialize`, `interrupt` and `can_use_tool` answers, and reads
  `system/init`, `stream_event`, `assistant` (tool_use only), `user`
  (tool_result) and `result` (`usage`, `total_cost_usd`, `session_id`).
  Every other `system` subtype is skipped in `consume_cli_stream`.
- **Codex.** `codex app-server generate-json-schema --out <dir>` writes the
  v2 protocol as JSON Schema (`codex_app_server_protocol.v2.schemas.json`).
  marim uses `initialize`, `thread/start|resume`, `turn/start|steer|interrupt`,
  `thread/compact/start`, `model/list`, `account/rateLimits/read`, the four
  approval and user-input requests, and the item, reasoning, usage and turn
  notifications. That is roughly a fifth of the surface.

Rules that apply to every phase:

- **Best-effort, never a failed turn.** Every new read is optional; a
  missing or malformed field degrades to today's behavior. Every new control
  request tolerates an error response (the CLI answers "not supported in
  this context" for callbacks it hasn't registered).
- **No `@internal` dependencies.** Claude marks some requests `@internal`
  (`get_workspace_diff`, `file_suggestions`, `rename_session`,
  `set_model.system_prompt`). Those can change without notice; marim reads
  only documented shapes.
- **Version-tolerant parsing.** New enum values and fields appear between
  CLI releases; parse leniently and log the unknown, never raise.
- **Serve parity.** Anything the local TUI shows from a backend must also
  ride the `marim serve` wire (`GET session` fields or a top-level event) so
  an attached TUI and marim-mobile see the same thing.
- **Persist what a resume needs.** A value the status bar shows before the
  first turn of a resumed session (context size, window) is stashed on the
  response's `provider_details`, next to the existing activity ledger.
- **Testable offline.** Claude events are replayed from recorded stream-json
  fixtures; Codex through the existing fake app-server. No live CLI in CI.

## Phase 1 — Context and quota reporting

**Status: shipped.** Each CLI adapter
keeps a `ContextReport(used, window)` (`config/context_report.py`) that
the status bar, the TUI link and `GET session` (`context` / `quota`) read;
it is persisted on the turn's `provider_details` so a resumed session shows
the backend's last reading cold. Claude polls `get_usage` once per turn for
a quota hint, and the cost double-count below is fixed by a `CostMeter`
that bills each turn the increase in `total_cost_usd` since the previous
result. The reported window feeds `ContextLimits` after a successful turn
and from persisted session state on resume, so compaction, masking and
overflow classification share the backend's real limit. Claude also polls
`get_context_usage` with `detail: summary` after each turn; its local category
estimates refine the passive request reading without extra token-count API
calls. Both status polls run concurrently and remain best-effort.

**Goal.** The status bar's `ctx used/max` and the quota hint mean the same
thing under a CLI backend as under a native provider.

**Before.** `ctx` was `estimate_tokens(history)` over marim's mirrored
history, divided by `compact_threshold`. Neither CLI provider has window
discovery in `build_context_limits`, so the denominator was the default
budget. The numerator missed the CLI's system prompt, `CLAUDE.md` or
`AGENTS.md`, MCP tool schemas and the CLI's own compaction. The codex quota
hint existed; Claude had none.

**Wire.**

| Backend | Where | What |
| --- | --- | --- |
| Claude | every `assistant` event, `message.usage` | prompt size of that request: `input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens` |
| Claude | `result.modelUsage[model]` | `contextWindow`, `maxOutputTokens`, per-model token and cost totals |
| Claude | control `get_usage` (`skip_behaviors: true`) | the plan's rate-limit windows, same shape of information as codex's quota |
| Claude | control `get_context_usage` (`detail: summary`) | the total and raw window from the same local category estimates as `/context`, without the token-count API calls made by `full` (verified on 2.1.271) |
| Codex | `thread/tokenUsage/updated` | `tokenUsage.modelContextWindow` (dropped today) and `last.inputTokens` as the current prompt size |

**marim seams.**

- Each CLI model adapter keeps a small `ContextReport(used, window, at)`
  refreshed per assistant event or usage notification, exposed the way
  `quota_hint` already is (the TUI link reads it with `getattr` off
  `harness.current_model`).
- `status_bar` prefers the report over the estimate when present, labels it
  as backend-reported, and denominates against the raw window rather than
  the 0.8 threshold: under a CLI backend the CLI's auto-compact governs, not
  marim's.
- `ContextLimits` records a discovered window from the report so
  `compact_threshold` stops riding on the default for these providers.
- The serve `GET session` payload carries the same pair; the remote link
  reads it.
- Claude gets a quota hint via a once-per-turn `get_usage`, mirroring the
  codex poll (and replaced by the push in phase 4 where one exists).
- Claude refines its prompt reading once per turn via `get_context_usage
  {detail: summary}`; malformed, unsupported or timed-out responses preserve
  the passive assistant-event reading.

**Side finding, confirmed and fixed.** The Claude binary's schema text says
`total_cost_usd` and `modelUsage` are cumulative across turns on the
streaming-input transport ("each result carries the running total so far,
so read the latest result rather than summing"), while `usage` is per-turn.
A three-turn live session on 2.1.270 showed `total_cost_usd` climbing
0.0109 → 0.0137 → 0.0162 while `usage` stayed per-turn, and
`request_usage_from_cli` fed every result's total into that turn's cost
detail — the ledger double-counted under the bidirectional claude-cli
provider. The fix is a per-turn delta against the previous result, the
same trick `codex/turn.py` already uses for token totals, reset whenever a
new process is launched.

**Acceptance.** Under both backends the gauge moves with the backend's own
numbers, drops after the backend compacts, and shows the real window. The
ledger's cost for a three-turn claude-cli session equals the CLI's own
final `total_cost_usd`.

## Phase 2 — Claude control parity

**Status: shipped** (unreleased at the time of writing). `claude/controls.py`
maps marim's vocabulary onto the wire values and keeps what the process last
acknowledged (`ControlState`); `ClaudeProcess.set_mode/set_model/
set_thinking` are the thin wrappers; the adapter's `_sync_controls` sends
only the deltas before each turn (skipped for aux clones); a same-provider
`/model` hands the live process to the new model object (`adopt`, called by
`Harness.set_model` before it closes the old one) so the switch is one
`set_model`. Two facts found while verifying the wire changed the plan
below: thinking has TWO levers — `set_max_thinking_tokens` for token-budget
models and `apply_flag_settings {settings: {effortLevel}}` for
adaptive-thinking ones (the Claude 5 family, Opus 4.6+, Sonnet 4.6, which
ignore the budget) — so a level sends both; and `system/status` is not
emitted on marim's headless transport, so the mode echo comes back in the
`set_permission_mode` response instead (`{"mode": ...}`).

**Goal.** marim's mode, model and thinking switches reach Claude Code
instead of being emulated or documented as no-ops.

**Wire** (control requests, all documented, none `@internal` in their
primary fields; verified on 2.1.270):

- `set_permission_mode {mode}` — answers `{"mode": <mode>}`; an unknown
  mode is a control error naming the valid set.
- `set_model {model}` — `null` or `"default"` resets to the session
  default; an unknown id is a control error (`Model 'x' not found`) and the
  previous model stays.
- `set_max_thinking_tokens {max_thinking_tokens, thinking_display}` —
  `null` resets to the session default; `0` disables; a positive budget is
  clamped to ≥ 1024. Ignored by adaptive-thinking models.
- `apply_flag_settings {settings: {effortLevel}}` — `low|medium|high|xhigh|
  max`, `null` resets. Ignored by models without effort; `get_settings`
  echoes the applied model + effort.

**marim seams.**

- `ClaudeProcess` gains `set_mode`, `set_model`, `set_thinking`, thin
  wrappers over `StreamJsonClient.control`. Each is sent on change and once
  after every (re)spawn so a resumed process starts in marim's state.
- Plan mode: keep the `can_use_tool` denial as the hard guarantee (marim's
  invariant is that plan mode cannot mutate) and add `set_permission_mode
  plan` so Claude plans instead of colliding with denials. `auto` maps to
  Claude's default mode with marim's broker still answering every
  `can_use_tool`; `ask` likewise. Never `bypassPermissions`.
- Model: `/model` under claude-cli becomes an in-place `set_model` instead
  of a respawn on `--resume`; the launch `--model` stays for the first
  spawn.
- Thinking: `TurnController._turn_model_settings` already resolves the
  level per turn; the claude-cli adapter maps it to a token budget plus an
  effort level and sends both when the level changes. Lift the "no-op under
  claude-cli" note from the docs and the `/think` UI annotation.

**Acceptance.** Switching mode, model or thinking level mid-session under
claude-cli takes effect on the next turn without a process restart, and
the `set_permission_mode` answer echoes the mode marim set.

## Phase 2b — Codex sub-agents

**Status: shipped.** Codex's collab tools now render, broker and persist as
first-class `spawn_agent` cards, matching the claude-cli Agent/Task demux.
Spec: [codex-collab-subagents.md](codex-collab-subagents.md).

**Wire.** A child is a separate thread on the same app-server, announced
with `thread/started` (`parentThreadId`, `agentNickname`, `agentRole`);
its items, deltas and approval requests carry its own `threadId`. The
parent's `collabAgentToolCall` item (`tool`, `senderThreadId`,
`receiverThreadIds`, `agentsStates`, `prompt`, `model`) is the spawn/
follow-up; `subAgentActivity` pings (`agentPath`, `agentThreadId`,
`kind`) mark agent starts/completions.

**marim seams.** `CodexServer.adopt_thread` registers a child under its
parent's handle (same queue, same request handler; dropping the parent
drops the children; a `thread/started` with a registered parent adopts on
the reader task so the child's first traffic is never dropped);
`codex/collab.py`'s `CollabRouter` turns a `spawnAgent` into a
`spawn_agent` `ActivityStart` on the parent's stream and routes each
child's translated items to its card; `ApprovalBroker.label_for` names
the agent on the panel; the main-loop model seals an open child in the
activity ledger at turn end (`running (detached; continues next turn)`)
and a spawn closes its children with its thread.

**Acceptance.** Shipped as the "Codex collab sub-agents" PR: a Codex
spawn shows as a `spawn_agent` card within the turn (streaming, model
badge, usage, notices); a child's approval opens the panel agent-labelled
under `ask` and is declined under `plan`; `GET …/history` carries the
spawn as a `spawn_agent` call + return; marim-mobile sees it via
`subagent.*` unchanged.

## Phase 3 — Backend lifecycle in the transcript

**Status: shipped.** Ordered backend notices share the live serve path and
durable transcript metadata.
See [capabilities and evidence](../reference/cli-lifecycle-capabilities.md) for
supported events, transport limits and the pinned protocol versions.

**Goal.** What the backend does between marim's turns (compaction, model
fallback, warnings, background work) is visible where a native provider's
equivalent already is.

**Wire.**

| Backend | Event | Use |
| --- | --- | --- |
| Claude | `system/compact_boundary` (`compact_metadata.trigger/pre_tokens/post_tokens`) | a compaction note in the transcript, and a context-report reset |
| Claude | `system/thinking_tokens` (`estimated_tokens`, delta) | the live token counter in the status bar |
| Claude | `system/session_state_changed` (`idle`, `running`, `requires_action`) | optional display observation only; result and approval ownership remain authoritative |
| Claude | `system/vcs_state_changed` (`kind`, `cwd`) | refresh checkpoint and git state after Claude commits, pushes, merges or rebases |
| Claude | `system/task_started`, `task_progress`, `task_notification` | backgrounded shells as cards next to the demuxed agents |
| Claude | `system/notification`, `permission_denied`, `model_refusal_fallback` (`model_fallback` is internal and excluded) | toasts |
| Claude | `result.permission_denials`, `num_turns`, `duration_api_ms`, `stop_reason` | stats ledger detail |
| Codex | completed `contextCompaction` item and deprecated `thread/compacted` | one completion note per occurrence; starting is not success |
| Codex | `model/rerouted`, `warning`, `configWarning`, `deprecationNotice`, `guardianWarning`, `error.willRetry` | toasts and transcript notices |

**marim seams.** `consume_cli_stream` grows a `NoticeChunk` (kind, text,
data) for system subtypes worth showing; `ItemTranslator` grows the same for
Codex. The TUI's existing `NoticeMessage` renders them; headless writes them
to stderr. The Claude `init` inventory (`tools`, `mcp_servers` with status,
`slash_commands`, `agents`, `permissionMode`) feeds marim's MCP panel and
`/` completion, so the user sees what the backend actually loaded.

**Acceptance.** A Claude auto-compaction or a Codex model reroute shows up in
the transcript and over the serve wire within the same turn.

## Phase 4 — Codex push, plan and diff

**Goal.** Replace polling with pushes where Codex offers them, and adopt
Codex's own plan and diff artifacts instead of re-deriving them.

**Wire.**

- `account/rateLimits/updated` — pushed; retires the per-turn
  `account/rateLimits/read` poll.
- `turn/diff/updated` — the turn's aggregate diff; feeds the diff card and
  the checkpoint summary without a second `git diff`.
- `turn/plan/updated` and `item/plan/delta` — Codex's plan streamed into
  marim's plan card when the session is in plan mode.
- `thread/name/set` and `thread/name/updated` — keep marim's autoname and
  the Codex thread title in sync in both directions.

**Acceptance.** No `account/rateLimits/read` calls in a normal session; a
plan-mode turn under codex-cli renders Codex's plan in the plan card.

## Phase 5 — Rewind and branching

**Goal.** `/rewind` under a CLI backend rewinds the conversation the backend
holds, not just marim's files.

**Today.** `CheckpointManager` restores files through `GitSnapshotter`. The
backend's transcript still believes the edits exist, so the next turn
reasons from stale tool results.

**Wire.**

- Codex: `thread/rollback` and `thread/revert` (conversation), `thread/fork`
  (branch a session).
- Claude: `rewind_files` only (file-level, tied to Claude's own
  checkpoints). No documented conversation-rewind request exists; the
  fallback is a respawn on `--resume` with marim re-sending a short
  "the following edits were reverted" note, which the tool-context bridge
  already knows how to phrase.

**Acceptance.** After `/rewind` under codex-cli, the next turn's context no
longer contains the reverted tool results (observable through phase 1's
context report dropping).

## Phase 6 — Sharing marim's workspace with the backend

**Goal.** What marim assembles for a native model (skills, per-turn context,
memory) reaches the CLI backend as first-class input rather than as prompt
prefix or not at all.

**Wire.**

- Codex `skills/extraRoots/set` — point Codex at marim's skill directories
  (workspace and plugin skills), so `/skill` in Codex sees the same set.
- Codex `thread/inject_items` — carry marim's `<turn-context>` (task
  checklist, finished-job digests, hook output) as proper items.
- Codex `skills/list`, `hooks/list`, `mcpServerStatus/list`, `plugin/list`
  — show what Codex loaded in marim's skills and MCP panels, the same way
  Claude's `init` inventory does in phase 3.
- Claude: skills and `CLAUDE.md` are read from disk by the CLI already;
  the remaining gap is marim's memory store, which reaches Claude only
  through the prompt prefix. Keep it there; no wire exists for more.

**Acceptance.** A workspace skill added in marim is listed by Codex without
a config edit; the turn-context prefix disappears from the prompt text under
codex-cli.

## Later / exploring

- **`review/start`** — Codex's built-in code review as a marim command.
- **`command/exec`** — run marim's own `bash` inside Codex's sandbox when a
  session is on codex-cli, for one consistent sandbox story.
- **`side_question`** — Claude's one-off question against the current
  context without a turn; a natural fit for the advisor seam.
- **`config/read` and `config/value/write`** — read Codex's defaults for the
  picker; write sandbox and approval settings once instead of per turn.
- **`externalAgentConfig/import`** — Codex's own importer for Claude
  configuration, adjacent to `marim import claude`.
- **Persist child transcripts for the main-loop CLI providers.** Claude's
  and Codex's sub-agents stream as cards but only a `backend:` spawn keeps
  the child transcripts (its sidecar `child_transcripts`); for the
  main-loop providers a `provider_details` sidecar keyed by stream id is
  the obvious shape, so the sub-agents screen can replay a child after a
  session resume.

## Non-goals

- **Re-implementing either CLI's tools in marim.** The launcher split stays:
  the CLI runs its tools, marim supplies policy and UI.
- **One app-server per session.** The module-level Codex singleton is
  deliberate; nothing here changes it.
- **Depending on `@internal` requests** or on undocumented event ordering.
- **Blocking a turn on any of this.** Every read and every control request
  is best-effort, and a backend that answers "not supported" leaves marim
  exactly where it is today.
