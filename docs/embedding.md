# Embedding

`marim-harness` is also a library: `HarnessBuilder` composes a `Harness` — the
same turn-execution engine the `marim` CLI drives — with explicit choices, no
env reads, and no writes outside the workspace unless you opt in. Use it to
run marim's agent loop, tools, and approval model inside your own process.

The CLI (`runtime/bootstrap.py`'s `build_harness`) is a preset built on this
same `HarnessBuilder` — env-var config, workspace scanning (project hooks,
`.marim/mcp.json`, plugin discovery), and the TUI/headless front-ends are all
CLI concerns layered on top. None of that runs when you build directly.

## Install

```bash
pip install marim-harness   # or: uv add marim-harness
```

No extra is required — `tui` and `serve` are for the console app, not the
library surface.

## Quickstart

```python
import asyncio
from pathlib import Path

from marim_harness import HarnessBuilder


async def main() -> None:
    harness = HarnessBuilder(
        workspace=Path("."),
        model="anthropic:claude-sonnet-4-6",   # any pydantic-ai model string
    ).build()

    reply = await harness.run_turn("list the files in this directory")
    print(reply)


asyncio.run(main())
```

`model` is passed straight to pydantic-ai's `infer_model` (or you can pass an
already-constructed `Model` instance). API keys follow pydantic-ai's own
env-var convention per provider (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, …) — marim never reads its own `MARIM_*`/`.env` config in
this path; that's a CLI-only concern (see "What this SDK does not do" below).

## Bare-build defaults

`HarnessBuilder(workspace=..., model=...).build()` with no `with_*` calls
gives you:

| On by default | Off by default (opt in via `with_*`) |
| --- | --- |
| File reads: `read_file`, `glob`, `tree`, `grep` | `bash` |
| File writes (gated): `write_file`, `edit_file` | `net` (`web_search`, `fetch_url`, also gated) |
| Mode `auto` (gated tools run unprompted) | `memory` (`remember`, `recall`) |
| An in-memory session (nothing touches disk) | `skills` (`activate_skill`, `read_skill_file`) |
| | `tasks` (`update_tasks`, `ask_user`, `present_plan`) |
| | `jobs` (background job tools) |
| | `spawn` (`spawn_agent` — implied by `with_subagent`) |
| | LSP (manager + the six navigation tools) |
| | MCP servers, forge (Gitea/GitHub), hooks, extra instructions |

Everything with reach beyond reading/writing files in the workspace is
opt-in. `with_defaults()` flips every tool group on plus LSP-with-tools and
the user-level global instructions (see below) — it's the "give me
everything the CLI has, minus workspace scanning" shortcut.

## `with_*` methods

- **`with_bash(policy=None)`** — enables `bash` (gated). `policy` is a
  `CommandPolicy` allow/deny list; omit it to allow everything.
- **`with_net()`** — enables `web_search`/`fetch_url` (gated — outbound
  network is an exfiltration boundary, so `plan` mode denies it).
- **`with_memory(dir=None)`** — enables `remember`/`recall`. Without `dir`,
  memory uses the CLI's default scopes (XDG global + `<workspace>/.marim/memory`
  project). With `dir`, both scopes are rehomed under `dir/global` and
  `dir/project` — nothing touches XDG.
- **`with_skills(dirs=None)`** — enables `activate_skill`/`read_skill_file`.
  Without `dirs`, this is CLI-style discovery; with `dirs`, only those
  directories are scanned (project/plugin/global skill discovery is skipped
  entirely).
- **`with_tasks()`** — enables `update_tasks`, `ask_user`, `present_plan`.
- **`with_jobs(combined=False)`** — enables background job tools. `combined`
  registers the single `job` tool instead of the four-tool split
  (`jobs`/`job_output`/`wait_for_job`/`cancel_job`).
- **`with_lsp(enabled=True, tools=True)`** — turns the LSP manager on
  (diagnostics-on-edit) and, when `tools` is also true, registers the six
  navigation tools (`goto_definition`, `find_references`, …). Call
  `with_lsp(enabled=False)` to explicitly disable after `with_defaults()`.
- **`with_mcp_server(server)`** — attaches a ready pydantic-ai MCP
  server/toolset object (marim's own JSON-spec format, `.marim/mcp.json`, is
  a CLI concern — `with_mcp_server` takes constructed objects only):

  ```python
  from pydantic_ai.mcp import MCPServerStdio

  builder.with_mcp_server(MCPServerStdio("npx", args=["-y", "some-mcp-server"]))
  ```

  (`MCPServerStdio`/`MCPServerSSE`/`MCPServerStreamableHTTP` are deprecated in
  pydantic-ai 2.x favor of `MCPToolset`, but marim's own MCP config layer
  still builds them — see `mcp/config.py` — so they're the tested path here
  too.)

- **`with_forge(backend)`** — attaches Gitea/GitHub PR tools against an
  explicit `ForgeBackend` implementation (see `forge/backend.py`). Bypasses
  the CLI's `tea`-on-`PATH` auto-detection.
- **`with_subagent(defn)`** — registers an `AgentDef` sub-agent spec (implies
  `spawn`, since a spec nobody can reach is dead weight). See below.
- **`with_tool(fn, requires_approval=False)`** — registers a custom tool on
  the exact same path as the built-ins. See below.
- **`with_instructions(extra=None, replace=None)`** — `replace` swaps the
  whole system prompt; `extra` appends a paragraph (repeatable — each call
  adds another). Neither is required; the default is a short generic prompt.
