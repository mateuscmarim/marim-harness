# Sub-agents and background jobs

The main agent can delegate work to **sub-agents**: isolated agents that start
with a clean context, run a single task with a tool reach fixed at spawn time,
and hand back one final report. Use them to fan out independent work in
parallel, to keep large investigation output (file dumps, logs, search sweeps)
out of the main context, or to run long tasks detached in the background while
you keep working.

This guide covers how spawning works, how to author your own agent specs, how
model tiers route spawns to cheaper or stronger models, the `claude-cli`
backend, and the operational knobs. Keyboard/TUI details live in
[guides/tui.md](tui.md); the full env-var tables live in
[reference/configuration.md](../reference/configuration.md).

> Provider note: under the `claude-cli` *main-loop* provider, marim is a
> launcher and none of this applies to the main turn — Claude Code runs its
> own Agent/Task sub-agents, which marim demuxes out of the stream and renders
> in the sub-agents screen. `backend: claude-cli` on an individual *spec*
> (below) is a different, fully supported thing.

## How the model spawns them

The main agent calls the `spawn_agent` tool with a `type` (an agent name) and
a `task`. The sub-agent runs to completion and its final message becomes the
tool's result. Several spawns issued in one response run in parallel — that is
the intended fan-out pattern.

`spawn_agent`'s notable parameters (see the tool docstring in
`src/marim_harness/tools/spawn_tools.py` for the model-facing text):

- `type` — a built-in (`explore`, `general`), a bundled agent (`researcher`),
  or a custom/plugin agent by qualified name.
