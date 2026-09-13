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

**Goal.** The status bar's `ctx used/max` and the quota hint mean the same
thing under a CLI backend as under a native provider.

**Today.** `ctx` is `estimate_tokens(history)` over marim's mirrored
history, divided by `compact_threshold`. Neither CLI provider has window
discovery in `build_context_limits`, so the denominator is the default
budget. The numerator misses the CLI's system prompt, `CLAUDE.md` or
`AGENTS.md`, MCP tool schemas and the CLI's own compaction. The codex quota
hint exists; Claude has none.

**Wire.**

| Backend | Where | What |
| --- | --- | --- |
| Claude | every `assistant` event, `message.usage` | prompt size of that request: `input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens` |
| Claude | `result.modelUsage[model]` | `contextWindow`, `maxOutputTokens`, per-model token and cost totals |
| Claude | control `get_usage` (`skip_behaviors: true`) | the plan's rate-limit windows, same shape of information as codex's quota |
| Claude | control `get_context_usage` (`detail: summary\|full`) | the per-category breakdown Claude's `/context` shows (*verify*: the handler answers "not supported" when its callback is unregistered; unknown on the headless transport) |
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
- `ContextLimits` accepts a discovered window from the report so
  `compact_threshold` stops riding on the default for these providers.
- The serve `GET session` payload carries the same pair; the remote link
  reads it.
- Claude gets a quota hint via a once-per-turn `get_usage`, mirroring the
  codex poll (and replaced by the push in phase 4 where one exists).

**Side finding to settle first.** The Claude binary's schema text says
`total_cost_usd` and `modelUsage` are cumulative across turns on the
streaming-input transport ("each result carries the running total so far,
so read the latest result rather than summing"), while `usage` is per-turn.
`request_usage_from_cli` feeds `total_cost_usd` into each turn's cost
detail, so the session ledger likely double-counts cost under the
bidirectional claude-cli provider. One multi-turn live session confirms it;
the fix is a per-turn delta against the previous result, the same trick
`codex/turn.py` already uses for token totals.

**Acceptance.** Under both backends the gauge moves with the backend's own
numbers, drops after the backend compacts, and shows the real window. The
ledger's cost for a three-turn claude-cli session equals the CLI's own
final `total_cost_usd`.

## Phase 2 — Claude control parity

**Goal.** marim's mode, model and thinking switches reach Claude Code
instead of being emulated or documented as no-ops.

**Wire** (control requests, all documented, none `@internal` in their
primary fields):

- `set_permission_mode {mode}` — Claude echoes the mode now in effect in
  `system/status.permissionMode`.
- `set_model {model}` — omitted or `"default"` resets to the session
  default.
- `set_max_thinking_tokens {max_thinking_tokens, thinking_display}` —
  `null` resets to the session default; `thinking_display` is
  `summarized` or `omitted`.

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
  level per turn; the claude-cli adapter maps it to a token budget and
  sends it when it changes. Lift the "no-op under claude-cli" note from the
  docs and the `/think` UI annotation.

**Acceptance.** Switching mode, model or thinking level mid-session under
claude-cli takes effect on the next turn without a process restart, and
`system/status` echoes the mode marim set.

## Phase 3 — Backend lifecycle in the transcript

**Goal.** What the backend does between marim's turns (compaction, model
fallback, warnings, background work) is visible where a native provider's
equivalent already is.

**Wire.**

| Backend | Event | Use |
| --- | --- | --- |
| Claude | `system/compact_boundary` (`trigger`, `pre_tokens`, `post_tokens`) and `system/status` (`compact_result`, `compact_error`) | a compaction note in the transcript, and a context-report reset |
| Claude | `system/thinking_tokens` (`estimated_tokens`, delta) | the live token counter in the status bar |
| Claude | `system/session_state_changed` (`idle`, `running`, `requires_action`) | the binary calls this the authoritative turn-over signal; use it to harden the turn lifecycle alongside `result` |
| Claude | `system/vcs_state_changed` (`kind`, `cwd`) | refresh checkpoint and git state after Claude commits, pushes, merges or rebases |
| Claude | `system/task_started`, `task_progress`, `task_notification` | backgrounded shells as cards next to the demuxed agents |
| Claude | `system/notification`, `permission_denied`, `model_fallback`, `model_refusal_fallback` | toasts |
| Claude | `result.permission_denials`, `num_turns`, `duration_api_ms`, `stop_reason` | stats ledger detail |
| Codex | `thread/compacted` | compaction note |
| Codex | `model/rerouted`, `warning`, `configWarning`, `deprecationNotice`, `guardianWarning`, `error.will_retry` | toasts and transcript notices |

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

## Non-goals

- **Re-implementing either CLI's tools in marim.** The launcher split stays:
  the CLI runs its tools, marim supplies policy and UI.
- **One app-server per session.** The module-level Codex singleton is
  deliberate; nothing here changes it.
- **Depending on `@internal` requests** or on undocumented event ordering.
- **Blocking a turn on any of this.** Every read and every control request
  is best-effort, and a backend that answers "not supported" leaves marim
  exactly where it is today.
