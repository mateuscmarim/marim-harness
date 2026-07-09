# Builder reference

`HarnessBuilder` composes a `Harness` explicitly. Builder methods are dumb
chainable setters (no I/O); `build()` validates the whole composition at once
and raises `BuilderError` listing every problem found.

```python
from marim_harness import HarnessBuilder, Mode

harness = (
    HarnessBuilder(workspace=Path("."), model="anthropic:claude-sonnet-4-6")
    .with_bash()
    .with_instructions(extra="Prefer small, focused diffs.")
    .with_mode(Mode.ask)
    .build()
)
```

## Constructor

```python
HarnessBuilder(*, workspace: Path, model: Model | str)
```

- `workspace` — the root all file tools resolve against and refuse to escape.
- `model` — a pydantic-ai model string (resolved via `infer_model` at
  `build()`) or a constructed `Model` instance. See
  [Getting started](getting-started.md#models-and-api-keys).

## Tool-group setters

Each method flips one tool group on. Groups map to tool names as follows
(`tools/names.py:TOOL_GROUPS` is the source of truth):

| Method | Group | Tools registered |
| --- | --- | --- |
| *(always on)* | `files_read` | `read_file`, `glob`, `tree`, `grep` |
| *(always on)* | `files_write` | `write_file`, `edit_file` *(gated)* |
| `with_bash(policy=None)` | `bash` | `bash` *(gated)* |
| `with_net()` | `net` | `web_search`, `fetch_url` *(gated)* |
| `with_memory(dir=None)` | `memory` | `remember`, `recall` |
| `with_skills(dirs=None)` | `skills` | `activate_skill`, `read_skill_file` |
| `with_tasks()` | `tasks` | `update_tasks`, `ask_user`, `present_plan` |
| `with_jobs(combined=False)` | `jobs` | `jobs`, `job_output`, `wait_for_job`, `cancel_job` — or the single `job` tool when `combined=True` |
| `with_subagent(defn)` | `spawn` | `spawn_agent` (implied — a spec nobody can reach is dead weight) |

*Gated* tools route through the approval loop — see
[Turns, modes & approval](turns.md).

Details per method:

- **`with_bash(policy=None)`** — `policy` is a
  [`CommandPolicy`](integrations.md#command-policy-bash) allow/deny list; omitting
  it allows every command (subject to the mode — `plan` still restricts bash
  to read-only commands).
- **`with_net()`** — outbound network is an exfiltration boundary, so both
  tools are gated and `plan` mode denies them outright.
- **`with_memory(dir=None)`** — without `dir`, memory uses the CLI's default
  scopes (XDG global + `<workspace>/.marim/memory` project). With `dir`, both
  scopes are rehomed under `dir/global` and `dir/project` — nothing touches
  XDG.
- **`with_skills(dirs=None)`** — without `dirs`, CLI-style discovery; with
  `dirs`, only those directories are scanned (project/plugin/global skill
  discovery is skipped entirely).
- **`with_subagent(defn)`** — registers an
  [`AgentDef`](subagents.md) and grants the parent `spawn_agent`.

## Environment & wiring setters

- **`with_lsp(enabled=True, tools=True)`** — turns the LSP manager on
  (diagnostics appended to write/edit results) and, when `tools` is also
  true, registers the six navigation tools (`goto_definition`,
  `find_references`, `hover`, `document_symbols`, `workspace_symbols`,
  `diagnostics`). `with_lsp(enabled=False)` explicitly disables after
  `with_defaults()`; the manager switch wins — `enabled=False, tools=True`
  still ends up with no LSP tools.
- **`with_mcp_server(server)`** — attaches a ready pydantic-ai MCP
  server/toolset object. Marim's own JSON-spec format (`.marim/mcp.json`) is
  a CLI concern. See [Integrations](integrations.md#mcp-servers).
- **`with_forge(backend)`** — attaches the five Gitea/GitHub PR tools against
  an explicit `ForgeBackend`. See [Integrations](integrations.md#forge).
- **`with_tool(fn, requires_approval=False)`** — registers a custom tool on
  the exact same path as the built-ins. See [Custom tools](custom-tools.md).
- **`with_instructions(extra=None, replace=None)`** — `replace` swaps the
  whole base system prompt; `extra` appends a paragraph and is repeatable
  (each call appends another). They compose: the base (default or replaced)
  comes first, extras follow in call order, joined by blank lines.
- **`with_sessions(dir=None)`** — opts into persistence. See
  [Sessions & state](sessions-and-state.md#sessions).
- **`with_mode(mode)`** — initial `Mode` (`Mode.auto` / `Mode.ask` /
  `Mode.plan`). `ask` needs an approval callback (`Harness.bind_ui`) to grant
  anything — without one it denies every gated call, so plain headless
  embedding wants `auto` or `plan`.
- **`with_hooks(runner)`** — attaches a `HookRunner` for lifecycle hooks. See
  [Integrations](integrations.md#lifecycle-hooks). Incompatible with
  `with_deps` (see below).
- **`with_defaults()`** — every tool group, LSP with tools, and the
  user-level global instructions. Does *not* do workspace scanning (project
  hooks/MCP/skills discovery stays a CLI concern). This is the one call that
  opts into XDG reads.

## Escape hatches (advanced)

These exist primarily for the CLI preset; reach for them only when the
regular setters can't express what you need.

- **`with_deps(deps)`** — replaces the builder-constructed `Deps` wholesale.
  Because the caller owns the object, it **overrides** `with_memory`,
  `with_skills`, the `with_bash` policy placement, and `with_hooks` — set the
  corresponding fields (`deps.hooks`, `deps.workspace.memory_root`, …) on
  your own `Deps` instead. Combining `with_hooks` with `with_deps` is a
  `build()` error rather than a silent no-op.
- **`with_config_overrides(**fields)`** — sets `HarnessConfig` fields
  directly (`model_source`, `context_limits`, masking knobs, …). Unstable
  surface: field names track `HarnessConfig`; unknown names raise `TypeError`
  immediately (at the call, not at `build()`).

## `build()` validation

`build()` checks the whole composition and reports **every** problem in one
`BuilderError` (a bad custom-tool name and a bad sub-agent grant show up in
the same exception, not one round-trip each):

```python
from marim_harness import BuilderError

try:
    harness = builder.build()
except BuilderError as exc:
    for problem in exc.problems:
        print("-", problem)
```

What it validates:

| Check | Failure reported |
| --- | --- |
| Model string resolves via `infer_model` | `model '...' is not resolvable: ...` |
| Custom tool name vs every tool actually loaded — built-ins from enabled groups, plus `LSP_TOOLS` when `with_lsp(tools=True)`, plus `FORGE_TOOLS` when `with_forge(...)` | `custom tool '...' collides with a built-in tool` |
| Custom tool registered twice | `custom tool '...' registered twice` |
| `with_hooks` + `with_deps` together | `with_hooks is ignored when with_deps supplies a Deps — ...` |
| Sub-agent grants an unknown tool name | `sub-agent '...' grants unknown tools: [...]` |
| Sub-agent grants a tool from a disabled group (including LSP names without `with_lsp(tools=True)` — the error hints at the fix) | `sub-agent '...' grants tools from disabled groups: [...]` |
| Sessions dir not usable (`with_sessions`) | `sessions dir is not usable: ...` |

One accepted gap: **MCP tool names are not collision-checked** — MCP servers
connect lazily after `build()`, so their tool names aren't knowable yet. A
custom tool colliding with an MCP tool name surfaces at connect/run time.

## Single-shot

A `HarnessBuilder` builds once. Calling `build()` a second time on the same
instance raises `RuntimeError` — create a new builder per `Harness`.