- `task` — the one required ask. `returns` / `constraints` / `context` are
  optional structured fields folded into the prompt (output contract, soft
  boundaries, orchestration background the clean-context spawn can't see).
- `description` — a short label for the spawn's card and tool line.
- `background` — see the next section.
- `mcp` — MCP server names to grant this spawn (none by default).
- `after` — background job ids this spawn must wait for; their reports are
  appended to its task. Requires a detached spawn.
- `max_output_chars` — a soft output budget; over-budget reports are spilled
  to a workspace file (`.marim/subagent-output/`) and replaced with a
  within-budget head plus a pointer, so nothing is lost.
- `tier`, `model`, `thinking` — model routing, covered below.
- `isolation="worktree"` — run a mutating spawn in its own git worktree,
  branched from HEAD; its changes are committed to a `subagent/<id>` branch
  named in the report.

### Inline vs detached

There are three shapes, selected by the `background` parameter:

- **Unset (the usual case).** In the interactive TUI, with detached fan-out on
  (`MARIM_DETACH_FANOUT`, default on), the spawn **auto-detaches**: it becomes
  a background job with a live sub-agent card, and the tool returns a job
  handle immediately. The agent can end its turn (the report is delivered when
  the job finishes) or `wait_for_job` inline. Auto-detached spawns default to
  a ~12,000-character output budget when none was passed, keeping a wide
  fan-out's synthesis bounded. Outside the TUI, or with detach off, an unset
  spawn runs inline.
- **`background=True`** — an explicit fire-and-forget background job.
- **`background=False`** — force an inline (foreground) run: the tool call
  blocks until the sub-agent reports.

Detached spawning (explicit or auto) is **top-level only**: a sub-agent's own
turn ends before a detached child would finish, so its report could never
reach the spawner. Nested spawns therefore always run inline.

### How results come back

An inline spawn's report is simply the tool result. A detached spawn's report
flows through the **job registry**:

- The agent can pull it with the jobs tools — `jobs` (list), `job_output`
  (non-blocking read), `wait_for_job` (block with a timeout), `cancel_job`
  (or the combined `job` tool). Polling is actively discouraged: repeated
  no-change checks get escalating nudges to end the turn instead.
- When a job finishes between turns, a **finished-job digest** is prepended to
  the next user turn's context, so the agent sees outcomes without asking.
- In the TUI, **autonomous wake** (`MARIM_AUTONOMOUS_WAKE`, default on) goes
  further: a background job finishing while the app is idle fires a
  digest-only turn on its own, so the agent reacts to the result without
  waiting for your next message. Chained wake→spawn→wake sequences are
  bounded by `MARIM_WAKE_DEPTH_CAP` (default 8). Toggle at runtime with
  `/jobs wake on|off`.

Interrupted spawns persist a sidecar transcript and can be resumed from the
sub-agents screen; a resume always continues as a background job.

## Agent specs

Two built-ins always exist, defined in code
(`src/marim_harness/workspace/agents.py`):

- `explore` — read-only investigation plus web access; changes nothing.
- `general` — the full sub-agent toolset; carries out a focused sub-task.

Custom agents are single Markdown files with YAML frontmatter; the file body
is the agent's system prompt. Discovery roots, highest precedence first:

1. **Project** — `.marim/agents/<name>.md` in the workspace. Loaded only when
   the project is trusted (the same `MARIM_TRUST_PROJECT_HOOKS` gate as hooks
   and MCP): a spec chooses a spawn's prompt, tool grants, and backend, so a
   cloned untrusted repo must not be able to plant one.
2. **Global** — `$XDG_CONFIG_HOME/marim/agents/` (default
   `~/.config/marim/agents/`).
3. **Bundled** — marim's own defaults, shipped inside the package
   (`src/marim_harness/builtin/agents/`; currently `researcher`).
4. **Plugin** — agents bundled by installed plugins, addressed as
   `plugin:name` (e.g. `superpowers:code-reviewer`). Project-scope plugin
   agents are gated by the same trust flag; global-scope plugin agents always
   load.

Names are deduped by qualified name with the highest-precedence root winning,
so a project file can override a global or bundled agent of the same name — or
a built-in. A malformed file (no frontmatter, bad YAML, missing description,
name/stem mismatch, illegal name) is silently skipped, never raised into a
turn.

### Frontmatter fields

The file **stem is the agent's identity**. Recognized keys:

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | no | If present, must equal the file stem (a mismatch invalidates the file). |
| `description` | **yes** | Shown in the spawnable-agents index the model sees; non-empty string. |
| `tools` | no | Tool names as a comma string or YAML list; unknown names are dropped. Absent/empty ⇒ the read-only set. |
| `backend` | no | `native` (default, in-process Pydantic AI loop) or `claude-cli`. |
| `model` | no | Backend-specific default model. For `claude-cli`, a Claude Code model name passed verbatim to `--model`; ignored by the native backend. |
| `tier` | no | `cheap`, `med`, or `high` — the spec's tier label for the native model router. Unknown values normalize to unset. |
| `thinking` (alias `effort`) | no | Reasoning-effort level (`off`/`minimal`/`low`/`medium`/`high`/`xhigh`). Unknown values degrade to "inherit". |

Everything after the frontmatter is the system prompt. Example:

```markdown
---
description: Reviews a diff for correctness bugs. Read-only.
tools: read_file, grep, glob, tree
tier: med
thinking: medium
---
You are a code-review sub-agent. Review the diff you are pointed at...
```

## Tool reach

A sub-agent's tools are granted **entirely up front** and registered *plain* —
no mid-run approval rounds, ever. What a spawn actually gets is its spec's
`tools:` set intersected with the session's current approval mode at spawn
time:

- **Mutating (gated) tools** — `write_file`, `edit_file`, `bash` — are granted
  only in `auto` mode. In `ask` or `plan` mode they are stripped, since a
  sub-agent has no way to prompt you per call.
- **Network tools** — `web_search`, `fetch_url` — are stripped in `plan` mode,
  mirroring the main agent's plan-mode egress denial.
- **MCP grants** follow the same doctrine: full grant in `auto`; in `ask`
  mode, servers whose calls would prompt per-call are withheld (and the
  spawner is told why); in `plan` mode the whole grant is withheld.

The grantable universe is: the read tools (`read_file`, `glob`, `grep`,
`tree`, plus the six LSP navigation tools), the network tools, and the gated
tools. Memory, skills, tasks, and forge tools are main-agent only.

**Nesting is bounded, not forbidden.** `spawn_agent` itself is granted to a
sub-agent only when the child could still spawn within the depth ceiling
(`SUBAGENT_MAX_DEPTH`, default 3: main agent at depth 0, sub-agents at 1,
grandchildren at 2). At the leaf depth the tool is simply absent, and the
ceiling also rides on the spawn's deps — never on a tool parameter the model
could override — so an over-deep spawn is refused even if attempted.

## Model tiers

Native spawns pick their model by **tier** — `cheap`, `med`, or `high` — so
read-only fan-out can run on a small cheap model while hard mutating work gets
a strong one. Configure the tier models (qualified `provider:model_id`):

```bash
MARIM_SUBAGENT_TIER_CHEAP=openrouter:some/cheap-model
MARIM_SUBAGENT_TIER_MED=...
MARIM_SUBAGENT_TIER_HIGH=...
```

A spawn's tier is resolved, highest precedence first:

1. the spawner's `tier=` argument on `spawn_agent`,
2. the spec's `tier:` frontmatter,
3. the tool-reach default: read-only specs (no gated tools) → `cheap`,
   workspace-mutating specs → `high`. (`med` is opt-in only — reachable via
   an override or a spec label, never from tool reach.)

The resolved tier maps to its `MARIM_SUBAGENT_TIER_*` model; a tier with no
model configured — and every tier when `MARIM_SUBAGENT_TIERING=0` turns the
master switch off — **inherits the main model**, so an unconfigured install
is unchanged and `tier` is always safe to pass. Disabling the switch keeps
the curated slugs, so it round-trips as a toggle (also available in TUI
Settings).

`model=` on `spawn_agent` is the escape hatch: an exact model id for one
spawn. With no tiers configured it passes through as-is; once any tier is
configured it is bounded to the set of configured tier models — an
out-of-allowlist slug falls back to the tier-resolved model. An unknown value
in the `tier` slot likewise degrades to the next resolution level rather than
failing the spawn.

**Thinking level** resolves independently, same shape: the spawn-call
`thinking=` override → the spec's `thinking:`/`effort:` → the inherited
session level (`/think`). A resolved `off` (or nothing anywhere) leaves the
spawn's model settings untouched.

## The claude-cli backend

A spec with `backend: claude-cli` runs its spawns as an external
`claude -p` (Claude Code CLI) process in headless stream-json mode instead of
the in-process loop — useful for delegating to a Claude subscription. Around
the swapped engine, the harness wrapping is identical: same worktree
isolation, hooks bracketing, output cap, transcript persistence, and
background handling.

Differences that matter:

- `model:` in the spec (or `model=` on the spawn) is a Claude Code model name
  (`sonnet`, `opus`, or a full id), passed straight to `--model`;
  `MARIM_CLAUDE_CLI_MODEL` is the env default. **Tiers do not apply** to
  claude-cli spawns.
- An interrupted claude-cli spawn resumes through the CLI's own `--resume`:
  the CLI session id is checkpointed into the spawn's sidecar meta, and marim
  hands it back rather than replaying its own transcript (the CLI owns its
  history).
- Claude's own Agent/Task sub-agents are demuxed out of the stream and
  rendered as first-class cards in the sub-agents screen, nested under their
  spawn.
- One spawn is bounded by a wall-clock ceiling, `MARIM_CLAUDE_CLI_TIMEOUT`
  (default 600 s); `MARIM_CLAUDE_CLI_BIN` picks the executable.

## Limits and operations

Operational knobs, briefly (full table in
[reference/configuration.md](../reference/configuration.md)):

- `MARIM_SUBAGENT_CONCURRENCY` (default 8) — how many spawns may run their
  model loop at once; excess queues instead of slamming a rate-limited route.
  `0`/negative means unbounded.
- `MARIM_SUBAGENT_REQUEST_LIMIT` (default 50) — max model requests one spawn
  may make before it is aborted; bounds a runaway sub-agent.
- `MARIM_SUBAGENT_TRANSCRIPT_CAP` (default 2000) — the persisted sidecar
  transcript size per spawn.

In the TUI, `/jobs` lists, prints, and cancels background jobs (and toggles
wake), and `ctrl+x` opens the **sub-agents screen** — every spawn this
session as a master list with a live transcript pane, where interrupted
spawns can be resumed. See [guides/tui.md](tui.md).

Two containment behaviors worth knowing:

- **Failure containment.** A crashed foreground spawn is returned to the
  spawner as an error string, so one failing member of a fan-out never takes
  down its siblings. A crashed background spawn marks its job failed, and the
  failure surfaces in the digest.
- **Context masking.** Each spawn gets its own observation masker: a
  sub-agent's history is dominated by short-lived tool output (file reads,
  grep dumps), and past a token trigger the stale payloads are swapped for a
  placeholder on outgoing requests — the model keeps the trace of what it did
  and can re-run a tool if it still needs the data. State is per spawn, so
  one run's mask set never leaks into another's, and the request prefix stays
  cache-stable between trigger events. A spawn that overflows even after
  masking is reported with an actionable "split the task" message.
