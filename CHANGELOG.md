# Changelog

All notable changes to marim-harness are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/) —
pre-1.0, minor versions may contain breaking changes.

## [Unreleased]

### Added

- **Claude CLI tier routing.** A sub-agent model tier can now name the
  reserved `claude-cli:<model>` execution target: an ordinary native role
  (`explore`, `general`, a custom agent) resolving to that tier transparently
  runs through your Claude subscription via the `claude` CLI — same
  approvals, lifecycle, and resume behavior as an explicitly authored
  `backend: claude-cli` spec — without naming a Claude-specific agent type.
  An explicit `backend:` declaration always keeps its own precedence; a
  selected Claude target never falls back to a native/API model on failure.

### Changed

- **A sub-agent that hits its request budget now wraps up instead of failing.**
  `MARIM_SUBAGENT_REQUEST_LIMIT` is a runaway guard, but reaching it used to
  discard the whole run: the spawner got `UsageLimitExceeded` and every finding
  the sub-agent had collected was lost. A run that reaches the cap now has its
  tools withheld and is asked for one final report from the work so far; that
  report is returned prefixed with a `[note: …]` saying the budget ran out and
  the report may be incomplete. Only a spawn that cannot produce the report
  (keeps calling tools) still fails. The default rose from 50 to 200 — the
  guard only has to sit above honest work, and read-only investigations
  routinely need more than 50 requests — and `0` now means unbounded (it used
  to be rejected and fall back to the default). `RetryPolicy.request_limit`
  and `HarnessConfig.subagent_request_limit` follow the same rules.

### Fixed

- **`marim update` no longer reports an upgrade it did not deliver.** A uv
  tool installed from a local wheel path keeps that path as its install
  source, so `uv tool upgrade` exits 0 with "Nothing to upgrade" and the old
  version stays — and the command printed "Upgraded to 0.15.0" over a binary
  still at 0.14.0. The installed version is now read back from `uv tool list`:
  an upgrade that stops short of the latest version is retried as a forced
  reinstall by name from PyPI (extras preserved, the same recovery as a stale
  source), and the success line is printed only when the tool actually
  reached the latest version; otherwise the command says which version is
  still installed, prints the reinstall command, and exits 1.

## [0.15.0] - 2026-09-19

### Added

- **Native Codex subscription provider.** `MARIM_PROVIDER=openai-codex` (or an
  `openai-codex:<model>` session) runs Marim's own agent on a Codex subscription
  through Pydantic AI, reusing `codex login` credentials: Marim's native tools,
  approvals, MCP, sub-agents, advisor and context management all apply, unlike
  the launcher-style CLI backend it replaces. The model must be named explicitly
  and there is no fallback to API billing — missing or rejected credentials fail
  the request with login guidance. Subscription token counts are recorded while
  the monetary cost stays unknown. See `docs/guides/codex-subscription-evaluation.md`.

### Changed

- **Skill activation is exempt from the 10k tool-output spill.** A SKILL.md
  body is instructions to follow, not data to page through, and real skills
  run 17-35k characters — under the general policy `activate_skill` returned a
  1k preview plus a read-back handle, so the model acted on a fragment. Its
  result (directory header plus body) now passes through whole below 60,000
  characters (a per-tool band list on the upstream `ToolOutputLimits`,
  `SKILL_PASSTHROUGH_CHARS`) and spills at or above that. `read_skill_file` keeps the general threshold.
- **Job waits hold through completion and yield to steering.** `wait_for_job`
  and `job(action="wait")` no longer default to a 60-second timeout: one call
  blocks until the job finishes (an explicit `timeout` still bounds it). A
  steer sent while the model is waiting releases the wait — the job keeps
  running, the tool returns a truthful "still running — wait released" note,
  and the steer reaches the model in that same request. Only a wait that
  delivered the result marks the job wake-consumed, so a released or timed-out
  wait still gets the autonomous wake; cancelling the turn still cancels only
  the waiter. `JobRegistry.wait_outcome`/`release_waits` and
  `TurnController.has_undelivered_steer` are the seams. The TUI renders a
  pending wait as `Waiting for <job> · <elapsed>` (actual blocked time, not
  the requested timeout) for both tool variants; the combined `job` tool's
  rows are labelled by action.

### Removed

- **Codex CLI executor.** Native `openai-codex` now supplies Codex subscription
  access. Removed the app-server transport, CLI model provider, CLI child backend,
  and their configuration/examples. Old transcripts remain readable; selecting
  or resuming the retired backend reports migration guidance without provider fallback.

### Fixed

- **Nested sub-agent spawns no longer deadlock on the concurrency cap.** A
  foreground child could wait forever when its ancestors held every
  `MARIM_SUBAGENT_CONCURRENCY` slot (including a single parent at concurrency
  one). The cap now bounds *model requests* through Pydantic AI's shared
  `ConcurrencyLimiter`, releasing capacity while a tool awaits its children;
  external CLI runs keep one slot for their whole run from the same pool. The
  spawn-wide semaphore is gone; the workflow-abort admission check stays.
- **`read_file` buffers a bounded amount before clipping.** Text reads streamed
  through `TextIOWrapper` iteration, which allocates an entire physical line
  before yielding it, so one minified line ballooned memory regardless of the
  clip. Reads now decode fixed-size byte chunks and retain only the clipped
  prefixes and rendered rows; pagination and the clipping footer keep their
  meaning.
- **An untrusted project plugin no longer shadows a global one of the same
  name.** Discovery falls through the untrusted project record to the eligible
  global plugin (skills, hooks, MCP and LSP contributions alike) while still
  reporting the project plugin's status; a trusted project keeps its local
  override.

## [0.14.0] - 2026-09-17

### Added

- **Server version in health responses.** `GET /v1/health` reports the running
  Marim package version, captured when the daemon starts, so clients can display
  which version each server is running. Unavailable package metadata is reported
  as `null`; the endpoint remains unauthenticated and backward compatible.

## [0.13.0] - 2026-09-17

### Removed

- **Deprecated Advisor import aliases.** The `marim_harness.capabilities`
  package has been removed after its 0.12.0 compatibility release. Import
  `Advisor` directly with `from pydantic_ai_harness import Advisor` instead.
  Runtime advisor selection and `HarnessBuilder.with_capability` are unchanged.
