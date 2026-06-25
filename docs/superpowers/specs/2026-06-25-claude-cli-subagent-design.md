# Claude Code CLI as a Sub-Agent Backend — Design

**Date:** 2026-06-25
**Status:** Proposed (design)

## Problem

Every sub-agent today is an in-process Pydantic AI `Agent` built by
`SubagentRunner.build` (`subagents.py:224`) and run via `_run_to_completion`
(`subagents.py:309`). That couples a sub-agent's reach to the harness's own tool
implementations, MCP grants, and provider model. There is no way to delegate a
sub-task to an *external* agent — specifically the Claude Code CLI (`claude`),
which brings its own tools, permission model, hooks, and MCP.

We want to spawn `claude -p` as a sub-agent: hand it a task and a working
directory, stream its activity to the UI, and get its final report back — without
re-implementing or bridging any of its tools.

## Goal

Let an authored sub-agent declare `backend: claude-cli` in its `.md` frontmatter
and, when spawned, run the Claude Code CLI in headless mode instead of the
Pydantic AI loop. Reuse the entire spawn lifecycle around the model call
(worktree, hooks bracketing, output cap/spill, stream plumbing, return-string
contract); swap only the **build + run** middle.

## The seam

`_execute_spawn` (`subagents.py:367`) is already a clean pipeline:

```
worktree open → build → MCP grant → hooks.subagent_start → run
→ hooks.subagent_stop → usage → cap output → worktree close
```

Only **build + run** is Pydantic-AI-specific. The branch is a single conditional
right after the agent definition is resolved:

```
if defn.backend == "claude-cli":  → ClaudeCliRunner.run(...)
else:                              → build() + _run_to_completion()  (unchanged)
```

Everything wrapping that branch stays shared. The CLI path lives in a new focused
module, `subagents_cli.py`, holding `ClaudeCliRunner` plus pure helpers (argv
construction, tool-name mapping, permission-mode selection, stream parsing) that
are unit-tested without a subprocess.

## Decisions

- **Declaration via frontmatter.** `AgentDef` gains `backend: str = "native"` and
  `model: str | None = None`, both read by `_parse_agent`
  (`workspace/agents.py:106`). `claude-cli` is the only non-native value for now.
  A normal authored agent `.md` — its body is appended to the CLI's system prompt
  via `--append-system-prompt`. Discovery, precedence, naming, dedup all reuse the
  existing machinery. (Rejected: a single hardcoded built-in type — loses
  per-role prompt/model; a global backend toggle — too broad, conflates "use CLI"
  with "which role".)

- **Permissions mirror native gating.** `effective_tools(defn, allow_gated = mode
  is Mode.auto)` (`workspace/agents.py:238`) already drops `GATED_TOOLS` outside
  `auto`. We translate the resulting name-set into CLI flags rather than passing it
  to a Pydantic AI provider. Reach is decided up front, exactly like native —
  `claude -p` cannot prompt mid-run, so anything not pre-authorized is simply
  unavailable. (Rejected: always-autonomous `--dangerously-skip-permissions` —
  breaks the mode contract; always read-only — loses the autonomous-worker case.)

- **Opt-in worktree isolation.** A CLI agent honors the existing
  `isolation="worktree"` spawn argument and nothing more — same as the native
  path, for consistency. No auto-isolation despite the larger blast radius; the
  caller decides. (See *Trust* below for the tradeoff being accepted.)

- **Full event streaming, with a notice fallback.** Run with `--output-format
  stream-json --verbose`; map CLI events onto the Pydantic-AI event vocabulary the
  TUI already renders, tagged with `stream_id`. If full-fidelity mapping proves
  too costly, fall back to textual progress via `on_subagent_notice`. Which one is
  decided during the implementation plan, after probing the TUI render path.

- **Model is backend-dependent, passed through unvalidated.** For a `claude-cli`
  agent the `model` value is a Claude Code model name (`opus`/`sonnet`/`haiku`/
  `fable` alias, or a full `claude-…` id) handed straight to `--model`. We **skip**
  `_build_model` (which resolves OpenRouter-style ids). There is no `claude models
  list` command, so we do not enumerate or validate — an unknown name surfaces the
  CLI's own error. Known aliases are documented as hints in the `spawn_agent`
  docstring and agents index, not enforced.

- **No transient-retry/resume.** The resume-on-transient-error machinery
  (`_run_to_completion`) is Pydantic-AI-specific. A CLI failure is reported, not
  auto-resumed (v1).

## The CLI runner

`ClaudeCliRunner.run(defn, task, cwd, mode, stream_id, model, on_event) ->
CliResult` does:

1. **Locate binary.** `claude` on PATH, overridable via `MARIM_CLAUDE_CLI_BIN`.
   Missing → a clear error string through the existing contained-error path (no
   crash).
