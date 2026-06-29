# Claude CLI as a model provider (use a Claude subscription)

**Date:** 2026-06-29
**Status:** Design — awaiting review
**Author:** brainstorming session

## Goal

Let marim use a **Claude Pro/Max subscription** as a selectable model provider,
alongside the existing `openrouter` / `local` / `google` providers, so Claude-model
work runs on the subscription at no per-token cost.

## The hard constraint (why this looks the way it does)

A Claude subscription authenticates the `claude` CLI via OAuth. That token is **not**
a general Anthropic API key — it only works *through* Claude Code, which is itself an
agent that runs its **own** tool loop. There is no supported way to point Pydantic AI's
Anthropic model at a subscription for raw per-step inference.

Consequence: a subscription exposes **an agent, not a model**. marim is built around the
opposite assumption (a provider hands it a model that marim drives with *its own* tools
and approval loop). So in this provider, marim becomes a **launcher**: it shells out to
`claude -p`, and Claude runs its own tools/loop internally. marim's own tools, approval
gating, LSP, and MCP do **not** apply when this provider is active. This is an accepted
tradeoff — the motivation is cost, and the provider is opt-in per session.

(Gray-area OAuth→Messages-API proxies that fake raw model access were considered and
rejected: they violate Anthropic's ToS and break frequently.)

## Approach: a custom Pydantic AI `Model` (no turn-loop changes)

The key design move is to **not special-case `Harness.run_turn`.** Instead, add a
custom `Model` subclass so everything downstream — `Harness`, `_run_with_approval`,
resumability, the TUI render path — stays untouched. The existing provider seam is
exactly the right place: `build_model(cfg)` already maps a provider name to a Pydantic
AI `Model`.

```
build_model(cfg)  ──cfg.provider == "claude-cli"──▶  ClaudeCliModel(Model)
                                                          │
   pydantic_ai Agent calls model.request_stream(messages)│
                                                          ▼
                              spawn `claude -p` (reuse cli_backend helpers)
                                                          │
                    Claude runs ITS OWN tools/loop internally (sub billing)
                                                          ▼
                  returns ONE ModelResponse = a single final-text TextPart
                                                  (+ synth_usage)
```

### Critical invariant: the response must be pure text

If `ClaudeCliModel` emitted `ToolCallPart`s in its response, Pydantic AI's agent graph
would try to **execute those tool calls itself** — double-running the work Claude already
did internally. Therefore the assembled `ModelResponse.parts` must contain **only a
`TextPart`** (Claude's narration + final answer). marim's loop then sees a `str` output
and terminates the turn cleanly with no approval round.

Claude's internal tool activity is surfaced as **streamed text**, never as structured
tool-call parts (see Streaming below).

## Reuse surface

`subagents/cli_backend.py` is already a kit of pure, dependency-light helpers (it imports
only `pydantic_ai` + `..usage`, so `config` importing it via a lazy import inside
`build_model` is cycle-safe). Reused as-is:

- `resolve_cli_binary()` — find the `claude` executable (`$MARIM_CLAUDE_CLI_BIN` or PATH).
- `build_cli_argv(...)` — construct the `claude -p --output-format stream-json --verbose …`
  argv. Extended to support `--resume <session_id>` (see History).
- `synth_usage(cli_usage, num_turns, total_cost_usd)` — build `RunUsage` from the CLI's
  terminal `result` event, including billed cost via `COST_DETAIL_KEY`.
- `_iter_ndjson_lines(stream)` — the 64 KiB-safe ndjson line reader.
- The stream-json event shapes (`system`/`assistant`/`user`/`result`) that
  `CliStreamTranslator` already parses.

**Not** reused verbatim: `CliStreamTranslator` (it emits real `ToolCallPart` events for
the sub-agent card). The main loop needs tool activity flattened to text instead — see
Streaming. We add a small main-loop translator for that.

## New code (small, isolated)

### 1. `config/model.py` — register the provider

- Add `"claude-cli"` to `_KNOWN_PROVIDERS`.
- `_provider_config`: a `claude-cli` branch. `model` = `MARIM_MODEL` (default `None` →
  let the CLI use its configured default; accepts `sonnet`/`opus`/`claude-sonnet-4-6`/…).
  No `base_url`/`api_key` (the CLI owns auth).
- `_provider_has_creds("claude-cli")` → `resolve_cli_binary() is not None`. (Binary
  present ≠ logged in, but a not-logged-in run fails with a clear CLI error; good enough
  for "is this provider available".)
- `build_model`: a `claude-cli` branch that lazily imports and returns `ClaudeCliModel`.
- `ModelSource.list_models`: a small static catalog for the picker
  (`claude-cli` entries: `sonnet`, `opus`, `haiku`). Optional; `[]` is acceptable for v1.

### 2. `config/claude_cli_model.py` — the adapter (new file)

- `class ClaudeCliModel(pydantic_ai.models.Model)`:
  - `__init__(self, model_id: str | None)` — stores the model id; `mode_getter` and a
    `session_id` holder default to `None` and are set later.
  - `request(self, messages, model_settings, model_request_parameters) -> ModelResponse`
    — non-streaming path (headless one-shot): spawn `claude -p`, collect the final
    `result` text + usage, return a single-`TextPart` `ModelResponse`.
  - `request_stream(...)` — returns a `ClaudeCliStreamedResponse` (below).
  - Both share one spawn routine: resolve binary → derive permission mode from
    `mode_getter()` → build argv (with `--resume` when a `session_id` is known) →
    `create_subprocess_exec` → consume ndjson → capture `session_id` (first
    `system`/`init` event) + final `result`.
- `class ClaudeCliStreamedResponse(pydantic_ai.models.StreamedResponse)` — consumes the
  ndjson stream and yields **only** `PartStart`/`PartDelta` text events:
  - `assistant` text blocks → streamed as text deltas.
  - `assistant` `tool_use` blocks → rendered as a compact activity line folded into the
    text stream, e.g. `\n  ⏺ Read config/model.py`. (Args summarized like the TUI's
    native labels: path for Read/Write/Edit, command for Bash, pattern for Grep/Glob.)
  - `user` `tool_result` blocks → not shown by default (kept terse); errors may append a
    short `  ⮑ failed` marker.
  - terminal `result` → captures usage; the accumulated text becomes the final
    single-`TextPart` `ModelResponse`.
- A small `CliUnavailable`-style error path: if the binary is missing or the process
  ends without a `result`, raise a clear error the harness surfaces as an actionable note.

### 3. `runtime/bootstrap.py` — two wiring lines

After the `Harness` is constructed, if the active model is a `ClaudeCliModel`:
- set `model.mode_getter = lambda: harness.mode` (live approval mode; same late-binding
  pattern already used for `get_model`).
- The `session_id` holder is persisted on the marim session so a resumed marim session
  reconnects to Claude's session (see History). On a fresh session it starts `None`.

No change to `Harness.run_turn` / `_run_with_approval`.

## Behavior decisions (locked)

### Approval-mode mapping

Headless `claude -p` cannot pop a per-tool prompt mid-run, so marim's `ask` mode has no
faithful equivalent. Mapping:

| marim mode | `--permission-mode` | Notes |
|------------|---------------------|-------|
| `auto`     | `acceptEdits`       | Claude edits/runs freely. |
| `plan`     | `plan`              | Read-only; Claude produces a plan. |
| `ask`      | `acceptEdits`       | Treated like `auto`. A **one-time per-session notice** warns that per-tool gating is unavailable in the claude-cli provider. |

The mode is read **per request** via `mode_getter()`, so switching mode mid-session
takes effect on the next turn.

### History: resume Claude's session

- Turn 1 (no `session_id`): spawn `claude -p "<latest user message>"` with
  `--append-system-prompt "<marim system prompt>"`. Capture `session_id` from the first
  `system`/`init` event.
- Turn N (`session_id` known): spawn `claude -p --resume <session_id> "<latest user
  message>"`. Claude holds the running context server-side; marim sends only what's new,
  which keeps Claude's prompt caching effective and is kinder to subscription rate limits.
  `--append-system-prompt` is sent only on the first (non-resume) call to avoid
  duplicating the system prompt into the resumed session.
- The "latest user message" is the text of the newest `ModelRequest` user part — which is
  marim's already-assembled prompt (the `<turn-context>` envelope, task checklist, etc.).
- **Fallback:** if `--resume` fails (session expired/unknown), drop `session_id` and
  flatten the full Pydantic AI message history into a single prompt for that one turn,
  then capture a fresh `session_id` from the result.
- **Known limitation:** a marim checkpoint *rewind* or compaction can desync Claude's
  server-side session from marim's history. v1 accepts this; the flatten fallback bounds
  the damage (a failed/garbled resume degrades to a clean stateless turn). Documented, not
  solved, in v1.

### Streaming fidelity

Stream Claude's assistant text live, with a compact per-tool activity line folded into the
text stream (decision above). Implemented as text deltas only, to preserve the pure-text
response invariant.

## Tools & MCP

This provider does **not** map marim's tools to `--allowedTools`. Claude Code uses its own
native tools and its own `~/.claude` / project `.claude` configuration, gated by the
`--permission-mode` derived above. We therefore omit `--allowedTools` entirely (the CLI
uses its configured defaults for the chosen permission mode). marim's MCP/LSP/hooks do not
reach Claude in this provider.

## Usage & cost

`synth_usage` already folds the CLI's cache buckets into `input_tokens` and stores the
CLI's `total_cost_usd` via `COST_DETAIL_KEY`, so marim's existing cost/usage display works
unchanged. On a subscription the CLI still reports a notional `total_cost_usd`, which
surfaces as the per-turn cost (informational; not actually billed per token).

## Error handling

- Missing `claude` binary → provider reports unavailable at config detection; if selected
  anyway, the first turn raises a clear "claude CLI not found" error.
- CLI exits without a `result` event → raise `CliRunError`-style error; the harness'
  `_actionable_error_note` surfaces a short, model-facing message and the turn ends
  cleanly (resumability invariant preserved — no dangling tool-call parts, since the
  response is text-only).
- `--resume` failure → automatic one-turn flatten fallback (above), logged at DEBUG.

## Testing

Pure helpers are unit-tested without spawning a process:

- `_provider_config` / `_provider_has_creds` / `build_model` routing for `claude-cli`
  (monkeypatch `resolve_cli_binary`).
- The main-loop translator: feed canned ndjson `system`/`assistant`/`user`/`result`
  objects → assert it emits only text-delta events and that `tool_use` blocks become the
  expected activity lines (never `ToolCallPart`s).
- `session_id` capture from the `init` event; argv includes `--resume` on the second call
  and omits `--append-system-prompt` there.
- Mode mapping table (`auto`/`ask`→`acceptEdits`, `plan`→`plan`) via a stub `mode_getter`.
- Resume-failure fallback path (simulate a `result`-less first attempt → flatten).

Integration (opt-in, skipped without a real CLI/login): a smoke test behind an env guard
that runs one real `claude -p` turn and asserts a non-empty final text + a captured
`session_id`.

## Out of scope (possible v2)

- **Real per-tool approval brokering** via Claude Code's stream-json control protocol
  (`canUseTool`), which would let marim's own approval modal gate each tool and restore
  faithful `ask`-mode behavior. Significantly more work (bidirectional streaming); deferred.
- Surfacing Claude's tool calls as **structured, richly-rendered** nested cards in the
  main transcript (blocked by the pure-text response invariant; would require a side
  channel distinct from the model response).
- Mapping marim's MCP servers / skills into the Claude Code spawn.
- Robust checkpoint-rewind ↔ Claude-session reconciliation.
```
