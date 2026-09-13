# Changelog

All notable changes to marim-harness are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/) —
pre-1.0, minor versions may contain breaking changes.

## [Unreleased]

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
