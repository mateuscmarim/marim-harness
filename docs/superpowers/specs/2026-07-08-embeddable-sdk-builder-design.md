# Embeddable SDK: HarnessBuilder — Design

**Date:** 2026-07-08
**Status:** Approved (brainstorming session)

## Goal

Make marim-harness easy to embed in other Python projects as an agent SDK:
`pip install marim-harness`, then build a harness programmatically — explicit
model, composable subsystems, custom tools/instructions/MCP/sub-agents from
code — with no hidden env or XDG state.

## Scope & phasing

Two independently shippable sub-projects, spec'd together so the API stays
coherent:

1. **Phase 1 — the builder/composition layer** (`HarnessBuilder`). This design's
   implementation target.
2. **Phase 2 — the event-stream runtime API** (`stream_turn`). Shape fixed
   here; implemented after the builder ships.

Chosen approach (over capability-object plugins and a thin facade): a fluent
builder that re-fronts the existing construction seams. `build_harness` is
refactored to drive the builder ("the CLI preset"), so the CLI/TUI dogfood the
SDK and the paths cannot drift. A `Capability` protocol can be introduced
*behind* the builder later without changing the builder API, if third-party
capability authors ever materialize.

## Non-goals

- Per-tool override of marim's builtin implementations (composition is at
  subsystem granularity; custom tools are *additive*).
- Runtime model switching in the SDK (`MultiModelSource` stays a CLI concern;
  an embedder may pass a `model_source` explicitly).
- `claude-cli` provider through the builder — it is a launcher that bypasses
  marim's tools/approval entirely, meaningless when embedding.
  `bootstrap.build_harness` keeps handling `_wire_cli_model` after calling the
  builder.
- Migrating the TUI onto `stream_turn` (possible later cleanup).

## Public API

### Entry point

`HarnessBuilder` lives in `runtime/builder.py`, re-exported as
`marim_harness.HarnessBuilder` via a lazy `__getattr__` on the package root so
that importing `marim_harness` for config/CLI purposes still does not pay for
`pydantic_ai` (same principle as the lazily imported CLI command modules).

```python
from marim_harness import HarnessBuilder

harness = (
    HarnessBuilder(workspace=Path("./repo"), model="anthropic:claude-sonnet-4-6")
    .with_bash(policy=CommandPolicy(denylist=["rm -rf"]))
    .with_lsp(tools=True)
    .with_mcp_server(server_spec)
    .with_tool(deploy, requires_approval=True)
    .with_subagent(SubagentSpec(name="reviewer", instructions=..., tools=[...]))
    .with_instructions(extra="You are the CI fixer for acme-app.")
    .with_sessions()                      # opt-in persistence
    .with_mode(Mode.AUTO)
    .build()
)
result = await harness.run_turn("fix the failing test")
```

### Model

`model` accepts a pydantic-ai `Model` instance or a plain string id. String ids
resolve through **pydantic-ai's own conventions** (its env-var API keys) — the
SDK never reads `MARIM_*`. Marim provider config remains a CLI concern.

### Defaults for a bare `.build()`

- Tools: read-only file tools (`read_file`, `glob`, `grep`, `tree`) plus gated
  `write_file`/`edit_file`. Nothing else — no bash, net, LSP, MCP, forge,
  sub-agents, memory, skills, tasks, or jobs.
- Mode: `auto`.
- Session: in-memory; nothing written to XDG.
- Rationale: a coding-agent SDK with zero file tools is useless, but everything
  with reach (shell, network, LSP servers, spawning) is opt-in.
- `.with_defaults()` preset turns on the full marim experience in one call
  (including workspace scanning for hooks/MCP/skills, i.e. the discovery
  behavior the SDK otherwise never performs).

### Sessions

`.with_sessions(dir: Path | None = None)` opts into persistence; `dir`
overrides the storage root (default: today's XDG location). Not calling it
means in-memory (`store=None`, already supported by `Harness`).
Summarizer/titler aux agents are built automatically from the main model (as
`build_harness` does today), overridable via builder knobs.

### Build result

`build()` returns the existing `Harness` — no wrapper type. The builder is pure
construction; the invariant-heavy core (`_run_with_approval`, resumability,
compaction) is reused untouched.

## Subsystem composition — the provider refactor

`BuiltinToolProvider.register()` is currently all-or-nothing (only
`register_lsp_tools` and `combined_job_tool` knobs). It grows a `ToolGroups`
config — a small frozen dataclass of booleans — describing which groups to
register:

| Group | Tools | Gated | Bare-build default |
|---|---|---|---|
| `files_read` | read_file, glob, grep, tree | no | **on** |
| `files_write` | write_file, edit_file | yes | **on** |
| `bash` | bash | yes | off |
| `net` | outbound network tools | yes | off |
| `memory` | remember / recall | no | off |
| `skills` | skill tools | no | off |
| `tasks` | task checklist tools | no | off |
| `jobs` | job tools (or combined `job`) | no | off |
| `spawn` | spawn_agent + sub-agent infra | no | off |

`register()` becomes `if self._groups.x:` blocks over the same registration
calls that exist today — mechanical, no tool implementation changes. Group
boundaries follow `names.py`'s existing sets (`GATED_TOOLS`, `NET_TOOLS`,
`LSP_TOOLS`, …), which remain the source of truth; a test asserts every
registered tool name belongs to exactly one group so they cannot drift.