2. **Build argv** (pure helper, unit-tested):
   `claude -p <task> --output-format stream-json --verbose
   --append-system-prompt <defn.prompt> --cwd <cwd>` plus the permission/model
   flags below.
3. **Spawn** via `asyncio.create_subprocess_exec` (the primitive
   `hooks/runner.py` already uses), reading stdout line-by-line as NDJSON.
4. **Translate** each event and forward it via `on_event` (→ `on_subagent_event`
   or `on_subagent_notice`).
5. **Return** `CliResult(text, usage)` from the terminal `result` event.

`cwd` is the worktree path when isolated, else `workspace_root` — set by
`_execute_spawn` exactly as it sets `work_root` for the native path.

## Permission mapping

| Harness mode | `--permission-mode` | `--allowedTools` |
|---|---|---|
| `auto`       | `acceptEdits`       | granted set → CC tool names (e.g. `Read,Edit,Write,Bash`) |
| `ask`/`plan` | `plan`              | read-only subset (e.g. `Read,Grep,Glob,WebFetch`) |

A small mapping table (harness tool names → Claude Code tool names) lives beside
the runner and is unit-tested directly.

## Model resolution (precedence, first hit wins)

1. Per-spawn `model` arg on `spawn_agent` → `--model <value>`.
2. `model:` frontmatter on the agent `.md`.
3. `MARIM_CLAUDE_CLI_MODEL` env.
4. Omit `--model` → the CLI's own configured default.

## Streaming → UI

The CLI's `stream-json` emits `{type:"assistant"…}`, `{type:"user"…}` (tool
results), and a terminal `{type:"result"…}`. The translation layer maps these onto
the event objects `on_subagent_event(stream_id, event, usage)` already renders
(text deltas, tool-call start/return), so a CLI spawn nests under its spawn card
like a native sub-agent. Fallback: low-fidelity textual lines via
`on_subagent_notice`.

## Output, billing & usage

- Final text flows through the existing `_cap_output` / spill path unchanged.
- **Caveat — separate billing.** The CLI authenticates and bills through *its own*
  config (your Claude subscription or `ANTHROPIC_API_KEY`), **not** the harness's
  OpenRouter provider. Its dollar cost hits a different account. The `result` event
  reports token counts and `total_cost_usd`; we best-effort fold the token counts
  into a synthesized `Usage` so the turn's token line isn't blind to the spawn, and
  note that the spend is out-of-band.

## Trust — the real semantic difference

A native sub-agent cannot recurse and runs the harness's exact gated tools under
its hooks engine. A CLI sub-agent is a **full Claude Code**: its own Task tool (it
can spawn its own sub-agents), its own hooks engine, its own MCP. Consequences,
accepted deliberately:

- The harness Pre/PostToolUse hooks **do not fire** for the CLI's internal tool
  calls — guardrails on delegated work do not extend inside it. We still bracket
  the spawn with `subagent_start` / `subagent_stop`.
- Its blast radius is larger. Containment is the caller's choice via
  `isolation="worktree"` (opt-in) and the `--permission-mode` / `--allowedTools`
  derived from harness mode. This difference is documented on the `backend`
  frontmatter field so no one assumes a `claude-cli` agent respects the same gates
  as a native one.

## Error handling

Contained-error parity with the native path: missing binary, non-zero exit, or
an unparseable stream each return a descriptive string (foreground) or propagate
to the job registry (background). No new failure mode reaches `run_turn`. On an
exceptional/cancelled exit (e.g. a Ctrl-C'd turn), `ClaudeCliRunner.run` reaps the
subprocess in a `finally` so an abandoned `claude` child — which in auto mode holds
write/Bash reach — is not orphaned.

No watchdog timeout in v1 (see Out of scope): a hung CLI with stdout still open
blocks the spawn until the turn is cancelled, at which point the `finally` kills
the child. A bounded timeout is deferred to v2.

## Testing

- **Pure helpers, no subprocess:** argv construction, tool-name mapping,
  permission-mode selection, and `stream-json` event parsing are unit-tested
  directly — matching the repo's "pure helpers tested directly, thin I/O wiring
  separate" split.
- **One integration test:** a fake `claude` script (pointed to via
  `MARIM_CLAUDE_CLI_BIN`) emits canned `stream-json`, verifying the end-to-end
  spawn → parse → return path and that the spawn lifecycle (hooks bracketing,
  output cap, worktree) wraps it correctly.

## Out of scope (v1)

- Transient-error auto-resume for CLI spawns.
- A bounded watchdog timeout on a hung CLI (cancellation reaps it instead; v2).
- Auto-isolation of writable CLI agents.
- Runtime model enumeration / validation.
- Granting harness MCP servers *into* the CLI (it uses its own MCP config).
- Bridging the harness's exact tool implementations into the CLI (it uses its
  own).
