# Configuration reference

marim-harness is configured entirely through environment variables (`MARIM_*`
plus a few provider API keys). This page documents every variable the code
reads, its default, and its accepted format, as implemented in
`src/marim_harness/config/`.

## How configuration is loaded

`config/env.py::load_environment` populates the process environment at startup.
Precedence, highest first:

1. **Real shell environment** — an already-set variable is never overridden.
2. **Project-local `.env`** — found by searching the current directory and its
   parents. Loaded with `setdefault`, so it fills gaps but never beats the
   shell.
3. **Global config `.env`** — `$XDG_CONFIG_HOME/marim/.env`
   (`~/.config/marim/.env` when `XDG_CONFIG_HOME` is unset). Loaded last, as a
   fallback.

Security-relevant keys are **blocked from the project `.env`** entirely
(`_PROJECT_ENV_BLOCKLIST`): a cloned, untrusted repo ships its own `.env`, and
must not be able to self-trust, disarm the command policy, redirect model
traffic, swap credentials, or point marim at a committed executable. The
blocked keys are honored only from the shell environment or the global config:

- `MARIM_TRUST_PROJECT_HOOKS`, `MARIM_DEFAULT_MODE`
- `MARIM_COMMAND_ALLOWLIST`, `MARIM_COMMAND_DENYLIST`
- `MARIM_PROVIDER`, `MARIM_BASE_URL`, `MARIM_SEARXNG_URL`
- `MARIM_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`,
  `OPENCODE_API_KEY`
- `MARIM_CLAUDE_CLI_BIN`, `MARIM_CLAUDE_CLI_TIMEOUT`, `MARIM_CLAUDE_CLI_IDLE_TIMEOUT`
- `XDG_CONFIG_HOME`, `XDG_DATA_HOME` (a project `.env` redirecting the XDG
  dirs could relocate the "trusted" global config into the clone itself)

Model *selection* (`MARIM_MODEL`, `MARIM_CLAUDE_CLI_MODEL`) is deliberately not
blocked — a project pinning its model is legitimate and cannot redirect an
endpoint or swap a binary/credential.

API keys are only ever **read** from the environment. They are never written to
session files (`SessionStore` persists model/thinking/mode selections, not
credentials) or logs. The one place marim writes settings — the TUI Settings
screen and `marim config set`, via `config/persist.py` — targets the global
`.env` only, atomically, with `0600` permissions.

Two runtime notes:

- After load, `MARIM_WAKE_DEPTH_CAP`, `MARIM_SUBAGENT_TRANSCRIPT_CAP`, and
  `MARIM_TOOL_SEARCH_THRESHOLD` are validated as positive integers; an invalid
  value is dropped (with a warning) so the built-in default applies.
- A fresh interactive session inherits the **most recent session's model**
  (and thinking level), which overrides `MARIM_MODEL` from the environment.
  Switch models via the TUI picker if a stale selection sticks.

**Value formats used below.** *Boolean* variables accept `1`, `true`, `on`,
`yes` (case-insensitive) as true; any other non-empty value is false; unset
means the listed default. *Positive int* variables ignore non-integer or
non-positive values and fall back to the default (exceptions are noted).