- **Superseded compatibility shims.** Token-estimation callers now import
  `estimate_token_count` from `pydantic_ai_harness.compaction`; tool constants
  come from `marim_harness.tools.names`. Context discovery accepts `fetchers=`
  instead of `fetch_catalog=` or `fetch_local=`. Internal `_TextFolder`,
  `_default_base_dir`, and CLI `run` aliases are removed in favor of `TextFolder`,
  `default_sessions_base`, and `main` in their owning modules.
- **Expired masking-setting warning.** `MARIM_MASK_MIN_CHARS` remains ignored,
  but no longer emits a compatibility warning or appears in configuration docs.
  Saved-session readers and `MARIM_MAX_CONTEXT_TOKENS` support are unchanged.

### Fixed

- **`marim update` recovers from a stale uv tool install source.** When
  marim-harness was installed from a local wheel path that no longer exists
  on disk (a dev build, a release scratchpad artifact), `uv tool upgrade`
  kept failing by reusing that stale path instead of resolving from PyPI.
  `marim update` now retries with a forced reinstall by package name — for a
  known uv tool install only, preserving its extras — before falling back to
  `pip`.

## [0.12.0] - 2026-09-17

### Changed

- **Advisor uses Pydantic AI Harness.** Calls now require `advisor(prompt=...)`;
  completed history is forwarded and nested usage shares turn limits. The use
  cap is per model request, output limits require at least 1024 tokens, model
  switches apply next turn, and provider errors propagate. Runtime routing
  preserves Marim's configured clients through local execution; SDK imports
  directly alias upstream Advisor for one release, with upstream defaults and
  options. Historical advisor messages remain readable. Advisor uses the existing
  base Harness dependency; workflows/Monty remain optional.

### Fixed

- **Compaction preserves the displayed conversation.** Local, attached, and
  mobile history replay uses a persisted transcript separate from reduced model
  context. Existing sessions retain their available messages; content discarded
  by older releases cannot be recovered.
- **Claude cancellation works before streaming starts.** Stopping a turn while
  waiting for its first response interrupts the CLI so the next request does not
  inherit an abandoned turn.
- **Advisor costs remain cumulative after session resume.** Mixed-model cost
  estimates retain pre-resume usage; unknown costs remain unknown.

## [0.11.1] - 2026-09-17

### Added

- **Trusted Codex embedding configuration.** Embedders can supply explicit,
  validated CLI configuration overrides without relying on project configuration.

### Fixed

- **External CLI models start in the configured SDK workspace.** The builder
  binds the workspace, live permission mode, and session hooks before returning,
  so the first turn works without attaching a UI or manually wiring the model.
- **Claude structured output stays separate from progress.** Native JSON Schema
  output uses the terminal payload for validation, preserves tool callbacks, and
  forwards validation feedback on retries. Schema changes and model switches keep
  the CLI process configured for the requested output, including plain text.
- **Codex structured output works in embedded turns.** Output schemas and
  corrective feedback reach the CLI, progress text stays out of validated output,
  and failed turns retain their usage for embedding callers.
- **Provider diagnostic writes cannot follow workspace symlinks.** Atomic writes
  use no-follow directory descriptors, and diagnostic failures preserve the
  original provider error.

## [0.11.0] - 2026-09-16

### Added

- **Safe retries for `marim serve` mutations.** Compatible clients can reuse
  an operation ID on protected routes to recover a lost response without
  repeating a message, approval, or configuration change. Durable claims and
  saved responses survive daemon restarts; unresolved claims prevent duplicate
  execution. Every attempt requires authentication, and ledger I/O runs off the
  event loop. Existing clients and routes keep their current behavior.

### Changed

- **Dynamic workflows use Pydantic AI Harness.** Scripts now use
  `run_workflow(code=...)` and named worker functions such as
  `explore(task=...)`. Host bindings supply fixed schemas and isolation;
  injected `args`, `log`, and per-call worker/timeout overrides are removed.
  The wall deadline defaults to 1800 seconds with a separate 30-second sandbox
  compute cap. Deep research uses typed research and verification bindings.
  Historical workflow previews remain readable; pending legacy calls receive
  migration guidance. The Monty upgrade and engine replacement ship together.

### Fixed

- **Interrupted Claude workers retain their latest partial response.** Final
  cleanup checkpoints the transcript and resume ID, including when no next
  stream event arrives; checkpoint failures still allow process cleanup.

## [0.10.0] - 2026-09-15

### Changed

- **Ordinary tool-output limits now use Pydantic AI Harness.** Large built-in,
  custom, skill, and MCP results share session-owned storage, head-and-tail
  previews, and the `read_tool_result` retrieval tool. Supported media stays
  intact; native sub-agents use the same policy, and legacy saved pointers
  remain readable. Requires Pydantic AI 2.43 or newer and Harness 0.31.0.
- **Compaction and native sub-agent history clearing now use Pydantic AI
  Harness.** Upstream strategies own safe cutoffs, summary messages, fallback
  trimming, and stale tool-result clearing. Recent-pair retention is approximate;
  clearing creates no new scratchpad copies, while existing offload handles and
  saved pointers remain readable. Summary model usage is recorded, and advanced
  embedders configure `compaction_strategy` instead of the former unstable
  `summarizer=` callback. `MARIM_MASK_MIN_CHARS` is accepted for one release but
  ignored with a deprecation warning.

### Fixed

- **Direct shell displays stay bounded.** TUI `!` commands collect a 4 KB
  head-and-tail preview; background-shell output shown locally or through HTTP
  uses a 20,000-character preview. Exit status and final output remain visible,
  and background jobs retain their collected result for agent retrieval.
- **Resumed Claude and Codex sub-agents become active again.** Reused agents
  are announced and their cards reactivate, including nested agents and cards
  whose previous turn was already pruned.

## [0.9.1] - 2026-09-15

### Fixed

- **Codex resume avoids deprecated full-history hydration.** Resume requests
  now exclude historical turns from the response, preventing the pagination
  deprecation warning while preserving Codex's conversation context.

## [0.9.0] - 2026-09-14

### Added

- **CLI backend lifecycle events are visible everywhere.** Claude Code and
  Codex compaction, rerouting, retry, warning and background-task events now
  appear in the live TUI, attached clients and headless stderr in backend
  order. Display-only notices survive transcript replay without entering the
  model-facing conversation; backend inventory, verified Claude commands,
  thinking/state observations and normalized result telemetry use the same
  session surfaces.
- **CLI-native agents appear in Background Tasks.** Claude and Codex agents
  are mirrored as backend-owned jobs even when they finish between parent
  turns. Terminal outcomes persist through ordered, session-bound snapshots;
  the backend continues to own execution, cancellation and result delivery.
