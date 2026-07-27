# Headless mode

`marim -p "prompt"` runs a single agent turn without the TUI and prints the
result to stdout. Headless runs go through the same construction path as the
interactive app (`runtime/bootstrap.build_harness`), so the model, session
store, MCP servers, hooks, and tools are wired identically — the only thing
missing is the interactive surface. That makes headless the mode for shell
pipelines, cron jobs, and CI.

> Provider note: under the `claude-cli` main-loop provider, marim acts as a
> launcher — the `claude` CLI runs its own tools and its own approval loop,
> so marim's tools, approval modes, LSP, and MCP do not apply to those turns.
> See [claude-cli differences](#the-claude-cli-provider) below.

## Run a one-shot turn

Three ways to get into headless mode:

```bash
marim -p "summarize the failing tests"     # explicit prompt
echo "explain src/foo.py" | marim          # piped stdin implies headless
marim -p < prompt.txt                      # bare -p reads the prompt from stdin
```

Any piped (non-tty) stdin selects headless even without `-p`. When you pass
both an explicit `-p PROMPT` *and* piped stdin, the explicit prompt wins and
stdin is ignored. An empty prompt prints `no prompt provided` to stderr and
exits with code 2.

The optional positional argument selects the workspace (default: the current
directory):

```bash
marim ~/code/myrepo -p "run the test suite and report failures"
```

### Sessions

Every headless run creates and persists a real session — the same kind the
TUI uses. It shows up in `marim sessions list`, and marim waits for the
background auto-naming to finish before printing the result, so the `json`
and `stream-json` outputs report the final session name. To continue the
most recent session instead of starting a fresh one:

```bash
marim -p "now fix the bug you found" --resume
```

`--resume` reattaches to the latest saved session for the workspace and
replays its history before the turn runs.

### Flags

| Flag | Effect |
|---|---|
| `-p, --print [PROMPT]` | run headless; with no value, read the prompt from stdin |
| `--output-format text\|json\|stream-json` | result rendering (default: `text`) |
| `--mode plan\|auto` | approval mode; headless defaults to `auto`. `ask` is rejected — it needs the TUI |
| `--resume` | continue the workspace's latest saved session |
| `--think off\|minimal\|low\|medium\|high\|xhigh` | thinking level for this run (overrides `MARIM_THINKING`) |
| `--worktree BRANCH` | run inside a git worktree for `BRANCH` under `<repo>/.worktrees/`, creating or reusing it |

Note that headless ignores `MARIM_DEFAULT_MODE`: without `--mode` it always
runs in `auto`, because nothing can answer an approval prompt.

## Output formats

All three formats stream the turn internally (mirroring the TUI, which keeps
runs on providers' more reliable streaming endpoints); the format only
changes what is written to stdout.

### `text` (default)

The final assistant message, followed by a newline. Nothing else goes to
stdout, so it is safe to capture:

```bash
answer=$(marim -p "one-line summary of this repo")
```

### `json`

One JSON object on stdout after the turn completes:

```json
{
  "output": "The test fails because ...",
  "session_id": "2026-07-23-104512-a1b2c3",
  "name": "Debug failing auth test",
  "usage": {
    "input_tokens": 8474,
    "output_tokens": 412,
    "total_tokens": 8886,
    "uncached_input_tokens": 1210,
    "cache_read_tokens": 7264,
    "cache_write_tokens": 0,
    "cost_usd": 0.0184,
    "cost_is_exact": true
  }
}
```

Fields: `output` is the final assistant text; `session_id` and `name`
identify the persisted session; `usage` is the canonical token/cost
breakdown (`cost_usd` is the provider's billed figure when reported —
`cost_is_exact: true` — otherwise a price-table estimate, or `null` when the
model isn't priced).

### `stream-json`

Newline-delimited JSON (NDJSON): streaming events as they happen, then a
terminal line. The event vocabulary is shared with `marim serve`'s WebSocket
stream, so a consumer of either sees the same shapes:

```json
{"type": "text", "text": "Looking at the"}
{"type": "text", "text": " test file now."}
{"type": "thinking", "text": "The assertion compares ..."}
{"type": "tool_call", "name": "read_file", "args": {"path": "tests/test_auth.py"}, "id": "call_abc123"}
{"type": "tool_result", "id": "call_abc123", "content": "1: import pytest ..."}
{"type": "result", "output": "The test fails because ...", "session_id": "2026-07-23-104512-a1b2c3", "name": "Debug failing auth test", "usage": {"input_tokens": 8474, "output_tokens": 412, "total_tokens": 8886, "uncached_input_tokens": 1210, "cache_read_tokens": 7264, "cache_write_tokens": 0, "cost_usd": 0.0184, "cost_is_exact": true}}
```

- `text` / `thinking` — incremental deltas of the assistant's answer and
  (when the model emits it) its reasoning.
- `tool_call` — a tool invocation with its parsed `args` and a `tool_call_id`.
- `tool_result` — the tool's output, keyed by the same `id`.
- `result` — the terminal line: the same object the `json` format prints,
  plus the `"type": "result"` envelope.

On a failed turn the stream still gets a terminal line, so a crash never
looks like a truncated stream:

```json
{"type": "error", "error": "Provider error: ... · code=400"}
```

## Exit codes and error behavior

| Code | Meaning |
|---|---|
| 0 | the turn completed; the result was printed |
| 1 | the turn failed — a one-line error goes to stderr (and, for `stream-json`, a `{"type": "error"}` line to stdout) |
| 2 | usage error: empty prompt, invalid arguments, or a failed `--worktree` setup |
| 130 | interrupted (Ctrl-C) — Python's standard SIGINT exit |

On a provider error, stderr gets a compact one-liner assembled from the
upstream body (message, code, provider name, truncated raw detail). The
full, untruncated payload is spilled to `.marim/last-provider-error.json`
in the workspace for debugging.

Two guarantees worth relying on in scripts:

- **Cleanup never masks the exit code.** Session persist, hook teardown, and
  MCP shutdown are each best-effort; a failure there is logged, not raised.
- **Failed runs stay resumable.** An aborted or failed turn flushes a
  repaired, resumable history (never one ending in an unanswered tool
  call), so `--resume` on the next invocation picks up cleanly. This holds
  for Ctrl-C too — the flush runs under a tight deadline so interrupt stays
  snappy.

Headless also fires desktop notifications on completion and error when
notifications are enabled, same as the TUI.

## Scripting and CI patterns

Extract just the answer:

```bash
marim -p "list the public functions in src/api.py" --output-format json \
  | jq -r .output
```

Track spend across a batch:

```bash
marim -p "$task" --output-format json | jq .usage.cost_usd
```

Watch tool activity live:

```bash
marim -p "audit the error handling in src/" --output-format stream-json \
  | jq -c 'select(.type == "tool_call") | {name, args}'
```

Read-only analysis in CI — plan mode denies every mutation but lets
read-only shell commands and file reads through, so an untrusted or
exploratory prompt cannot modify the checkout:

```bash
marim --mode plan -p "review this diff for missing test coverage" \
  --output-format json | jq -r .output
```

(Plan mode also denies the outbound network tools `fetch_url`/`web_search`,
closing off exfiltration through a fetch URL; see below.)

Isolate mutating runs in a worktree so parallel jobs don't collide:

```bash
marim --worktree ci-fix-lint -p "fix all ruff findings and commit"
```

Choose a provider and model per run via the environment (see
[configuration](../reference/configuration.md) for the full list):

```bash
MARIM_PROVIDER=openrouter MARIM_MODEL=anthropic/claude-sonnet-4-6 \
  marim -p "..."
```

One inheritance rule to know: a **fresh** session inherits the model (and
thinking level) of the workspace's most recent session, and that inherited
choice takes precedence over `MARIM_MODEL`. If a previous session switched
models interactively, a later headless run in the same workspace keeps that
model even with `MARIM_MODEL` set. Point scripted runs at a clean workspace,
or check the `json` output's `usage`/session fields when the exact model
matters.

## What is NOT available headless

- **Approval prompts.** `ask` mode is interactive by definition, and the
  `--mode` flag rejects it (`plan` and `auto` only); headless defaults to
  `auto`. As a defense in depth, if `ask` mode is ever active with no
  approver attached (an embedding or server scenario), gated tool calls are
  denied with "no approver available; denied" rather than hanging or
  crashing.
- **Gated tools under `plan`.** `write_file`, `edit_file`, and mutating
  `bash` commands are denied ("read-only plan mode"); read-only `bash`
  commands are approved so the agent can research; `fetch_url`/`web_search`
  are denied with a note to leave plan mode.
- **The `ask_user` tool.** With no UI attached it returns "Can't ask the
  user — no interactive UI here. Proceed with your best judgment." — the
  agent is told to decide on its own rather than stall.
- **`present_plan` execution choices.** In plan mode the plan is still
  written to `.marim/plans/`, but with no UI the tool reports that it is
  staying in plan mode instead of offering the execute/hand-off choices.
- **Interactive pickers and panels.** Model picker, session picker,
  settings, rewind, the sub-agents screen — use the management subcommands
  and environment variables instead.

## Management subcommands

These run without a model or a session; they are plain CLI utilities.

### `marim sessions`

```bash
marim sessions list [workspace] [--json]
marim sessions delete <id> [workspace]
```

`list` prints a table (ID, NAME, UPDATED, MESSAGES, TOKENS, DURATION) or,
with `--json`, an array of objects with `id`, `name`, `updated`,
`message_count`, `tokens`, and `duration_seconds`. `delete` removes a
session by id (exit 1 if the id doesn't exist in the workspace).

### `marim config`

```bash
marim config show [--json]
marim config set KEY VALUE
```

`show` prints the resolved configuration (provider, model, base URL, context
limits, default mode, tool-search settings, whether an API key is set, and
the global config path). `set` persists one of a fixed allowlist of keys
(`MARIM_PROVIDER`, `MARIM_MODEL`, `MARIM_BASE_URL`, `MARIM_API_KEY`,
`OPENROUTER_API_KEY`, `MARIM_DEFAULT_MODE`, context and tool-search knobs)
to the global config file; unknown keys and invalid enum/integer values are
rejected with exit 2 rather than silently written.

### `marim models`

```bash
marim models list [--json]
```

Lists the models available from the configured providers — qualified id and
display name, or `{"id", "name", "provider"}` objects with `--json`.

### `marim mcp`

```bash
marim mcp add <name> <command> [args...]        # stdio server
marim mcp add --transport http <name> <url> -H "Authorization: Bearer ..."
marim mcp list [--json]
marim mcp get <name>
marim mcp remove <name>
```

Manages MCP servers in the global config or the project's `.marim/mcp.json`
(`--scope user|project`, default project). The flag surface mirrors
`claude mcp add`. Project-scoped servers only load when the project is
trusted (`marim trust grant` below, or `MARIM_TRUST_PROJECT_HOOKS`). See the
[MCP guide](mcp.md) for the full picture. Plugins are managed with `marim plugin` — see
[plugins](../plugins.md).

### `marim trust`

```bash
marim trust                 # status: decision + source + gated surface (cwd)
marim trust grant [path]    # persist trusted, against the current fingerprint
marim trust revoke [path]   # persist untrusted
```

Inspects or sets the same per-project trust decision the TUI's first-open
prompt and `/trust` write — see the [trust guide](trust.md). `status` is the
default action, the workspace defaults to the current directory, and a bad
path exits `2`. Deliberately cheap: stays off the `pydantic_ai` import path
like `marim config`/`marim models`. A headless `-p` run never prompts; when
the workspace ships gated config and isn't trusted, it prints one stderr-only
note (`marim trust grant && marim -p ...` is the one-shot pattern).

### `marim serve`

`marim serve [--host] [--port] [--workspaces-root] [--idle-ttl]` runs marim
as a long-lived HTTP daemon exposing sessions over REST plus a WebSocket
event stream (the same event shapes as `--output-format stream-json`). It binds
`127.0.0.1:8642` by default, authenticates with a bearer token persisted
under the server state dir (the token file's path is printed at startup),
and requires the `serve` extra (`pip install 'marim-harness[serve]'`). See the
[serve API reference](../reference/serve-api.md).

## The claude-cli provider

With `MARIM_PROVIDER=claude-cli`, each headless turn is delegated to the
`claude` CLI (a Claude subscription) as `claude -p`; Claude Code runs its
own tools and its own permission system, so marim's tool set, approval
modes, LSP, MCP, and `--think` do not apply to the turn. marim's `--mode`
still matters at the boundary — it is mapped onto Claude Code's
`--permission-mode`: `auto` becomes `acceptEdits`, and `plan` (or anything
else, since headless can't answer prompts) becomes Claude's read-only
`plan` mode. Output formats work the same shape-wise, but with a twist:
without a UI attached, Claude Code's internal tool activity is folded into
the assistant text as `▸` activity lines, so `stream-json` carries it in
`text` events rather than `tool_call`/`tool_result` events.
