# Architecture

A map of how marim-harness is put together, and the invariants you need to
know before changing the core. For hands-on embedding docs see
[`embedding.md`](embedding.md) and [`sdk/`](sdk/README.md).

## The big picture

The dependency flow is:

```
__main__ → interfaces/cli/router → default_cmd
        → runtime/bootstrap.py (build_harness) → Harness
```

The two front-ends — the Textual TUI and the headless one-shot mode — both go
through `build_harness`, so they wire up models, sessions, MCP, hooks, and LSP
identically. New wiring belongs there, not duplicated per interface.

The turn-execution engine lives in the **`runtime/`** package:

| Module | Responsibility |
| --- | --- |
| `harness.py` | `Harness`, `build_collaborators`, `build_services` — owns the Pydantic AI `Agent`, drives one user turn to completion |
| `controller.py` | `TurnController` — the approval/persist loop |
| `context.py` | Per-turn context assembly helpers |
| `deps.py` | `Deps`, the `RunContext` payload threaded through every tool |
| `permissions.py` | `Mode` (`auto`/`ask`/`plan`) and approval resolution |
| `builder.py` | `HarnessBuilder` — the embedding front door: explicit model and tool/session/sub-agent composition, no `MARIM_*` env reads |
| `bootstrap.py` | The CLI preset built on the builder: env config, workspace scanning, TUI/headless wiring |
| `errors.py`, `instructions.py` | Error classification; system-prompt assembly |

Imports target submodules directly (`from .deps import Deps`); the package
root deliberately re-exports nothing, keeping the deps/services cycle (below)
from leaking through `__init__` at import time.

## The core turn loop

`Harness.run_turn` → `_run_with_approval` is the heart of the system. Key
invariants encoded there (read the docstrings before touching):

- **Approval rounds.** The agent's `output_type` is
  `[str, DeferredToolRequests]`. Gated tools (`write_file`, `edit_file`,
  `bash`) defer; `_run_with_approval` loops, resolving each deferred batch
  against the current `Mode`, then continues the run with the results.
- **Resumability.** A persisted history must never end with a `ToolCallPart`
  lacking its `ToolReturnPart` — every provider rejects that on the next
  request. `_repair_unanswered_tool_calls` self-heals such histories; an
  aborted turn is flushed via `_flush_resumable` (with a tight deadline so
  Ctrl-C stays snappy). The dirty history held during an approval round is
  deliberately **not** persisted — `resumable` is the rollback baseline and is
  refreshed only after a clean persist.
- **Prompt assembly.** `_assemble_prompt` prepends per-turn context (task
  checklist, finished-job digests, the prior turn's actionable error note,
  hook output) and wraps the injected prefix in a `<turn-context>` envelope so
  a resumed session can recover just what the user typed. The system prompt is
  kept stable to preserve prompt caching.
- **Error notes.** `_actionable_error_note` surfaces only failures the *model*
  can act on (4xx client errors, usage limits, malformed responses) and stays
  silent on infra errors (429/5xx), cancels, and render bugs. Full provider
  payloads spill to `.marim/last-provider-error.json`.

## Construction and the deps/services cycle

`build_collaborators` builds the whole collaborator graph (agent, MCP, LSP,
session, checkpoints, hooks, sub-agents) in dependency order. There is one
unavoidable late binding: `Deps` and `HarnessServices` form a reference
cycle — `TurnHooks` and the sub-agent runners hold `deps`, while tools reach
back through `ctx.deps.services`. `build_services` performs that single
binding.

`Deps` is the `RunContext` payload threaded through every tool. All UI-facing
collaboration is **optional callbacks** that headless leaves as `None` (each
reader guards with `is None`). The TUI wires them in one place via
`Harness.bind_ui` — interface code never pokes `harness.deps` field-by-field.

## Tools

Tool implementations are module-level functions in `tools/provider.py` so they
can be registered two ways from one source of truth: onto the main agent
(gated tools behind `requires_approval=True`) and onto sub-agents *plain* —
a sub-agent's reach is decided up front by which tool names it is granted,
never by mid-run prompting.

The layering is a deliberate three-way split:

1. **Pure helpers** (command policy, snapshot diffing, arg coercion,
   path-guard resolution) are side-effect-free and unit-tested directly.
2. **Effectful I/O** lives in `tools/impl/` (`fs.py` writes, `shell.py`
   spawns, `fetch.py` opens sockets) — the real I/O core, exercised against a
   tmp workspace.
3. **The tool layer** (`fs_tools.py`, `edit_tools.py`, `net_tools.py`) is thin
   `ctx.deps`-unwrapping wiring.

Tool docstrings are the model-facing tool descriptions — they are part of the
product. `names.py` is the leaf module holding tool-name sets to avoid import
cycles.

## Supporting subsystems (one concern each)

- **`session/`** — `SessionStore` (persists to
  `$XDG_DATA_HOME/marim-harness/sessions`), `SessionManager`,
  `SessionController` (compaction/autoname), `CheckpointManager` (rewind via
  `GitSnapshotter`, honoring `.gitignore`).
- **`mcp/`** — MCP server config + lifecycle; servers can be granted
  selectively to sub-agents. Project-local servers load only when the project
  is trusted (`MARIM_TRUST_PROJECT_HOOKS`).
- **`lsp/`** — multilspy-backed language servers assembled from LSP providers
  (`provider.py`). Four bundled language plugins (python, typescript, cpp,
  java) always load; third-party plugins add languages declaratively under the
  trust gate. See [`lsp-plugins.md`](lsp-plugins.md).
- **`forge/`** — Gitea/GitHub integration behind a `ForgeBackend` seam;
  `TeaBackend` shells out to the `tea` CLI.
- **`hooks/`** — Claude-Code-compatible lifecycle hook engine. Observe-only
  except SessionStart/UserPromptSubmit (inject context) and PreCompact (may
  block a manual compact).
- **`plugins/`** — bundles skills + sub-agents + hooks + MCP + `AGENTS.md`;
  hooks/MCP load only for trusted plugins. See [`plugins.md`](plugins.md).
- **`workspace/`** — fs primitives, memory, skills, sub-agent specs, git
  worktrees, snapshots, and the per-session scratchpad.
- **`subagents/`** — spawn lifecycle (`runner.py`), model-loop recovery
  (`run_driver.py`), context masking, model tiers (`tiers.py`), and the
  optional `claude -p` CLI backend.
- **`workflows/`** — the gated `run_workflow` tool executes model-authored
  Python in a pydantic-monty sandbox; never cancel the Monty VM task — aborts
  flow through host functions (see `engine.py`'s module docstring).
- **`interfaces/tui/`** — Textual app, widgets, streaming render, inline
  interaction panels (mounted above the status bar, not modals, so the
  transcript stays scrollable). **`interfaces/cli/`** — router + per-command
  modules, lazily imported so light subcommands don't pay for `pydantic_ai`.