- **`with_sessions(dir=None)`** — opts into persistence. See below.
- **`with_mode(mode)`** — sets the initial `Mode` (`Mode.auto` / `Mode.ask` /
  `Mode.plan`). `ask` needs an approval callback (`Harness.bind_ui`) to grant
  anything — without one it denies every gated call, so plain headless
  embedding wants `auto` or `plan`.
- **`with_hooks(runner)`** — attaches a `HookRunner` for lifecycle hooks.
- **`with_defaults()`** — every tool group, LSP with tools, and global
  instructions. Does *not* do workspace scanning (project hooks/MCP/skills
  discovery stays a CLI concern).
- **`with_deps(deps)`** / **`with_config_overrides(**fields)`** — advanced
  escape hatches for replacing the constructed `Deps` wholesale or setting
  `HarnessConfig` fields directly. Unstable surface — field names track
  `HarnessConfig`; unknown names raise `TypeError` immediately.

## Custom tools and approval modes

A custom tool registers exactly like a built-in one — same signature shape
(`RunContext[Deps]` first arg, a docstring the model reads as the tool
description), same approval path when gated:

```python
from pydantic_ai import RunContext
from marim_harness.runtime.deps import Deps


def deploy(ctx: RunContext[Deps], target: str) -> str:
    """Deploy the app to `target`."""
    return f"deployed {target}"


harness = (
    HarnessBuilder(workspace=Path("."), model="anthropic:claude-sonnet-4-6")
    .with_tool(deploy, requires_approval=True)
    .build()
)
```

`requires_approval=True` routes the call through `resolve_approvals` against
the current `Mode`, same as `write_file`/`edit_file`/`bash`: `auto` runs it
unprompted; `plan` denies it (read-only bash is allowed through); `ask`
delegates to whatever approval callback you've wired via `Harness.bind_ui` —
with none wired (the common case for a headless embedder), `ask` denies
every gated call rather than crash, since nothing can grant approval. A
custom tool whose name collides with a built-in, or that's registered twice,
fails at `build()`.

## Sessions

The bare build's session is **in-memory only** — `harness.session.store is
None`, nothing is written anywhere. Call `with_sessions()` to opt into
persistence: with no `dir`, sessions land under the CLI's default
(`$XDG_DATA_HOME/marim-harness/sessions/<workspace-name>-<hash>/`, one JSON
file per session); pass `dir=` to scope them under a directory you own
instead, so an embedded harness never touches XDG unless you ask it to.

```python
harness = (
    HarnessBuilder(workspace=Path("."), model="anthropic:claude-sonnet-4-6")
    .with_sessions(dir=Path("./.myapp/sessions"))
    .build()
)
```

## Sub-agents

`AgentDef` (from `marim_harness.workspace.agents`) describes a role: name,
description, system prompt, and the tool names it may use.
`with_subagent(defn)` registers it and grants the parent `spawn_agent`.
`build()` validates the grant against what's actually enabled — a spec
granting `goto_definition` without `with_lsp(tools=True)`, or an unknown tool
name, fails `build()` rather than silently dropping the tool at spawn time:

```python
from marim_harness.workspace.agents import AgentDef

reviewer = AgentDef(
    name="reviewer", description="reviews diffs", prompt="You review diffs.",
    tools=frozenset({"read_file", "grep"}), source="programmatic",
)
builder.with_subagent(reviewer)
```

## Error handling

`build()` validates the whole composition at once and raises `BuilderError`
listing every problem found (a bad custom-tool name and a bad sub-agent grant
both show up in the same exception, not one round-trip each):

```python
from marim_harness import BuilderError

try:
    harness = builder.build()
except BuilderError as exc:
    for problem in exc.problems:
        print("-", problem)
```

A `HarnessBuilder` is single-shot: calling `build()` a second time on the
same instance raises `RuntimeError`. Build a new `HarnessBuilder` per
`Harness` you need.

## What this SDK deliberately does not do

- **No `MARIM_*` env reads.** Model, mode, tool selection, and every other
  knob are explicit constructor/`with_*` arguments. (Model API keys are the
  one exception — those follow pydantic-ai's own per-provider env-var
  convention, not marim's.)
- **No uninvited XDG/workspace writes.** Sessions stay in-memory until
  `with_sessions()`; memory/skills stay on CLI defaults or your `dir=`
  overrides. Note: provider-error payloads still spill best-effort to
  `<workspace>/.marim/last-provider-error.json` on hard failures — that's
  workspace-local, not XDG, and happens regardless of session config.
- **No uninvited XDG reads either — with one opt-in exception.** A bare
  `.build()` never reads `~/.config/marim` at all: the instruction closures
  that would advertise a tool group (sub-agent roster, skill index, memory
  index) only register when the matching `with_*` call loaded that group,
  and the user-level `AGENTS.md` / installed-plugin instructions only
  register when you opt in (`global_instructions=True` via
  `with_config_overrides`, or by calling `with_defaults()`, which turns it
  on along with every other group). `with_defaults()` is therefore the one
  builder call that performs XDG reads (global `AGENTS.md`, skills, plugins,
  memory index) — everything else stays workspace-scoped.
- **No `claude-cli` backend.** That provider shells out to a Claude Code
  subscription as a launcher; it's a CLI-only mode, not part of the builder
  surface.
- **No runtime model switching.** The model is fixed at `build()`. The CLI's
  `/model` picker and provider-switching commands are CLI-layer features on
  top of a fresh `Harness`, not something the builder itself supports.

For any of the above, use the `marim` CLI (see the top-level `README.md`) —
or drive `runtime/bootstrap.py`'s `build_harness` directly, which is the same
builder with those CLI presets layered on.

Phase 2 of this SDK work (a `stream_turn` async-iterator API for consuming
tool-call/text events incrementally, instead of only the final string from
`run_turn`) is planned but not yet implemented.
