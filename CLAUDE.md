# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`marim-harness` is a terminal coding agent built on [Pydantic AI](https://ai.pydantic.dev/)
and [Textual](https://textual.textualize.io/). It reads/searches/edits files and runs
commands in a workspace, with an interactive TUI and a headless one-shot mode. The
console scripts are `marim` and `marim-harness` (both → `marim_harness.__main__:main`).

## Commands

```bash
uv sync                          # install deps (creates .venv)
uv run pytest                    # full test suite (coverage is on by default via pyproject)
uv run pytest tests/test_agent.py            # one file
uv run pytest tests/test_agent.py::test_name # one test
uv run pytest --no-cov tests/test_x.py       # skip coverage for a fast single run
uv run ruff check src tests      # lint
uv run ruff check --fix src tests
uv run pyright                   # type-check (standard mode, src only)
uv run marim serve --port 8642   # HTTP daemon (REST + SSE); needs the [serve] extra
```

CI (`.gitea/workflows/ci.yml`) runs ruff → pyright → pytest on Python 3.10, 3.12,
and 3.14 (plus a `uv build` packaging check on the 3.12 leg). Match that order
locally before claiming work is done. `requires-python` is `>=3.10`, so avoid
3.11+ only syntax.

Set `MARIM_DEBUG=1` for DEBUG logging. Provider config lives in env vars / `.env`
(see `.env.example`): `MARIM_PROVIDER` (`openrouter`|`local`|`google`|`claude-cli`), `MARIM_MODEL`,
`OPENROUTER_API_KEY`, etc. Default provider is OpenRouter, default model
`anthropic/claude-sonnet-4-6`. `claude-cli` delegates each turn to the `claude` CLI on a
Claude subscription — marim acts as a launcher (Claude runs its own tools/loop), so marim's
own tools/approval/LSP/MCP do not apply in that provider. Claude's own Agent/Task
sub-agents, however, are demuxed out of the stream (`subagents/cli_demux.py`) and
rendered as first-class cards in the sub-agents screen, for both the main-loop
provider and `backend: claude-cli` spawns. Interrupted `claude-cli` spawns
resume via the CLI's own `--resume` (the session id is checkpointed in the
spawn's sidecar meta).

## Architecture

The dependency flow is **`__main__` → `interfaces/cli/router` → `default_cmd` →
`runtime/bootstrap.py`'s `build_harness` → `Harness`**. The two front-ends (TUI and
headless) both go through `build_harness`, so they wire up models, sessions, MCP,
hooks, and LSP identically — keep new wiring there, not duplicated per interface.

The turn-execution engine lives in the **`runtime/`** package: `harness.py`
(`Harness`, `build_collaborators`, `build_services`), `controller.py`
(`TurnController`, the approval/persist loop), `context.py` (per-turn context
helpers), `deps.py`, `permissions.py` (`Mode`), `errors.py`, `instructions.py`, and
`bootstrap.py`. Imports target submodules directly (`from .deps import Deps`); the
package root deliberately re-exports nothing, keeping the deps/services cycle below
from leaking through `__init__` at import time.

### The core turn loop (`runtime/harness.py`)

`Harness` owns the Pydantic AI `Agent` and drives one user turn to completion.
`Harness.run_turn` → `_run_with_approval` is the heart of the system. Key invariants
encoded there (read the docstrings before touching):

- **Approval rounds.** The agent's `output_type` is `[str, DeferredToolRequests]`.
  Gated tools (`write_file`, `edit_file`, `bash`) defer; `_run_with_approval` loops,
  resolving each deferred batch via `resolve_approvals` against the current `Mode`
  (`auto`/`ask`/`plan`), then continues the run with the results.
- **Resumability.** A persisted history must never end with a `ToolCallPart` lacking
  its `ToolReturnPart` — every provider rejects that on the next request.
  `_repair_unanswered_tool_calls` self-heals such histories; an aborted turn is
  flushed via `_flush_resumable` (with a tight deadline so Ctrl-C stays snappy).
  The dirty history held during an approval round is deliberately **not** persisted —
  `resumable` is the rollback baseline and is refreshed only after a clean persist.
- **Prompt assembly.** `_assemble_prompt` prepends per-turn context (task checklist,
  finished-job digests, the prior turn's actionable error note, SessionStart /
  UserPromptSubmit hook output) and wraps the injected prefix in a `<turn-context>`
  envelope so a resumed session can recover just what the user typed
  (`strip_turn_context`). The system prompt is kept stable to preserve prompt caching.
- **Error notes.** `_actionable_error_note` surfaces only failures the *model* can act
  on (4xx client errors, usage limits, malformed responses) and stays silent on infra
  (429/5xx), cancels, and render bugs. Full provider payloads spill to
  `.marim/last-provider-error.json`.

### Construction & the deps/services cycle