- **Authenticated workspace file downloads.** `marim serve` exposes a
  session-scoped file endpoint for transcript links, allowing attached clients
  to preview, save and share regular workspace files. Descriptor-anchored
  traversal rejects escapes and symlinks, streams at most 50 MiB, and closes
  resources on disconnect or cancellation.

### Changed

- **CLI context limits follow the backend's actual window.** A successful
  Claude or Codex turn teaches compaction, masking and overflow handling the
  backend-reported context window, and resumed sessions restore it before the
  first turn. Claude also polls the public `get_context_usage` summary beside
  its quota request to refine the total shown by the context gauge without an
  extra token-count API call. Status polling remains concurrent and
  best-effort, and a saved window is ignored after a persisted model switch.

## [0.8.0] - 2026-09-14

### Added

- **`codex-cli`: Codex's own sub-agents are first-class cards.** When
  Codex spawns an agent with its collab tools (`spawn_agent`, `send_input`,
  `wait`, `close_agent`), marim now renders it as a `spawn_agent` card in
  the sub-agents screen — type `codex-agent`, a `codex-cli:<model>` badge,
  the child's text, tool calls and usage streaming in, collab follow-ups
  as notices on the card, settled with the child's last message — the
  Codex counterpart of the `claude-cli` Agent/Task demux. The child's
  approval requests go through marim's panel labelled with the agent's
  name (`agent scout`; declined without a prompt under `plan`). The spawn
  persists with the turn as an ordinary `spawn_agent` tool call + return
  (an agent still running at the end of a turn is sealed with a `running
  (detached; continues next turn)` return and picked up by the turn that
  collects it), reaches attached clients as the `subagent.*` events, and
  works inside `backend: codex-cli` spawns too (nested under the spawn's
  card, with each child's transcript persisted in its own sidecar). The
  opaque `codex_agent` tool card that stood in for a collab call before is
  gone; histories written with it still expand.
- **`/mode`, `/model` and `/think` reach Claude Code.** Under the
  `claude-cli` main provider the three switches are now sent to the live
  process as control requests before the next turn instead of being
  emulated or documented as no-ops: `plan` runs Claude in its own plan mode
  (`set_permission_mode`; marim's broker keeps denying every mutating tool
  on top, and Claude's `ExitPlanMode` is answered with "the user switches
  with `/mode`"), `auto`/`ask` run it in Claude's `default` mode where it
  keeps asking marim; a same-provider `/model` switch is one `set_model` on
  the process you already have (no close + `--resume` respawn, the context
  and quota readings carry over) and a rejected id fails that turn with the
  CLI's message instead of running on the wrong model; `/think` is sent as
  a thinking-token budget plus an effort level (`set_max_thinking_tokens` +
  `apply_flag_settings {effortLevel}`), so both token-budget and
  adaptive-thinking Claude models honour it (the latter cannot switch
  thinking off, so `off` is their lowest effort). Each is sent once per process
  and again only when it changes; the model picker's claude-cli entries are
  now annotated as thinking-capable.
- **The CLI backends report their own context.** Under `claude-cli` and
  `codex-cli` the status bar's `ctx` field now shows the backend's real
  numbers — the prompt size of its most recent model request (system
  prompt, `CLAUDE.md`/`AGENTS.md` and tool schemas included, as the CLI's
  own `/context` counts them) over the model's context window — instead of
  a chars/4 estimate over marim's mirrored history against marim's default
  budget. Claude reports it on every `assistant` event and names the window
  at the turn's `result`; Codex on `thread/tokenUsage/updated`. The reading
  persists with the turn so a resumed session shows the backend's last
  known context before its first new turn, and `GET .../sessions/{sid}`
  carries it as `context` (`{"used", "window"}`) with the rendered quota
  hint as `quota`; the attached TUI reads both over the wire.
- **`claude-cli` quota hint.** After each turn marim asks the CLI for its
  rate limits (`get_usage`) once and shows the five-hour and seven-day
  windows as `quota 11% (5h) · 59% (1w)`, the way `codex-cli` already did.

- **`HarnessBuilder.with_usage_limits(request_limit=, total_tokens_limit=)`**
  — a per-turn budget for the main agent. The cap spans the whole turn
  (every approval round counts against the same budget, not one `agent.run`
  round), so a model that keeps calling a gated tool cannot outrun it by
  getting approved. Tripping it raises pydantic-ai's `UsageLimitExceeded`
  out of `run_turn` with the spend up to that point still banked on
  `session.usage`. Unset limits are unbounded (pydantic-ai's own
  `request_limit` default of 50 does not leak through).
- **`HarnessBuilder.with_files_write(enabled)`** — `with_files_write(False)`
  builds a read-only harness: neither `write_file` nor `edit_file` is
  registered (nor the scratchpad prompt paragraph that advertises them), and
  a sub-agent granting either is a `build()` error like any other disabled
  group.
- **`TurnOutcome.usage`** — a `RunUsage` for this turn summed across
  approval rounds. `session.usage` stays the cumulative total across turns.

### Changed

- **Breaking (SDK): a bare `HarnessBuilder` build no longer injects the
  workspace's `AGENTS.md`/`CLAUDE.md` into the system prompt.** Project
  instructions were read unconditionally, which for an embedder whose
  workspace is an untrusted checkout (a review bot over a contributor's
  branch) was a straight prompt-injection path into the model. They are
  now opt-in via `with_instructions(project=True)`; `with_defaults()` — and
  therefore the CLI — turns them on, and `with_instructions(project=False)`
  after it turns them off. Embedders that relied on the old behaviour add
  the one call.

### Fixed

- **`codex-cli`: a blank `MARIM_MODEL` no longer fails every turn.** With
  `MARIM_MODEL=` set but empty, `CodexCliModel` sent the app-server
  `model: ""` on `thread/start` / `turn/start` and Codex answered 400 *"The
  '' model is not supported"*. An empty id now means "Codex's own default",
  exactly like an unset one.
- **`claude-cli`: a background Agent's report was lost and its card spun
  forever.** Claude Code runs its Agent sub-agents in the background: the
  spawning turn ends at launch, and when the agent finishes while no turn
  is open the CLI reacts on its own — it injects the report into its
  history and runs a model turn nobody asked for. marim dropped every
  object that arrived with no turn open, so the spawn card never settled,
  the reaction was never shown, and the two histories diverged (Claude
  would later insist it had "posted the summary above"). The process now
  buffers that turn and marim plays it as an autonomous turn as soon as the
  session is idle: the card settles, the reaction renders as its own
  transcript entry, and nothing is re-sent to the CLI. A typed turn
  submitted first goes out first (the CLI's own turn is let finish before
  marim sends one), a report the CLI never reacts to still settles its card
  at the start of the next turn, the idle reaper holds (bounded) while a
  background agent runs, and a resumed history records the unanswered spawn as
  "reports later" rather than as an interrupted call.
- **`claude-cli`: a background Agent inside a spawn or a headless run died
  with the process.** A `backend: claude-cli` spawn closed its `claude`
  process when its turn ended and `marim -p` exited with the turn, so an
  Agent that spawn or run had launched in the background was killed before
  it reported — the spawn's report said "I kicked off an agent" and ended,
  and its card spun forever. Both now wait the agent out (bounded by
  `MARIM_CLAUDE_CLI_TIMEOUT`, `ClaudeProcess.wait_background`): the spawn
  consumes Claude's reaction turn as part of the run, so its report is what
  Claude has to say once the agent is done (the last result, usage summed)
  and a report Claude never reacts to still settles the card; headless
  plays the reaction as an autonomous turn (`Harness.wait_backend_turn`,
  also for embedders running one turn at a time) and prints its text after
  the turn's own. A reaction that goes silent is interrupted and dropped —
  the turn's own result stands.
- **Switching sessions under `claude-cli`/`codex-cli` kept driving the old
  conversation.** A session switch, `/new` or `/clear` rebound marim's
  session store but left the adapter's live `claude` process or codex thread
  in place, so the next prompt continued the conversation the user had just
  left — and the CLI's reply persisted the old conversation's id over the new
  session's ref. The harness now tells the model to release its provider-side
  conversation at every store rebind (`ExternalCliModel.release_conversation`),
  so the next turn resumes what the incoming session recorded, or starts
  cold.
- **A bare-id `/model` switch orphaned the CLI thread ref.** `/model sonnet`
  under a `claude-cli` (or `codex-cli`) default provider carries no provider
  prefix, and the session read its first segment as the provider — so every
  same-provider switch cleared the persisted thread ref and the next resume
  started cold. The harness now tells the session which provider the new
  model runs on.
- **`claude-cli` cost was double-counted in the usage ledger.** On the
  bidirectional transport every `result` carries the process's *running*
  `total_cost_usd`, not the turn's cost, and marim billed each turn the
  whole total — a three-turn session was charged roughly six turns. Each
  turn now bills the increase since the previous result, and the baseline
  resets whenever a new `claude` process is launched (a fresh or resumed
  session starts its total at zero).

## [0.7.2] - 2026-09-13

### Fixed

- **CLI providers' tool calls missing from persisted history.** Under
  `claude-cli` and `codex-cli` the CLI's tool calls streamed live (as
  `tool.call` / `tool.result` since 0.7.1) but were never written to the
  session, so a resumed TUI transcript, `GET .../history` and every client
  rebuilding from history (marim-mobile) showed the prose with holes where
  the tool cards had been. The streamed response now records the activity in
  an ordered ledger and the turn controller expands it at persist time into
  real tool-call / tool-return messages — the same shape marim's own tools
  leave, so replay, compaction and a mid-session provider switch all see
  them. Results are capped at 16k characters in the persisted copy; a call
  the CLI never answered (interrupted mid-tool) persists with an
  `interrupted` return so the history stays resumable.

### Changed

- **`claude-cli` model catalog is live.** The picker (and `GET /v1/models`,
  `marim models list`) used a hard-coded `sonnet`/`opus`/`haiku` list for the
  `claude-cli` provider, which had already fallen behind the CLI (no `fable`,
  no 1M-context variants). The catalog is now the CLI's own `/model` menu read
  from its stream-json `initialize` handshake — filtered by your plan and org
  allowlist, each row naming the concrete model an alias resolves to — cached
  from any running claude-cli session or fetched by a one-off handshake-only
  probe (no session file, ~1 s). The old aliases (plus `fable`) remain the
  fallback when the CLI cannot be launched.

## [0.7.1] - 2026-09-13

### Fixed

- **CLI providers' tool calls missing from API clients.** Under `claude-cli`
  and `codex-cli` the CLI runs its own tools, and the daemon published those
  calls inside a TUI-only `subagent.cli_activity` envelope that other clients
  (marim-mobile) dropped, so their transcripts showed no tool calls at all.
  The daemon now publishes them as the same top-level `tool.call` /
  `tool.result` events a native tool produces; `subagent.cli_activity` is
  gone from the wire vocabulary.

## [0.7.0] - 2026-09-13

### Added

- **Jobs over the wire (attached TUI).** A TUI attached to a daemon-owned
  session now sees and acts on the daemon's background jobs: `GET
  .../jobs` seeds the jobs panel and is re-read on every `jobs.changed`,
  the daemon's list decides whether a replayed sub-agent card is still
  running or finished (a spawn that settles while attached fills its card
  with the full result via `GET jobs/{id}`), `/jobs` (list, `output`,
  `cancel`) and `r` on an interrupted spawn go through the new `POST
  .../jobs/{id}/cancel` and `POST .../subagents/{stream_id}/resume`
  routes, and only `/jobs wake` stays refused (autonomous wake is the
  daemon's). The list DTO gains `result_tail` (`null` while running) and a
  cold session's `GET jobs` returns the settled history its file carries.
  Phase 4b of the cross-process session-event design.
- `codex-cli` provider: `MARIM_PROVIDER=codex-cli` delegates each turn to
  `codex app-server` (JSON-RPC over stdio) on a ChatGPT/Codex subscription,
  with approvals brokered through marim's own panel, thinking levels mapped
  to reasoning effort, `/steer` and `/compact` forwarded to the thread, and
  the thread id persisted with the session for resume.
- `backend: codex-cli` sub-agents: one Codex thread per spawn on the shared
  app-server, read-only sandbox unless the agent has a mutating tool, native
  `outputSchema` enforcement, resumable via the persisted thread id.
- Settings > Providers shows a `codex-cli` card (binary + `codex login`
  detection) and the model picker lists Codex models from `model/list`.
- The shared `codex app-server` is launched isolated from the user's own
  Codex setup: every MCP server in their Codex config is disabled by name
  (`codex mcp list` enumerates them), and plugins and the built-in apps
  connector are switched off, so a marim thread runs with marim's tool
  reach only.
- Tiered CLI worker examples: `docs/examples/agents/{claude,codex}-{fast,general,deep}.md`
  (haiku/sonnet/opus and gpt-5.6-luna/terra/sol) with a "Tiered CLI workers"
  section in the sub-agents guide; parsed in CI.

- **Attach the TUI to a daemon-owned session.** `marim --session <id>` opens
  a specific saved session; when a running `marim serve` daemon owns it (a
  reachable endpoint, a matching daemon pid, a readable token, a registered
  workspace), the TUI attaches over the daemon's REST + WebSocket API
  instead of refusing: `GET session` seeds the status bar, `GET history`
  replays the transcript, the socket streams the live tail from the
  persisted boundary, and submit/interrupt/steer/asks/mode/model go
  through the routes. Reconnects with backoff (`daemon · reconnecting…`),
  gives up after 60 s or an auth/not-found rejection (`daemon · lost`),
  and resyncs from history on `stream.gap`. Commands that need the
  session's own process (`/clear`, `/new`, `/compact`, `/rewind`, `/name`, `/switch`, `/skill`, `/mcp`, `/jobs`, `/worktree`, `/plugin`, `/trust`, `/advisor`, `/think`, `!`, image steers, in-place session switch) are refused
  with a notice. The session picker tags sessions other processes hold. Phase 4a
  of the cross-process session-event design; the `[tui]` extra now pulls
  `websockets`.
- `POST .../messages` accepts `trigger: "user" | "system"` (a slash
  command's own prompt renders without a user bubble; `autonomous` is
  refused), `tool.result` events carry `images` references resolvable at
  `GET .../images/{sha}` the moment they are published, and `GET session`
  reports the loaded host's live `mode`/`model_label`/`advisor_model`/
  `thinking` plus `workspace_path`, cumulative `usage` and
  `compact_threshold` (both `null` while cold).
- `turn.started` carries a `trigger` (`user` | `system` | `autonomous`) so a
  client can tell a typed prompt from a slash command's own prompt or an
  autonomous wake, and `steer.accepted` carries the attachment count.
  `SessionHost.submit(..., trigger=)`, `SessionHost.steer(text, attachments)`
  and `SessionHost.stop()` (interrupt + stop the worker without the daemon's
  full teardown) are the host-side seams behind them.

### Changed

- **TUI turns run on the host and complete from the wire.** The TUI no longer
  awaits a turn: every submit (typed, drained from the queue, `/remember`,
  wake) goes through `SessionHost.submit()`, and the turn's end reaches the
  app as `turn.finished` / `turn.error` / `session.status` events — the
  duration stamp, error card, cancelled marker, queue drain and wake all
  hang off those handlers (`interfaces/tui/turn_state.py` folds the submit
  latch and the host's status into the busy flag). Esc calls
  `host.interrupt()`; steers go through `host.steer()`. An ask this client
  is showing that another client answers is dismissed with a notice
  ("Approval granted from another client"). No behavior change intended for
  a single local user; phase 3b of the cross-process session-event design.
- **The TUI renders from the event bus.** `HarnessApp` now hosts an in-process
  `SessionHost` — the same one `marim serve` runs — and paints the transcript
  from the typed wire events it publishes (`text.delta`, `tool.call`,
  `ask.pending`, …) through a single pump task, instead of consuming
  `Harness.bind_ui` callbacks directly. `SessionHost` is the sole `bind_ui`
  consumer; approvals, `ask_user` and plan cards park as asks and resolve
  through `ask.resolved`, so a remote answer dismisses the local panel. No
  behavior change intended for the user; it is the seam the cross-process
  session-event design builds on. New `turn.usage` wire event carries the
  run's running token total (published on change) for the live `+N` counter.
- **OpenCode (`zen`/`zen-go`) requests identify themselves.** The gateway now
  requires a stable `x-opencode-session` header and a self-identifying
  `User-Agent`; marim sends one session id per process plus
  `marim-harness/<version>`.
- **claude-cli is bidirectional.** The `claude-cli` provider and `backend: claude-cli`
  sub-agents now keep one long-lived `claude` process per conversation over its
  stream-json control protocol instead of launching `claude -p` per turn. marim's
  `auto`/`ask`/`plan` modes, the approval panel and `ask_user` now apply to Claude's
  tool calls (`--permission-mode` is no longer passed); steer folds into the live turn;
  Esc in the TUI (Ctrl-C headless) sends an interrupt (kill after a 2 s grace); the session resumes by id after
  an idle close, crash, model switch or restart. New knob
  `MARIM_CLAUDE_CLI_IDLE_TIMEOUT` (default 600 s) closes an idle process;
  `MARIM_CLAUDE_CLI_TIMEOUT` is now a per-turn *silence* bound that pauses while an
  approval prompt waits. Claude Code ≥ 2.1 required (older versions warn). Closes #109.

### Fixed

- **Resuming a sub-agent on a different model.** A spawn transcript written
  by one model and resumed on another (a tier reconfigured in between, or a
  recorded `model=` slug the current allowlist no longer carries) failed the
  first request with `400 Extra inputs are not permitted, field:
  messages[n].reasoning` — pydantic-ai replays persisted thinking parts under
  the field they arrived in, and models behind one OpenAI-compatible provider
  disagree about that field. The sidecar now records the model the spawn ran
  on (`model_ref`), and a resume that lands on a different model (or a sidecar
  that predates the stamp) drops the persisted thinking parts, keeping tool
  calls and results.
- **Stale daemon claim taken over silently.** After a `marim serve` daemon
  died, `marim --session <id>` (or `--resume`) ran the session locally with
  no sign of it — the only tell was a status bar without `daemon`. The launch
  now says so: a notice on stderr for headless, and in the TUI transcript
  once it is up.
- **Sub-agents screen repaint after teardown.** The screen's flush tick could
  fire after Textual had already removed the view from the DOM (a shutdown
  without `App.exit`, or a screen switch mid-stream) and raise `NoMatches`;
  the tick now skips quietly when the view is gone.

### Security

- Dependency bump clearing 19 pip-audit advisories: `cryptography` 50.0.1,
  `requests` 2.34.2 (pinned through a `[tool.uv]` override), `httpcore`/`httpx`
  2.12.0, `mcp` 1.30.0, `pyasn1` 0.6.4, `pydantic-settings` 2.15.0.

## [0.6.0] - 2026-09-01

### Added

- **Single-owner session claims.** A session file is now owned by at most one
  process at a time. The TUI, headless, and `serve` all claim the session
  before building a harness on it, so two processes can no longer interleave
  writes into the same history. A claim follows the active view — switching
  or creating a session adopts the new one and releases the old — and is
  released on teardown.
- Refusals that make the ownership visible instead of silently losing work:
  `serve` answers **409** for a session another process owns, the TUI refuses
  to switch onto a claimed session with a notice naming the holder, and
  deleting a claimed session is refused from both the CLI and HTTP.
- `serve` publishes the daemon's bind address in `runtime.json` (IPv6 hosts
  bracketed), so a client can find the daemon without assuming the configured
  host is the one it actually bound.

### Fixed

- **A turn interrupted mid-reasoning no longer wedges the session.** Aborting
  before the model emitted any text or tool call persisted a
  `ModelResponse(state="interrupted")` carrying only `ThinkingPart`s, which
  maps to an assistant message with `content: null` and no `tool_calls`.
  Providers disagree about that shape — xAI accepts it, Alibaba (qwen)
  rejects the request with *"The content field is a required field"* — so the
  session could fail every subsequent turn, and the failure typically only
  appeared on a later switch to a stricter model. Such responses are now
  dropped before the request, at turn-start sanitize, and on the abort flush
  (main agent and sub-agents), which both prevents new occurrences and heals
  an already-wedged session.

## [0.5.1] - 2026-08-29

### Removed

- The `forge` subsystem (Gitea/GitHub PR tools) — `list_prs`, `view_pr`,
  `ci_status`, `create_pr`, `checkout_pr`, the `ForgeBackend`/`TeaBackend`
  seam, `with_forge()`, and the `MARIM_FORGE` config knob. There was no
  GitHub backend implementation, only the Gitea-backed `TeaBackend`.

## [0.5.0] - 2026-08-29

### Added

- Structured output for embedder turns: `HarnessBuilder.with_output_type`
  accepts a pydantic `BaseModel` subclass or an object-rooted JSON Schema
  dict; turns validate against it (BaseModel: pydantic-ai in-run retries;
  dict: post-turn validation with one corrective round) and report through
  the new `TurnOutcome` subtypes, mirroring the Claude Agent SDK's
  `ResultMessage`.

### Changed (breaking)

- `Harness.run_turn` now returns a `TurnOutcome` instead of `str` — the
  final text moved to `outcome.result` (`outcome.structured_output` carries
  validated data when `with_output_type` is set). Migration: replace
  `out = await harness.run_turn(...)` with `out = (await
  harness.run_turn(...)).result` where you used the text.

## [0.4.0] - 2026-08-12

### Added

- `marim import claude` — import a Claude Code CLI memory store into the
  workspace's `.marim/memory`. Dry-run by default (`--apply` to write),
  auto-detects Claude's per-project store or takes `--from`, and skips anything
  that would overwrite an existing marim memory unless `--force` is passed.

- `marim update` — upgrade the installed package in place. Bare, it upgrades
  via `uv tool upgrade` and falls back to `pip install --upgrade` when marim
  was not installed as a uv tool; `--check` only reports the installed version
  against the latest on PyPI and leaves the install alone.

- The Tools settings page is navigable rather than a flat wall of switches.
  Controls are grouped into aligned compact rows, a docked help line explains
  whichever field holds focus, and controls that depend on a disabled parent
  (the LSP nav tools under `lsp_enabled`, the advisor token knobs under a
  configured advisor) dim and go unclickable instead of silently doing nothing.

### Fixed

- Context masking no longer corrupts tool-search observations. pydantic-ai
  narrows `content` to a typed payload on `ToolReturnPart` subclasses like
  `ToolSearchReturnPart`, and the masker — which matched every `ToolReturnPart`
  — was swapping that payload for its "observation elided" placeholder. A
  session that compacted after a tool search then spewed
  `PydanticSerializationUnexpectedValue` warnings on every history dump, died
  on the *next* turn with `TypeError: string indices must be integers` (pydantic-ai
  reads `content['discovered_tools']` on each request to decide which tools are
  visible), and could no longer be resumed at all — the part failed validation,
  so loading it raised `SessionLoadError`. Typed returns are now left alone;
  they carry reveal state, not the bulk masking exists to shed. Sessions and
  sub-agent transcripts already written in the broken shape are repaired on
  load instead of failing.

- Killing a spawned process now kills its deep descendants. MCP servers started
  under `claude-cli` call `start_new_session=True`, so they landed in their own
  process groups and survived a group-only kill — leaking a server process per
  spawn. `kill_process_tree` walks `/proc` for every descendant, groups them by
  PGID, and SIGKILLs each group, falling back to the old group-only behavior if
  the walk cannot run.

- Empty `Thinking:` labels no longer appear in the TUI transcript when a model
  emits a reasoning part with no text.

### Changed

- Minimum `pydantic-ai-slim` raised to `>=2.28,<3`. The lockfile had drifted to
  2.8 while the range admitted 2.28, so CI was not testing the version installs
  actually resolved — which is how the masking bug above shipped. `defer_loading`
  also changed shape in 2.28: a deferred capability tool is now withheld from the
  request entirely rather than listed and flagged.

## [0.3.0] - 2026-07-31

- The approval panel now shows what you are approving. A long `write_file`
  preview rendered 18 rows in a non-scrolling box with no scrollbar and no
  marker, so a 101-line write looked like an 18-line one — you consented to a
  file you could not see. The detail pane scrolls, and a live "+N more lines"
  hint counts everything below the fold, including the rows the hosting panel
  itself clips; it recomputes on resize instead of measuring once at mount.
  The title, the preview, and the sudo passthrough modal's command line all
  run through a sanitizer that neutralizes terminal control sequences, so a
  tool argument cannot repaint or relocate the text a consent decision is
  read from.

- Four crash and hang paths around that panel are closed. A pending
  interaction panel that lost focus could never get it back — `a`/`d` typed
  into the prompt and Esc cancelled the turn — so teardown now prefers a
  still-pending sibling and the sub-agents view refuses to open over one. A
  queued message containing an unbalanced `[` raised `MarkupError` mid-render
  and took the app down with the in-flight turn; queue rows compose as
  `Content` (never parsed) instead of escaping to markup. A read-only data
  dir or a non-UTF-8 prompt-history file killed the app on write and on
  launch respectively; both are best-effort now, matching `prefs.py`.
  Finished sub-agent cards stop their spinner timers instead of waking the
  app 10×/s for the rest of the session, and a turn ending by cancel or
  provider error settles its in-flight rows and cards rather than leaving
  them pending forever.

- A run of consecutive tool calls folds into its group when the results
  interleave with the calls (call → result → call → result) — the common
  sequential case, where the group used to stay expanded forever with no
  duration in its header. `/exit` and `/quit` no longer discard queued
  messages in silence; they warn when there is something to lose. The
  slash-menu no longer covers a multi-line draft: its offset is recomputed
  from the prompt's real height on every filter and re-wrap.

- A durable usage ledger records per-turn token counts and cost deltas to a
  pair of JSONL files under the sessions base, queryable through
  `load_overview()` / `load_models()` (`marim_harness.stats`) for spend and
  model mix over time. Writes ride on the existing per-turn usage banking and
  are best-effort — a ledger failure never fails a turn. There is no backfill
  from sessions recorded before this landed. Opt out with `stats=False` on
  the builder or `MARIM_STATS=0`; see `docs/sdk/sessions-and-state.md`.

- An LSP server whose process died is evicted and cold-started instead of
  being trusted as alive. Eviction keyed on multilspy's `server_started`
  flag, which only flips when the `start_server` context exits — so a server
  killed by the OOM killer or a segfault left that context suspended and the
  flag reporting a corpse as alive. Nothing else caught it either: the RPC
  read loop exits quietly on EOF without failing pending requests, so every
  later request for that language stalled to the full 15s timeout, for the
  rest of the session, with no restart path. The subprocess's returncode is
  now a second, authoritative signal; an uninspectable server still reads as
  alive, so only a *known*-dead one is ever evicted.

- The model is told the current date. It rides in the per-turn user message
  rather than the system prompt, so the cached prefix stays byte-stable
  across turns and across day boundaries.

- `/sessions` now opens an interactive picker instead of printing a text
  list: type to filter by name, Tab into the list to navigate, Enter to
  switch. Press `d` twice on a highlighted (non-active) session to delete it
  — the same teardown `marim sessions delete` already performs. `/switch
  <number|name>` is unchanged.

- `marim serve qr` prints a QR code that pairs a client with the daemon in one
  scan, encoding `marim://pair?v=1&url=…&token=…&name=…` — the URL, the bearer
  token, and a profile name (the machine's hostname by default), which is
  everything `marim-mobile` needs for a server profile. The address it encodes
  comes from the source address of the default route, so it's the one a phone
  on the same network should use rather than a `docker0` or bridge address, and
  it always prints in plain text under the code; `--advertise` overrides it for
  a tailnet name or a reverse proxy. `marim serve --qr` prints one at startup.
  Because the code carries a credential it is never part of normal startup
  output and is refused outright when stdout isn't a terminal. Needs `segno`
  (added to the `serve` extra); without it the pairing URI prints as text. The
  On a terminal that advertises sixel the code is drawn as an image — square,
  crisp, and free of any dependence on the font — and otherwise with Unicode 13
  sextants, six modules to a character cell, which puts a typical pairing
  payload in 27 columns by 18 rows and the whole block inside a 25-line
  terminal. `--sixel`/`--no-sixel` overrides the detection either way, and
  `--wide` falls back to half-blocks (square modules, five rows taller, drawn
  from a block every terminal font has had for decades) for fonts that predate
  Unicode 13 — an escape hatch the code prints under itself.

- `marim serve` startup now leads with the MARIM wordmark (the same art as the
  TUI intro header, shared from `interfaces/branding.py`) over an aligned block
  of the facts you need to drive the daemon — listen URL, bearer-token path,
  workspaces root, and idle TTL. The last two come from flags that were
  previously invisible at startup, so you couldn't tell from the terminal which
  workspaces root a running daemon had adopted. Only the token's path is
  printed, never its value. The art is skipped automatically when stdout isn't
  a terminal (systemd, Docker, a pipe), where the same facts print one per line
  led by the long-standing `marim serve … listening on …` line; new
  `--no-banner` flag and `MARIM_NO_BANNER=1` force that plain form on a
  terminal, and `NO_COLOR`/`TERM=dumb` drop the accent color.

- Model-catalog fetch failures no longer shout. An unreachable model server
  (LM Studio not running, the box offline, a slow upstream) used to spill a
  full httpx traceback per probe, which in `marim serve` — a daemon that
  re-fetches catalogs on every session build and model listing — buried the
  log. Transport failures now log a single line, and only the first time per
  endpoint until it answers again (repeats drop to DEBUG); HTTP status errors
  log one line every time (a 401 is actionable, its stack frames aren't);
  genuinely unexpected errors keep their traceback. Applies to the OpenRouter,
  Google, Zen, local (LM Studio/Ollama), and LM Studio context-window fetches.
- Interactive per-project trust: instead of a silent, undiscoverable
  `MARIM_TRUST_PROJECT_HOOKS` env var, marim now remembers a per-project trust
  decision in a persistent store (`$XDG_STATE_HOME/marim-harness/`), honored
  only while the project's gated surface (hooks/MCP/plugin executables)
  hasn't changed since the decision was made. First-open TUI dialog lists
  what a grant would enable and hot-applies it live (hooks reload, MCP
  connects, LSP registry rebuilds) — no restart needed; a decline persists
  too, with a one-line notice instead of re-prompting. New `/trust [on|off]`
  command and a live settings row; new `marim trust [status|grant|revoke]`
  CLI subcommand (headless `-p` prints a one-line stderr notice when
  untrusted, never re-prompting); `marim serve` gets
  `GET/POST /v1/workspaces/{ws}/trust` plus `trust_prompt_pending` on session
  payloads. `MARIM_TRUST_PROJECT_HOOKS` still works as a standalone override
  in both directions (explicit falsy now force-untrusts even over a trusting
  store). See `docs/guides/trust.md`.
- The TUI renders LaTeX math in replies (`$..$`, `$$..$$`, `\(..\)`, `\[..\]`)
  as Unicode approximations (`α² + √(β₁)`, `(-b±√(b²-4ac))/(2a)`) on every
  prose surface, including sub-agent transcripts. Streaming-safe by design
  (the parser converts a span once its closer arrives), falls back to literal
  LaTeX on anything unparsable, `MARIM_TUI_MATH=0` disables. flatlatex joins
  the `[tui]` extra.
- The test suite runs in parallel by default (pytest-xdist, `-n auto` with
  work-stealing): a ~5.5-minute serial run drops under a minute on a
  multi-core machine. `uv run pytest -n 0` restores the serial run for
  debugging.
- Live session mode switch: new `POST /v1/workspaces/{ws}/sessions/{sid}/mode`
  route lets a client change an existing session's approval mode
  (ask/auto/plan) after creation — same live-vs-persist shape as the existing
  `/model` route, 409 while a turn is running. The TUI's own mode
  toggle/cycle is unaffected (still a live, per-launch setting, not
  persisted).

## [0.2.0] - 2026-07-26

- `marim serve`: sessions can switch models — `GET /v1/models` lists the
  catalog, session create accepts a `model`, and
  `POST /v1/sessions/{sid}/model` switches an existing session (409 while a
  turn is running). Session payloads report the *effective* model, never null.
- `marim serve`: background jobs are visible over HTTP —
  `GET /v1/sessions/{sid}/jobs` returns the job snapshot and
  `GET /v1/sessions/{sid}/jobs/{job_id}` the detail (prompt + result), with
  `started_at` stamped on each job.
- Autonomous wake now works in serve mode: the wake policy moved into a shared
  `WakeDriver` orchestrator used by both the TUI and the HTTP daemon, so a
  scheduled wake fires exactly once per trigger in either front-end.
- `marim serve`: safe GET endpoints send explicit `Cache-Control` headers
  (session reads are `no-cache`, so clients never act on a stale snapshot).
- TUI tool summaries show workspace-relative paths instead of absolute ones —
  `src/app.py` rather than the full `/home/...` prefix.
- Error handling hardened across the codebase: streaming errors are classified
  transient and retried like other infra failures, and a sweep across 32
  modules replaced broad exception swallowing with precise handling.
- New bundled `marim-docs` skill: navigating marim-harness's own documentation
  (guides, reference, architecture) from inside a session.
- Large tool-output spills (sub-agent reports, workflow results, fetched
  bodies) now land in the session scratchpad instead of `.marim/output/`
  inside the workspace — intermediate files no longer clutter the project;
  the legacy directory is still read as a fallback.
- Resumed sessions now revalidate offloaded-output references at load: a
  handle whose scratchpad file was cleaned up (reboot, tmpfiles aging) gets
  an explicit "file no longer exists — re-run the tool" note appended, with
  the inline preview kept — instead of promising a `read_file` that would
  fail. Sub-agent and workflow spill notes now always carry absolute paths.
- New `zen-go` provider: OpenCode Go, Zen's flat-rate subscription plan, via
  its OpenAI-compatible endpoint — `MARIM_PROVIDER=zen-go` with the same
  `OPENCODE_API_KEY` as `zen`, default model `glm-5.2` (open coding models
  only). Catalog, settings card, and qualified `zen-go:<model>` ids included.
- Image reads hardened (follow-ups to the `read_file` image support below):
  files are now recognized by header magic, not extension alone — a text file
  named `diagram.png` reads as text and a corrupt/0-byte image gets a notice
  instead of failing the whole turn on a provider 400; a vision-gate-blocked
  read no longer counts as "file observed" for the read-before-edit guard;
  and sub-agent transcript sidecars externalize image bytes to the image
  cache instead of re-serializing inline base64 before every model request.
- `read_file` now returns image files (png/jpg/webp/gif, up to 5 MB) as
  model-visible images on vision-capable models — screenshots and diagrams can
  be inspected directly, including by sub-agents (gated per spawn's own model).
  Catalog-gated: a model the catalog marks text-only gets a text notice
  instead; unknown capability sends the image optimistically. Image tool
  results are cached content-addressed on disk (not inlined into session
  files) and masked like any other stale observation.
- New `zen` provider: OpenCode Zen (opencode's model gateway) via its
  OpenAI-compatible endpoint — `MARIM_PROVIDER=zen` + `OPENCODE_API_KEY`,
  default model `mimo-v2.5-free` (free tier). Catalog, settings card, and
  qualified `zen:<model>` ids included.
- `HarnessBuilder.with_capability(...)`: attach pydantic-ai
  `AbstractCapability` instances (e.g. Pydantic AI Harness modules) to the
  embedded agent, after marim's built-in capabilities.
- `marim_harness.capabilities.Advisor` — marim's advisor exported as a
  standard pydantic-ai capability, attachable to any pydantic-ai agent (or
  via `HarnessBuilder.with_capability`). Marim's own advisor now shares the
  same consult core, so the two cannot drift.

## [0.1.0.post1] - 2026-07-23

- Packaging only: the PyPI project page now renders the README's relative
  links and the demo GIF as absolute forge URLs (rewritten at build time via
  `hatch-fancy-pypi-readme`). No code changes.

## [0.1.0] - 2026-07-23

The first tagged release. Highlights of what exists today:

- **Interactive TUI** (Textual): streaming responses, tool-call cards, live
  token counter, inline approval / ask-user / plan panels, sub-agents screen,
  settings, model picker, themes.
- **Headless mode**: one-shot prompts with `text`, `json`, or `stream-json`
  output; `marim serve` HTTP daemon (REST + WebSocket) in the `[serve]` extra.
- **Providers**: OpenRouter (default), any local OpenAI-compatible server
  (Ollama, LM Studio), Google Gemini, and a `claude-cli` provider that
  delegates turns to Claude Code on a Claude subscription.
- **Permission modes** (`auto` / `ask` / `plan`), a configurable shell command
  policy, and workspace-confined file tools.
- **Sessions**: per-workspace persistence, resume, compaction with observation
  masking, checkpoints and `/rewind` backed by git snapshots.
- **Sub-agents**: parallel spawns with granted tool reach, model tiers
  (`cheap`/`med`/`high`), nesting with a depth ceiling, detached fan-out with
  live cards, and an optional `claude -p` backend.
- **Dynamic workflows**: a gated `run_workflow` tool executing model-authored
  scripts in a pydantic-monty sandbox (`[workflows]` extra).
- **Integrations**: MCP servers (global / project / plugin scope), LSP
  navigation + diagnostics via pluggable providers (Python, TypeScript, C++,
  Java bundled), Gitea/GitHub forge tools via the `tea` CLI, lifecycle hooks
  (Claude-Code-compatible), plugins, skills, and persistent memory.
- **Embeddable**: `HarnessBuilder` composes the same agent loop as a library
  with explicit config and no env reads (see `docs/embedding.md` and
  `docs/sdk/`).
- **Extras**: advisor model, thinking levels, desktop notifications, image
  input, background jobs with autonomous wake, session scratchpad.