Sub-agent tool granting already works by name, so it composes with groups for
free; `build()` validates that a sub-agent spec is not granted tools from
unloaded groups.

### Builder → seam mapping

Each `with_*` method maps to one existing seam — no new subsystem code:

| Builder call | Wires |
|---|---|
| `.with_bash(policy=…)` | bash group on + `CommandPolicy` into `WorkspaceConfig` |
| `.with_lsp(tools=…)` | `lsp_enabled` (+ `register_lsp_tools`) |
| `.with_mcp_server(x)` | appends to `HarnessConfig.mcp_servers`; accepts a marim spec **or** a ready pydantic-ai MCP server object |
| `.with_forge(backend=…)` | `forge_enabled` + explicit backend, skipping tea auto-detection |
| `.with_subagent(spec)` | spawn group on + spec registered alongside workspace-discovered ones |
| `.with_tool(fn, requires_approval=…)` | registered after builtins via the same `agent.tool` path gated builtins use |
| `.with_hooks(runner)` | `Deps.hooks` — programmatic only; no directory scanning unless `.with_defaults()` |
| `.with_instructions(extra=…)` / `(replace=…)` | system prompt extension/replacement |
| `.with_mode(mode)` | initial approval `Mode` |

Custom tools participate in the approval loop identically to gated builtins:
`requires_approval=True` routes them through `resolve_approvals`, so `ask`
prompts (via callback), `plan` denies, `auto` runs — embedders get the whole
permission model for free. A custom tool name colliding with a loaded builtin
is a `build()`-time error.

## Dogfooding

`bootstrap.build_harness` is refactored to read env config and drive the
builder. Its env/XDG behavior is unchanged — it becomes "the CLI preset" and
the standing proof that the builder can express everything the CLI needs.

## Phase 2 — event-stream runtime API (shape only)

```python
async for event in harness.stream_turn(prompt):
    match event:
        case ToolCallEvent(): ...        # tool started / finished, args, result summary
        case TextEvent(): ...            # model text (delta or complete part)
        case ApprovalRequest() as req:   # gated tool awaiting a decision
            await req.respond(approve=True)
        case TurnEnded(): ...            # result, usage, session state
```

Built by wrapping existing seams, not rewriting the loop: the `UIHooks`
callbacks and the approval path in `_run_with_approval` already fire at exactly
these moments. `stream_turn` installs internal callbacks feeding an `asyncio`
queue and yields typed events; `ApprovalRequest.respond()` fulfils the same
future the TUI's approval modal fulfils. `run_turn(prompt)` remains the sugar:
drives `stream_turn`, auto-resolves approvals per the current `Mode`, returns
the final result.

## Error handling

Fail at `build()`, not mid-turn:

- `build()` validates the whole composition and raises a single `BuilderError`
  (a `ValueError` subclass) listing **all** problems, not just the first:
  tool-name collisions, sub-agent granted tools from unloaded groups,
  `.with_lsp(tools=True)` without LSP, sessions dir not creatable, a string
  model id pydantic-ai cannot resolve.
- Builder methods are dumb chainable setters (return `self`, no I/O).
- Calling `build()` twice raises — single-shot, avoiding aliased collaborator
  graphs.
- Runtime errors unchanged: `_actionable_error_note`, resumability repair, and
  the provider-error spill file — which moves under the session dir instead of
  assuming `.marim/` when the embedder has not opted into workspace scanning.

## Testing

1. **Builder unit tests** — pure construction: registered tool names per group
   combo, collision/validation errors (all-at-once reporting), defaults-off
   assertions ("bare build has no bash"), groups↔`names.py` exhaustiveness.
2. **Turn tests with pydantic-ai `FunctionModel`/`TestModel`** — a built
   harness runs a scripted turn end-to-end: custom gated tool defers and
   resolves through the approval loop; in-memory session round-trips; MCP
   toolset composes.
3. **Dogfood equivalence** — existing `build_harness` wiring tests pass
   unchanged after the refactor onto the builder.