## Provider & model

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_PROVIDER` | `openrouter` | Default provider: `openrouter`, `local`, `google`, `zen`, `zen-go`, `claude-cli`, or `codex-cli`. |
| `MARIM_MODEL` | per provider, see below | Model id on the default provider. Sent to the provider verbatim. |
| `MARIM_BASE_URL` | `http://localhost:11434/v1` | Base URL for the `local` provider (any OpenAI-compatible server). |
| `MARIM_API_KEY` | `local` (local provider) | Generic API key: used by `local`, and as a last-resort fallback for `openrouter`, `google`, `zen`, and `zen-go`. |
| `OPENROUTER_API_KEY` | unset | OpenRouter API key (preferred over `MARIM_API_KEY`). |
| `OPENCODE_API_KEY` | unset | OpenCode Zen API key, shared by the `zen` and `zen-go` providers (preferred over `MARIM_API_KEY`). Get one at <https://opencode.ai/auth>. |
| `GOOGLE_API_KEY` | unset | Google (Gemini) API key. |
| `GEMINI_API_KEY` | unset | Alternative Google key; checked after `GOOGLE_API_KEY`, before `MARIM_API_KEY`. |
| `MARIM_CLAUDE_CLI_BIN` | `claude` (resolved on PATH) | Claude Code executable to launch for the `claude-cli` provider and `backend: claude-cli` spawns. |
| `MARIM_CLAUDE_CLI_MODEL` | unset (CLI's own default) | Claude Code model for `backend: claude-cli` **sub-agent** spawns (alias like `sonnet` or a full id). |
| `MARIM_CLAUDE_CLI_TIMEOUT` | `600` | Seconds of silence (no stream object) allowed inside one open claude-cli turn, excluding time an approval prompt is waiting in the panel; on expiry marim interrupts, waits 2 s, then kills the process. |
| `MARIM_CLAUDE_CLI_IDLE_TIMEOUT` | `600` | Seconds a main-loop `claude` process may sit with no turn open before marim closes it; the next turn resumes the same Claude session by id. `0` disables reaping. Spawns and aux clones never idle (their process closes at run end). |
| `MARIM_CODEX_CLI_BIN` | `codex` (resolved on PATH) | Path to the `codex` binary when it is not on PATH (used by the `codex-cli` provider and `backend: codex-cli` sub-agents). |
| `MARIM_CODEX_CLI_TIMEOUT` | `600` | Seconds a Codex turn may sit idle (no notification) before marim interrupts it. Default `600`. |
| `MARIM_CODEX_CLI_MODEL` | unset (CLI's own default) | Default model for `backend: codex-cli` sub-agents (a Codex model id such as `gpt-5.4-mini`). The spec's `model:` and a spawn's `model=` override it. |

An unknown `MARIM_PROVIDER` value falls back to `openrouter` with a warning.
Every provider whose credentials are present is auto-detected and merged into
one model picker; `MARIM_PROVIDER` only selects the default (the startup model,
and the target for a bare model id without a `provider:` prefix). A qualified
id like `local:qwen2.5-coder` addresses any active provider.

`MARIM_MODEL` defaults per provider: `anthropic/claude-sonnet-4-6`
(openrouter), `qwen2.5-coder` (local), `gemini-2.5-flash` (google),
`mimo-v2.5-free` (zen), `glm-5.2` (zen-go), and *unset* for `claude-cli`
(the CLI uses its own configured default) and for `codex-cli` (the Codex
CLI's configured default model). The value is passed to the
provider verbatim — marim does not validate or rewrite it.

Requests to the `zen` and `zen-go` gateway carry a `marim-harness/<version>`
User-Agent and a stable `x-opencode-session` id (one per marim process), which
OpenCode uses for routing and prompt-cache affinity — Go rejects requests
without it.

The `zen` provider talks to [OpenCode Zen](https://opencode.ai/auth)'s
OpenAI-compatible gateway at a fixed `https://opencode.ai/zen/v1` (not
`MARIM_BASE_URL`), authenticated with `OPENCODE_API_KEY`. Its catalog is
fetched from the public `/models` endpoint and filtered to OpenAI-compatible
ids — `claude-*`/`gemini-*` ids are hidden because they route to Anthropic/
Google endpoint shapes marim's zen provider doesn't speak. Free-tier models
carry a `-free` suffix (e.g. `mimo-v2.5-free`, `deepseek-v4-flash-free`).
Qualified ids like `zen:mimo-v2.5-free` work anywhere a qualified id does,
including sub-agent tier slugs.

The `zen-go` provider is the same Zen account on OpenCode's flat-rate
[Go subscription plan](https://opencode.ai/docs/go/): a separate
OpenAI-compatible endpoint at `https://opencode.ai/zen/go/v1` whose catalog
is open coding models only (`glm-5.2` default, `kimi-k3`, `minimax-m3`,
`deepseek-v4`, …). It authenticates with the same `OPENCODE_API_KEY`; a key
without an active Go subscription lists the provider but fails clearly at the
first chat request. Billing is flat monthly with usage windows, so marim shows
no per-token cost for it.

`claude-cli` runs your Claude subscription through the `claude` CLI. marim keeps
one long-lived `claude` process per conversation and talks to it over its
stream-json control protocol: every tool Claude wants to run comes back to marim
as a permission request, so `auto`/`ask`/`plan`, the approval panel and
`ask_user` apply exactly as with a native model; a steer folds into the running
turn; an interrupt is a control request (the process is killed only if it
ignores the interrupt for 2 s). The conversation resumes by session id after an
idle close (`MARIM_CLAUDE_CLI_IDLE_TIMEOUT`), a crash, a model switch, or a
marim restart. Claude runs its own tools, LSP and MCP servers — marim's tools,
LSP and MCP do not apply. The process is launched with `--safe-mode
--strict-mcp-config --setting-sources ""`, so Claude's own hooks, MCP servers,
plugins and settings do NOT load; its skills and `CLAUDE.md` files are plain
files under the workspace and remain readable (the CLI's `--bare` flag would
close that gap but breaks subscription auth, so it is not used). Requires Claude
Code 2.1 or newer (older versions log a warning). `/mode`, `/model` and
`/think` reach the running process as control requests before the next
turn (`set_permission_mode`, `set_model`, `set_max_thinking_tokens` +
`apply_flag_settings {effortLevel}`), so none of them needs a restart; a
same-provider `/model` switch keeps the process and its session (the launch
`--model` still seeds the first spawn). Plan mode is enforced twice: Claude
runs in its own plan mode AND marim denies every mutating `can_use_tool`.
The thinking level maps to a token budget (`off` 0, `minimal` 1024, `low`
4096, `medium` 16384, `high` 32768, `xhigh` 65536) plus an effort level
(`off`/`minimal`/`low` → `low`, then `medium`, `high`, `xhigh`); each
Claude model honours whichever of the two it supports — an
adaptive-thinking model cannot switch thinking off, so `off` is its lowest
effort. An unset level sends nothing and leaves the CLI's own defaults.

The status bar's `ctx` field shows Claude's own context under this provider:
each `assistant` event carries the prompt size of that request (uncached
input plus both cache buckets), and the turn's `result` names the model's
context window. After each turn marim refines that reading through
`get_context_usage {detail: summary}`, which includes Claude's local estimates
for the system prompt and tool schemas without making extra token-count API
calls. It also asks the CLI for its `get_usage` rate limits and shows them as
`quota 11% (5h) · 59% (1w)` (the five-hour and seven-day windows); failed
reads are ignored. Cost in the
usage ledger is per turn: the CLI's `result` reports a *running* total for
the process (documented as resetting on a fresh or resumed session and on a
mid-session `/clear`), so marim bills each turn the increase since the
previous result and starts over whenever it launches a new process.

Claude's own Agent sub-agents run in the background: the turn that spawns one
ends as soon as it is launched (its spawn card stays open, and a resumed
history records the call as "reports later", not as cut short), and when the
agent finishes while no turn is open Claude Code reacts on its own — it
injects the report into its history and runs a model turn nobody asked for.
marim buffers that turn and plays it as an *autonomous* turn (trigger
`autonomous`, an empty prompt) as soon as the session is idle: the card
settles with the agent's report, the reaction renders as its own transcript
entry, marim's history stays aligned with Claude's, and nothing is sent to
the CLI for it. This bypasses the wake policy on purpose — it costs no marim
model call and there is no decision to make, the turn already ran. A typed
turn submitted first goes out first; the buffered one plays right after. The
idle reaper stretches its clock tenfold while a background agent is still
running (closing the process would kill it and lose the report; the
stretched clock only bounds an agent that never reports, since a closed
process resumes by id anyway) and starts the normal one when the report
lands. A `backend: claude-cli` *spawn* and a headless run (`marim -p`)
own their process for one turn only, so they wait a running background
agent out before closing it (bounded by `MARIM_CLAUDE_CLI_TIMEOUT`): the
spawn's report is Claude's reaction to the agent (the last result of the
process), and headless plays the reaction as an autonomous turn and prints
its text after the turn's own. An aux clone (advisor, summarizer) is
one-turn read-only and does not wait.

No API key is read for this provider — the CLI owns its own subscription auth.
The model picker's `claude-cli` catalog is the CLI's own `/model` menu, read
from its stream-json handshake: it lists whatever aliases and models the
installed Claude Code offers *your* account (plan and org allowlist applied),
each row naming the concrete model an alias resolves to today, so it stays
current with every CLI release. A session already running on claude-cli
refreshes it for free; otherwise marim launches one throwaway `claude` for the
handshake alone (no session file, about a second) and caches the answer for
ten minutes. If the CLI cannot be launched the picker falls back to the family
aliases `opus`, `sonnet`, `haiku` and `fable`.
`MARIM_CLAUDE_CLI_MODEL` applies only to sub-agent specs with
`backend: claude-cli` (precedence: per-spawn override, then the spec's
frontmatter model, then this variable, then the CLI's default); the main-loop
claude-cli model comes from `MARIM_MODEL`. `MARIM_CLAUDE_CLI_BIN` may be a
name resolved on PATH or a path; a non-positive or unparseable
`MARIM_CLAUDE_CLI_TIMEOUT` or `MARIM_CLAUDE_CLI_IDLE_TIMEOUT` (float, seconds)
falls back to its default rather than disabling the guard (`0` for the idle
timeout means never reap).

Under the `codex-cli` provider marim delegates each turn to `codex app-server`
(the Codex CLI's JSON-RPC front end): one app-server per marim *process* — a
module-level singleton shared by the main-loop model and every `codex-cli`
spawn, not one process per session (a `marim serve` daemon holding many
sessions shares a single app-server). Codex runs its own tools inside its
own sandbox, so marim's tools and LSP do not apply. The app-server is
launched with config overrides that isolate it from the user's own Codex
setup: every MCP server in the user's Codex config is disabled by name
(`codex mcp list` enumerates them, each gets
`-c mcp_servers.<name>.enabled=false`), the plugin system is switched off
(`-c features.plugins=false`) and so is the built-in apps connector
(`-c features.apps=false`, the `codex_apps` server with the `github.*`
tools), so a marim-started thread loads neither marim's own MCP servers nor
Codex's user-level ones, nor Codex plugins or connectors. Two residuals:
Codex *skills* (`$CODEX_HOME/skills`, `~/.agents/skills`, `.codex/skills`)
have no working override and still load, and a server whose name is not a
bare TOML key (anything outside `[A-Za-z0-9_-]`) cannot be disabled this
way — marim logs it at WARNING and it loads. The env-gated live smoke
(`tests/test_codex_live.py`) checks the server's own `mcpServerStatus/list`
reports no connected server (disabled servers are still listed there,
without server info or tools). Unlike `claude-cli`, Codex *asks*
before privileged actions and marim answers: approvals go through the same
approval panel native tools use. The modes map as follows.

| marim mode | Codex `approvalPolicy` | Codex sandbox |
|---|---|---|
| `auto` | `on-request` | workspace-write (the workspace root; the scratchpad is always writable) |
| `ask` | `untrusted` (every command/edit is brokered to the approval panel; scratchpad-only edits auto-accept) | workspace-write |
| `plan` | `never` | read-only |

Requires `codex login` (marim never handles the OpenAI credentials) and
`codex >= 0.152`. `/think` levels map to Codex reasoning effort
(`minimal`/`low` → low, `medium`, `high`, `xhigh` when the model lists it);
`/steer` forwards to the running turn; `/compact` also compacts the Codex
thread. The thread id is saved with the session, so `--resume` continues the
same Codex thread; if Codex no longer has it, a fresh thread starts from the
saved history. Codex's own sub-agents (its collab `spawn_agent` tool) are
first-class `spawn_agent` cards in the sub-agents screen, their approval
requests brokered through the same panel labelled with the agent's name —
see [Codex-side sub-agents](../guides/subagents.md#codex-side-sub-agents).
(Up to 0.7.2 they rendered as opaque `codex_agent` tool cards; histories
written then still expand fine, the name is just no longer produced.)

Per-turn usage on a resumed thread is seeded from Codex's own
`thread/tokenUsage/updated` notification (its cumulative `total` minus the
newest response's `last` at the turn's first update), so the first turn
after `--resume` reports only its own tokens rather than the whole thread's
history. After each turn marim also polls `account/rateLimits/read` once and
shows the subscription quota in the status bar as `quota 37% (5h) · 12% (1w)`
(primary and secondary windows); a failed read is ignored and the field
simply stays absent. The same notification's `last.inputTokens` and
`modelContextWindow` feed the status bar's `ctx` field, so it shows Codex's
own prompt size and window rather than an estimate over marim's mirror. The Settings › Providers card verifies the CLI live on
show (a `model/list` against the app-server) and reports
`✓ connected · N models` like a keyed provider.

## Context window & compaction

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_CONTEXT_BUDGET` | `100000` | Global context budget in tokens — a spend ceiling, not the model's window. `0` = unbudgeted (window-only). |
| `MARIM_MAX_CONTEXT_TOKENS` | — | **Deprecated** alias for `MARIM_CONTEXT_BUDGET` (same meaning); warns once, still honored when the new name is unset. |
| `MARIM_CONTEXT_WINDOW` | unset (discover) | Manual context-window override in tokens for servers discovery can't read. Always wins over discovery. |
| `MARIM_CONTEXT_BUDGETS` | empty | Per-model budget overrides: comma-separated `pattern=tokens` pairs. |
| `MARIM_MASK_OBSERVATIONS` | `1` (on) | Boolean. At compaction, let the upstream strategy clear older non-mutating tool results. |
| `MARIM_MASK_KEEP_RECENT` | `4` | Positive int. Approximate number of recent tool-call/result pairs clearing retains. |
| `MARIM_MASK_MIN_CHARS` | — | **Deprecated and ignored.** Accepted for one compatibility release with a warning; upstream clearing uses a whole-pass token-savings threshold. |

Compaction and masking trigger at `min(budget, 0.8 × window)`, where the 0.8
safety ratio applies only when the window is *known* (discovered from the
provider catalog / local probe, or stated via `MARIM_CONTEXT_WINDOW`). A
negative `MARIM_CONTEXT_BUDGET` is treated as garbage and fails **closed** to
the 100k default (only an explicit `0` uncaps). Under `claude-cli` and
`codex-cli` the CLI's own auto-compaction governs its context; marim's
threshold still gates compaction of the mirrored history, but the status
bar's gauge shows the backend's reported context, not this threshold (see
the provider notes above).

`MARIM_CONTEXT_BUDGETS` pairs are fnmatch patterns matched against the model
id, first match wins; `=0` or an empty value means "no budget for this model"
(window-only). Example:
`anthropic/claude-opus*=60000,openrouter/*free*=0`. A `provider:` qualifier on
the model id is stripped before matching. Malformed pairs are dropped
silently.

## Sessions & UI

No environment variable names a session; sessions persist under
`$XDG_DATA_HOME/marim-harness/sessions` and carry their own model, thinking
level, and advisor selection, which override the corresponding env defaults on
resume (and the latest session's model seeds a fresh one — see the loading
notes above). TUI theme selection is a Settings-screen choice, not an env var
(`MARIM_THEMES` in `interfaces/tui/themes.py` is a Python constant holding the
built-in theme palette, not configuration).

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_TUI_MATH` | `1` (on) | Boolean. Render LaTeX math spans (`$..$`, `$$..$$`, `\(..\)`, `\[..\]`) in assistant replies as Unicode approximations. |

Needs `flatlatex` (the `[tui]` extra); an unparsable span falls back to the
literal LaTeX and the transcript on disk always keeps the raw source.
`MARIM_TUI_MATH=0` reverts the TUI to Textual's stock markdown parser.

## Approval & command policy

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_DEFAULT_MODE` | `ask` | Initial approval mode for a fresh interactive session: `ask`, `auto`, or `plan`. |
| `MARIM_COMMAND_DENYLIST` | empty | Regex patterns; a bash command matching any of them is blocked. |
| `MARIM_COMMAND_ALLOWLIST` | empty | Regex patterns; when non-empty, a bash command matching none of them is blocked. |

`MARIM_DEFAULT_MODE` is **ignored when set from a project-local `.env`** — a
cloned repo shipping `MARIM_DEFAULT_MODE=auto` would silently auto-approve
every mutation in that repo, so the startup posture comes only from the shell
environment or the global config. An invalid value falls back to `ask` with a
warning. The headless one-shot has its own `--mode` flag and does not consult
this variable.

The command lists are comma- or newline-separated regular expressions matched
with `re.search` against the whole command string; deny takes precedence over
allow, and an empty policy allows everything. A pattern needing a literal
comma can use a character class (`[,]`). The policy is enforced inside the
`bash` tool itself, so it applies in every mode and to sub-agents — but it is
defense-in-depth against honest mistakes, **not a sandbox**. A pattern that is
not valid regex fails closed: a broken deny rule blocks everything, a broken
allow rule grants nothing (both logged). Both variables are blocked from the
project `.env`.

## Trust

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_TRUST_PROJECT_HOOKS` | unset (fall through) | Tri-state. `1`/`true`/`on`/`yes` forces trusted; any other set value (`0`, `false`, ...) force-*untrusts*, overriding even a trusting store entry; unset falls through to the per-project trust store. |

This is one layer of the project-trust gate; the full resolution order is
explicit config → this env var → the per-project trust store (honored only
while its fingerprint matches the project's current hooks/MCP/plugin
surface) → untrusted (fail closed). The store is written by the TUI's
first-open trust prompt, `/trust`, `marim trust grant/revoke`, and
`POST /v1/workspaces/{ws}/trust` — see [the trust guide](../guides/trust.md)
for the full model. When resolved trusted (by any layer), project-local hooks
(`.marim/hooks.json`), project-local MCP servers (`.marim/mcp.json`),
project-scope plugins, project skills/agents, and third-party plugin **LSP**
manifest blocks are honored; otherwise they are withheld (global and bundled
equivalents always load). The env var is blocked from the project `.env`, so
a cloned repo cannot self-trust; the store lives outside the repo entirely
(`$XDG_STATE_HOME/marim-harness/trusted-projects.json`).

## Sub-agents

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_SUBAGENT_CONCURRENCY` | `8` | Max spawns running their model loop at once. `0` (or negative) = unbounded. |
| `MARIM_SUBAGENT_REQUEST_LIMIT` | `50` | Positive int. Max model requests one sub-agent run may make before it is aborted. |
| `MARIM_SUBAGENT_TRANSCRIPT_CAP` | `2000` | Positive int. Per-part character cap on persisted spawn transcripts (tool results are truncated to this many characters). |
| `MARIM_SUBAGENT_TIER_CHEAP` | unset (inherit main) | Model for the `cheap` tier, qualified `provider:model_id`. |
| `MARIM_SUBAGENT_TIER_MED` | unset (inherit main) | Model for the `med` tier. |
| `MARIM_SUBAGENT_TIER_HIGH` | unset (inherit main) | Model for the `high` tier. |
| `MARIM_SUBAGENT_TIERING` | `1` (on) | Boolean. Master switch for tier routing. |
| `MARIM_DETACH_FANOUT` | `1` (on) | Boolean. `spawn_agent` with `background` unset auto-detaches in the TUI (live cards + job handles) instead of running inline. |
| `MARIM_AUTONOMOUS_WAKE` | `1` (on) | Boolean. Finished background jobs start a new turn to deliver their reports while you're away. |
| `MARIM_WAKE_DEPTH_CAP` | `8` | Positive int. Bounds chained autonomous wakes. |

`MARIM_SUBAGENT_CONCURRENCY` is the one knob where a non-positive value is a
meaningful sentinel (unbounded) rather than an error; unparseable garbage
falls back to the safe cap of 8, never to unbounded.

Native spawns pick a model by tier (`cheap`/`med`/`high`): resolved from the
spawner's `tier=` override, then the spec's `tier:` frontmatter, then tool
reach (read-only → cheap, mutating → high). A configured tier maps to its
`MARIM_SUBAGENT_TIER_*` model; unset tiers inherit the main model. Once any
tier is configured, a raw `model=` slug override is bounded to the set of
configured tier models. `MARIM_SUBAGENT_TIERING=0` bypasses routing (every
spawn inherits the main model) without clearing the configured slugs, so it
round-trips as a toggle. Tiering applies to native spawns only; the
`claude-cli` main-loop provider bypasses it.

## LSP

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_LSP` | `1` (on) | Boolean. Master switch: language-server pool + diagnostics on write/edit. |
| `MARIM_LSP_TOOLS` | `1` (on) | Boolean. The six LSP navigation tools (requires `MARIM_LSP` on). |

With `MARIM_LSP=0` nothing LSP-related is built. With `MARIM_LSP=1` and
`MARIM_LSP_TOOLS=0` the manager still runs (diagnostics keep grounding the
agent after writes) but the navigation tools are not registered. marim never
downloads server binaries — it probes PATH and surfaces the provider's install
hint. Bundled language plugins (python, typescript, cpp, java) always load;
third-party `lsp` manifest blocks follow the `MARIM_TRUST_PROJECT_HOOKS` gate.
See `docs/lsp-plugins.md`.

## MCP & tool search

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_TOOL_SEARCH` | `auto` | Defer the MCP/plugin tool surface behind tool search: `off`, `auto`, or `on`. |
| `MARIM_TOOL_SEARCH_THRESHOLD` | `15` | Positive int. In `auto`, defer only when the live MCP tool count exceeds this. |

`off` loads every MCP tool on every request; `on` always defers; `auto` defers
only above the threshold. Built-in tools are never deferred. An invalid
`MARIM_TOOL_SEARCH` value falls back to `auto` with a warning. MCP servers
themselves are configured in JSON (`.marim/mcp.json` project-local, gated by
`MARIM_TRUST_PROJECT_HOOKS`; global/plugin servers always load), not by env
vars.

## Hooks & plugins

Hooks and plugins are configured by files, not environment variables; the only
env gate is `MARIM_TRUST_PROJECT_HOOKS` (above), which controls whether
project-local hooks and project-scope plugins run.

`MARIM_PLUGIN_ROOT` is **not an environment variable**: `${MARIM_PLUGIN_ROOT}`
is a substitution token used inside a plugin's manifest (hook entries, MCP
server specs, LSP `command`/`args`). At load time it is replaced with the
plugin's installed root directory, so manifests can reference bundled files
without hard-coding install paths. See `docs/plugins.md`.

## Workflows

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_WORKFLOWS` | `1` (on) | Boolean. Dynamic workflows (the `run_workflow` tool; requires the `[workflows]` extra). |
| `MARIM_WORKFLOW_TIMEOUT` | `1800` | Positive int, seconds. Wall-clock deadline per `run_workflow(code)` call, including child waits. Separate from the 30-second sandbox compute cap; no per-call timeout override. |

With `MARIM_WORKFLOWS=0` the engine is never built, even when pydantic-monty
is installed.

## Advisor

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_ADVISOR_MODEL` | unset (no advisor) | Model the agent may consult mid-task via the `advisor` tool: qualified `provider:model_id`, or a bare slug for the default provider. |
| `MARIM_ADVISOR_MAX_TOKENS` | `2048` | Output cap per consultation; minimum 1024. |
| `MARIM_ADVISOR_MAX_USES` | unset (unlimited) | Calls per model request; unset or `0` = unlimited. |

When `MARIM_ADVISOR_MODEL` is unset the tool is not offered to the model at
all. `/advisor <model>` and `/advisor off` persist immediately and apply on the
next turn; an explicit saved `off` overrides the env default on resume.
The model calls `advisor(prompt: str)` with a self-contained question and current
evidence. Completed history is forwarded, excluding the current response.
The call allowance resets per model request. Provider errors propagate through
normal turn handling, and nested usage shares the turn's usage limits.

Runtime configuration resolves a concrete model through Marim's model source and
uses upstream local execution. The SDK's explicit upstream `Advisor` also offers
native/auto routing with upstream provider resolution. See [SDK migration](../sdk/capabilities.md).
Runtime advice is unavailable with a `claude-cli` or `codex-cli` main executor;
either CLI works as an advisor using a separate ephemeral read-only conversation.

## Thinking

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_THINKING` | unset (no reasoning effort) | Thinking level applied via `ModelSettings.thinking`: `off`, `minimal`, `low`, `medium`, `high`, or `xhigh`. |

Unset and `off` are equivalent and byte-identical to pre-thinking behavior
(the settings key is omitted, preserving prompt caching). An unrecognized
value is treated as unset — a typo silently disables thinking rather than
crashing startup. The `/think` command overrides it per session (persisted on
the session store, which wins over the env on resume). Under the `claude-cli`
main-loop provider this is a documented no-op. Sub-agents resolve their level
as: spawn override → spec `thinking:`/`effort:` frontmatter → inherited
session level.

## Notifications

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_NOTIFICATIONS` | `1` (on) | Boolean. Native desktop notifications for agent events. |
| `MARIM_NOTIFICATION_EVENTS` | `turn_complete,error,approval_needed,ask_user` | Comma/newline-separated event names to notify on. |

Known events: `turn_complete`, `error`, `approval_needed`, `ask_user`,
`job_done` (the default set is everything but `job_done`). Unknown names are
dropped; if nothing valid remains, the default set applies. Notifications are
best-effort (`notify-send` on Linux, `osascript` on macOS, a PowerShell toast
on Windows) and never raise; rapid duplicates of the same event are coalesced
within a 2-second window.

## Startup banner

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_NO_BANNER` | `0` (banner shown) | Boolean. Suppress `marim serve`'s startup wordmark. |

The wordmark prints only when stdout is a terminal, so a daemonized `marim
serve` (systemd, Docker, `nohup`) already gets the plain line-per-fact form
without setting anything; `MARIM_NO_BANNER=1` (or `--no-banner`) forces it on
a terminal too. `NO_COLOR` (any non-empty value) and `TERM=dumb` keep the art
but drop the accent color.

## Scratchpad & memory

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_SCRATCHPAD` | `1` (on) | Boolean. Per-session scratchpad directory for intermediate files. |
| `MARIM_PROACTIVE_MEMORY` | `0` (off) | Boolean. The agent saves/recalls memory notes unprompted instead of only on request. |

The scratchpad is advertised in the system prompt, reachable by the file tools
as an extra guard root, and auto-approved in ask mode; `MARIM_SCRATCHPAD=0`
removes all three. `MARIM_PROACTIVE_MEMORY` only switches which memory-policy
instructions the model receives (proactive vs on-request); the
`remember`/`recall` tools exist either way.

## Stats ledger

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_STATS` | `1` (on) | Boolean. Per-turn usage recording into the stats ledger. |

Each turn's token/cost delta is appended as one JSONL line to both a
per-workspace and a global ledger file under
`$XDG_DATA_HOME/marim-harness/stats`. Requires sessions to be on (in-memory
sessions have no session id to attribute events to); `MARIM_STATS=0` disables
recording without affecting sessions themselves.

## Misc & debug

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARIM_DEBUG` | unset | Exactly `1` enables DEBUG logging (and sub-agent spawn timing lines); anything else leaves it at WARNING. |
| `MARIM_SEARXNG_URL` | `https://searxng.marim.dev` | SearXNG endpoint for the `web_search` tool. |
| `MARIM_IMAGE_CACHE_DIR` | `~/.marim/image-cache` | Root of the content-addressed cache for pasted-image bytes. |
| `MARIM_JOB_TOOL_COMBINED` | `0` (off) | Boolean. Collapse the four job tools (`jobs`/`job_output`/`wait_for_job`/`cancel_job`) into one `job(action, …)` tool, for models that handle fewer tools better. |

Unlike the other booleans, `MARIM_DEBUG` compares the value to the literal
string `1` — `true`/`on`/`yes` do not enable it.

`MARIM_SEARXNG_URL` is operator-controlled only (blocked from the project
`.env`): it is an egress target, and search results feed back into the agent's
context. Your instance must have SearXNG's JSON output format enabled (off by
default in SearXNG).
