# `marim acp` — Agent Client Protocol Interface Design

**Date:** 2026-07-10
**Status:** Approved (brainstorm), pending implementation plan

## Goal

A `marim acp` command that serves marim-harness over the
[Agent Client Protocol](https://agentclientprotocol.com) (stdio JSON-RPC), so
ACP-capable editors — Zed, JetBrains IDEs, Neovim (CodeCompanion/avante),
Emacs — can host marim as an external coding agent: streamed turns, diff-
rendered edits, mid-turn tool approval, session resume, and mode/model
switching from the editor UI.

The integration serves **marim**, not a bare Pydantic AI agent: `Mode`
(ask/auto/plan) semantics, marim's session store and resumability invariants,
hooks, compaction, MCP config, and error handling all apply unchanged. This is
the deciding reason to build on the official `agent-client-protocol` Python
SDK rather than adopt pydantic-ai-harness's experimental `run_acp_stdio`
capability — that adapter owns the run loop around a bare `Agent` and would
bypass `Harness` entirely (and it carries a may-change-without-deprecation
warning). Its module remains a *reference implementation* for protocol sharp
edges (approval lifecycle states, chunking limits, presenter design,
permission-decision scoping); steal solutions, not the dependency.

Decisions settled in brainstorm Q&A:

- **No spike.** Straight to the real integration; the harness-ACP README and
  the codebase exploration already answered what a spike would.
- **V1 features:** core protocol (initialize, session/new, streamed prompts,
  cancellation, ask-mode permission requests) **plus** session resume
  (`session/load`), mode switching from the editor, rich tool rendering
  (kind/locations/diffs), and model selection from the editor.
- **Deferred (v2+):** editor-native filesystem/terminal routing, client-offered
  MCP servers, ACP slash commands (`available_commands`), sub-agent tree
  rendering (spawns surface as a plain `spawn_agent` tool call in v1).
- **Validation target:** Zed (reference ACP client; exercises permissions,
  modes, diffs, session picker). Live testing uses the local LM Studio model.
- **claude-cli provider:** allowed, degraded honestly — Claude's own tool
  activity streams via the existing `on_cli_activity` side-channel and renders
  as display-only completed tool-call updates, no approval prompts (mirrors
  what headless already does).

## Why the codebase is ready for this

- `run_headless` (`interfaces/cli/headless.py`) is a working template for a
  non-TUI front-end: `connect()` → `session_start()` → `run_turn(prompt,
  event_stream_handler=…)` → guarded teardown (`wait_autoname`,
  `finalize_active_time`, `persist(force=True)`, `session_end`, `aclose`).
- `Harness.bind_ui` is the callback contract an ACP bridge needs —
  `request_approval` is exactly the seam `session/request_permission` plugs
  into, the same way the TUI's approval modal does.
- `resolve_approvals` (`runtime/permissions.py`) already encodes auto/plan
  behavior with no callback; the ACP bridge only supplies the ask-mode path.
- `stream_events.event_to_dict` proves out the pydantic_ai-event → wire-object
  mapping (stream-json); the ACP translator is its sibling.
- No `os.chdir` anywhere; multiple `Harness` instances on different workspace
  roots coexist in one process (the serve-mode design already relies on this).
- The `serve` extra shows the optional-dependency pattern: lazy import in the
  CLI module, install-hint on `ImportError`.

## Architecture (chosen: one harness per ACP session)

`marim acp` starts an ACP stdio server (editor launches it as a subprocess).
On `session/new(cwd)` the adapter calls `bootstrap.build_harness(root=cwd)`
and wires ACP callbacks through `bind_ui` — the same attach path the TUI uses.
Multiple editor workspaces become multiple harnesses in one process, each with
its own session store, MCP connections, and mode. Provider config comes from
the subprocess environment (the editor's agent config sets `MARIM_PROVIDER`,
API keys, etc.), same as any marim launch.

Alternatives considered and rejected:

- **Single harness at process start, process-cwd-rooted** — simpler, but an
  editor may create sessions with different cwds; we'd mis-root or reject
  them. Saves little since `build_harness` already exists.
- **Reuse serve-mode's `SessionSupervisor`/`WorkspaceRegistry` behind an ACP
  transport** — attractive symmetry, but serve is a long-lived daemon with
  idle eviction and token auth; ACP is an editor-launched subprocess that dies
  with the connection. Different lifetimes; the only genuinely shared code is
  already in `build_harness`.

One harness = one turn at a time: the adapter serializes prompts per session
(a second `session/prompt` while one is in flight queues behind it), matching
the TUI's exclusive-worker discipline.

### Package layout

New top-level package `src/marim_harness/acp/` (peer of `server/`), plus a CLI
entry. Pure translation is side-effect-free and unit-tested directly; the SDK
wiring stays thin — per the repo's pure-helpers/thin-IO convention.

```
acp/
  server.py       # SDK wiring: stdio transport, session registry, dispatch
  session.py      # one ACP session ↔ one Harness: lifecycle, turn queue, teardown
  translate.py    # pydantic_ai stream events → session/update payloads (pure)
  presenter.py    # tool call → ACP kind / file locations / diff blocks (pure)
  permissions.py  # ask-mode approval bridge → session/request_permission
interfaces/cli/acp.py   # arg parsing, lazy import, install hint
```

`interfaces/cli/router.py` gains an `acp` management keyword (lazily imported,
like `serve`). New extra in `pyproject.toml`:
`acp = ["agent-client-protocol>=0.11"]` (SDK requires Python >=3.10 — matches
`requires-python`). The rest of the codebase must not import from
`marim_harness.acp` — only the CLI entry does, behind the import guard.

## Session lifecycle

- **`initialize`** — advertise agent name/version, text-only prompt
  capabilities in v1, `session/load` support, and the three session modes.
- **`session/new(cwd)`** — `build_harness(root=cwd)` → `bind_ui(ACP
  callbacks)` → `connect()` → `session_start("startup")`. Marim's session id
  doubles as the ACP session id (no mapping table).
- **`session/load(id)`** — locate the workspace whose store holds `id` (the
  editor passes cwd on load too), build/reuse that harness,
  `switch_session(id)`, then replay the persisted transcript to the client as
  update notifications: user text recovered via `strip_turn_context`,
  assistant text as message chunks, historical tool calls as already-completed
  tool-call updates. Unknown id → ACP error response.
- **`session/cancel`** — cancel the in-flight turn the way the TUI's Ctrl-C
  path does; `_flush_resumable` keeps the persisted history resumable (never
  ending on an unanswered `ToolCallPart`).
- **Disconnect (stdin EOF) / `session/close`** — per live harness, the same
  guarded teardown sequence as `run_headless`'s `finally` block:
  `wait_autoname` → `finalize_active_time` → `persist(force=True)` →
  `session_end("exit")` → `aclose`. Teardown steps are individually guarded so
  one failure can't mask another.

## Turn loop

`session/prompt` → `harness.run_turn(text, event_stream_handler=…)`. The
handler feeds `translate.py`, which maps pydantic_ai events to ACP
notifications:

- text deltas → `agent_message_chunk`; thinking deltas →
  `agent_thought_chunk` (chunked under the SDK's wire limits)
- tool call start → `tool_call` (annotated by `presenter.py`); tool result →
  `tool_call_update` (`completed`/`failed`)
- gated tools awaiting approval surface as `pending` and transition to
  `in_progress` only once granted, so an unapproved action is never rendered
  as running
- claude-cli provider: `on_cli_activity` events → display-only completed
  tool-call updates

Token usage from `harness.session.usage` is reported on the `PromptResponse`
(the same UNSTABLE ACP field pydantic-ai-harness uses; clients that don't
know it ignore it).

## Permissions and modes

`bind_ui(request_approval=…)` bridges ask-mode deferrals to
`session/request_permission` with **allow-once / reject-once** options only in
v1 — no remembered "always" decisions, because marim's `Mode` already covers
"stop asking me" (switch to auto). Reject → `ToolDenied`, identical to the TUI
path. A permission request outstanding when the client cancels the turn
resolves as rejected.

The three marim modes are advertised as ACP session modes (`ask`, `auto`,
`plan`). Editor mode switches call `harness.set_mode`; marim-side switches
(e.g. plan-flow transitions) emit `current_mode_update` via the
`on_mode_change` callback. Plan-mode denials flow through `resolve_approvals`
untouched — the editor simply sees the denied tool call with marim's denial
message.

## Rich rendering and model selection

`presenter.py` maps marim tool names to ACP annotations, from tool args alone
(no extra I/O):

- `read_file`, `grep`, `glob`, LSP navigation → kind `read`/`search`, with
  file `locations` (click-to-file in Zed)
- `edit_file` / `write_file` → kind `edit`, locations, and a diff content
  block built from the old/new text already present in the args
- `bash` → kind `execute`, the command as content
- unknown/MCP tools → kind `other`, raw args (never crash on shape surprises)

Model selection: advertise the active provider's model list as the ACP
`model` session config option (default = the harness's current model, which —
per existing behavior — a resumed session's saved model may override).
Selection calls `harness.set_model(persist=True)`. If the provider can't
enumerate models, the option is simply not advertised.

## Error handling

Turn failures map to an ACP error response carrying `format_provider_error`
detail — the same small, scriptable failure surface headless has. The
actionable-error-note machinery (what the *model* sees next turn) is
unchanged; infra errors (429/5xx) reach the editor as the error response only.
Full provider payloads keep spilling to `.marim/last-provider-error.json`. A
crash in translation/rendering must fail the notification, not the turn —
translate errors are logged and the event dropped, mirroring the render-bug
silence rule in `_actionable_error_note`.

## Testing

- **Unit:** `translate.py`, `presenter.py`, `permissions.py` as pure functions
  (event in → payload out; call in → annotation out; approval/denial paths
  including cancel-while-pending).
- **Integration:** drive the real server over an in-process duplex stream
  using the SDK's client classes (the pattern pydantic-ai-harness uses in
  `tests/experimental/acp/_wire.py`), with `TestModel`/`FunctionModel` —
  no network, no editor. Cover: initialize handshake, new/prompt/stream,
  approval round-trip (approve and reject), plan-mode denial, cancel mid-turn
  with resumable history, session/load replay, mode and model switching,
  teardown persistence.
- **Packaging:** bare install (no extra) → `marim acp` prints the install
  hint and exits 1, mirroring the serve test.
- **Live validation:** Zed `agent_servers` entry launching `marim acp` against
  the local LM Studio model (no paid models). Checklist: streamed turn, edit
  diff rendering, approval prompt, mode picker, model picker, reopen a past
  session.
- CI order locally before done: `ruff check` → `pyright` → `pytest` on the
  supported matrix; `agent-client-protocol` joins the dev dependency group so
  tests run on all legs.

## Out of scope (deferred)

- Editor-native filesystem/terminal (`fs/read_text_file` etc. routed through
  the client) — marim's tools work on the shared disk in v1.
- Client-offered MCP servers — marim has its own MCP config; a client sending
  MCP servers gets them ignored in v1 (documented), revisit with v2.
- ACP slash commands (`available_commands`).
- Sub-agent tree rendering; image/audio prompt blocks; remembered
  always-allow permission scopes.