`build_collaborators` (in `runtime/harness.py`) builds the whole collaborator graph
(agent, MCP, LSP, session, checkpoints, hooks, subagents) in dependency order. There
is one unavoidable late binding: `Deps` and `HarnessServices` form a reference cycle — `TurnHooks` and the
sub-agent runners hold `deps`, while tools reach back through `ctx.deps.services`.
`build_services` performs that single binding. `HarnessConfig` bundles all optional
knobs; `Harness.__init__` still accepts legacy `**kwargs` as a shorthand for
building one (pass `config=` *or* kwargs, not both — mixing raises `TypeError`).

`Deps` (`runtime/deps.py`) is the `RunContext` payload threaded through every tool. All
UI-facing collaboration is **optional callbacks** that headless leaves as `None`
(each reader guards with `is None`). The TUI wires them in one place via
`Harness.bind_ui` — don't poke `harness.deps`/`harness.session` field-by-field from
the interface layer.

### Tools (`tools/`)

Tool implementations are module-level functions in `tools/provider.py` so they can be
registered two ways from one source of truth: onto the main agent (gated tools behind
`requires_approval=True`) and onto sub-agents (registered *plain* — reach is decided
up front by which names are granted, never by mid-run prompting). `spawn_agent`
is granted to a sub-agent only when it could still nest within the depth ceiling
(`depth + 1 < SUBAGENT_MAX_DEPTH`, default 3) — see `SubagentRunner.build`; at
the leaf depth the tool is absent, so nesting is bounded, not forbidden. Nested
spawns render in the sub-agents screen as an indented tree (a child card streams
into its parent's transcript pane). Tool docstrings are the model-facing tool
descriptions — they are part of the product; write them with that in mind.
`names.py` is the leaf module holding tool-name sets (`GATED_TOOLS`, `LSP_TOOLS`, etc.)
to avoid import cycles.

### Supporting subsystems (one concern each)

- `session/` — `SessionStore` (persists to `$XDG_DATA_HOME/marim-harness/sessions`),
  `SessionManager`, `SessionController` (compaction/autoname), `CheckpointManager`
  (rewind via `GitSnapshotter`, honoring `.gitignore`).
- `mcp/` — Model Context Protocol server config + lifecycle; servers can be granted
  selectively to sub-agents. Project-local `.marim/mcp.json` servers launch code on
  connect, so they load only when the project is trusted (the same
  `MARIM_TRUST_PROJECT_HOOKS` gate as project hooks); global/plugin servers always load.
- `lsp/` — multilspy-backed language-server pool. Two independent switches:
  `lsp_enabled` (the manager + diagnostics-on-edit) and `lsp_tools_enabled` (the six
  navigation tools). Diagnostics are appended to write/edit results best-effort.
- `hooks/` — Claude-Code-compatible lifecycle hook engine (session/prompt/tool/
  compaction events). Observe-only except SessionStart/UserPromptSubmit, which inject
  context. Project-local hooks run only when trusted (`MARIM_TRUST_PROJECT_HOOKS`).
- `plugins/` — bundles skills + sub-agents + hooks + MCP + `AGENTS.md`; hooks/MCP load
  only for *trusted* plugins. Namespaced `plugin:item`. See `docs/plugins.md`.
- `workspace/` — fs primitives, memory (`remember`/`recall`), skills, sub-agent specs,
  git worktrees, snapshots. (The root-level `compaction.py` builds the
  summarizer/titler aux agents and the token-budget compaction helpers.)
- `subagents/` — `runner.py` (`SubagentRunner`: spawns and drives isolated
  sub-agents), `masking.py` (per-spawn context masking of stale tool
  observations), and `cli_backend.py` (the optional `claude -p` CLI backend it
  delegates to). Re-exported as `marim_harness.subagents.SubagentRunner`.
- `interfaces/tui/` — Textual app, widgets, `styles.tcss`, approval/ask-user modals,
  streaming render. `interfaces/cli/` — router + per-command modules (lazily imported
  so `config`/`models` don't pay for `pydantic_ai`).

## Conventions

- Use `uv` for everything (`uv run …`, `uv sync`). Don't invoke `pip` or a bare
  `python`/`pytest`.
- Ruff line length is 100; lint set is `E,F,I,UP,B,SIM` (import sorting enforced;
  pyupgrade, bugbear, and flake8-simplify also on).
- Pure helpers (fs ops, command policy, snapshots) are kept side-effect-free and
  unit-tested directly; the I/O wiring lives in the thin tool/interface layer. Follow
  that split when adding behavior.
- The codebase favors long, explanatory comments on *why* a non-obvious invariant
  holds (especially around resumability and the deps/services cycle). Preserve them
  when editing nearby code.
- Follow `coding-guidelines.md` for code design principles: control complexity,
  prefer straight-line flow, model the domain when it pays off, encapsulate
  collections with behavior, limit deep navigation, name for clarity, optimize for
  cohesion, treat large state as a smell, and encapsulate behavior over data.
  It's guidance, not dogma — break a rule when the tradeoff is clear and
  document why.
