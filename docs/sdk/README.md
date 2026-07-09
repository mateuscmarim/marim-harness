# marim-harness SDK

`marim-harness` is a terminal coding agent that is **also a library**:
`HarnessBuilder` composes a `Harness` — the same turn-execution engine the
`marim` CLI drives — with explicit choices, no env reads, and no writes
outside the workspace unless you opt in. Use it to run marim's agent loop,
tools, and approval model inside your own process.

```python
import asyncio
from pathlib import Path

from marim_harness import HarnessBuilder


async def main() -> None:
    harness = HarnessBuilder(
        workspace=Path("."),
        model="anthropic:claude-sonnet-4-6",
    ).build()
    print(await harness.run_turn("list the files in this directory"))


asyncio.run(main())
```

## How the SDK relates to the CLI

There is one construction path. The CLI preset
(`runtime/bootstrap.py`'s `build_harness`) drives this same builder — env-var
config, workspace scanning (project hooks, `.marim/mcp.json`, plugin
discovery), and the TUI/headless front-ends are CLI concerns layered on top.
None of that runs when you build directly:

```
your app ──────────────► HarnessBuilder ──► Harness
marim CLI ─► bootstrap ─► HarnessBuilder ──► Harness ─► TUI / headless
```

The public surface is exported lazily from the package root (importing
`marim_harness` never loads `pydantic_ai` until a symbol is touched):

```python
from marim_harness import (
    HarnessBuilder,  # the front door
    BuilderError,    # every composition problem, reported together
    Mode,            # auto | ask | plan
    Deps,            # RunContext payload — custom tools' first parameter type
    ToolGroups,      # the tool-group composition record
    CommandPolicy,   # bash allow/deny rules
    AgentDef,        # sub-agent role spec
)
```

## Pages

| Page | What it covers |
| --- | --- |
| [Getting started](getting-started.md) | Install, quickstart, models & API keys, bare-build defaults |
| [Builder reference](builder.md) | Every `with_*` method, `build()` validation, `BuilderError` |
| [Turns, modes & approval](turns.md) | `run_turn`, the approval loop, `Mode` semantics, `bind_ui`, streaming |
| [Custom tools](custom-tools.md) | Tool signature, gating, collision rules, the import gotchas |
| [Sub-agents](subagents.md) | `AgentDef`, grants, the depth ceiling |
| [Sessions & state](sessions-and-state.md) | What touches disk, sessions, memory, skills, the XDG boundary |
| [Integrations](integrations.md) | MCP servers, LSP, forge (Gitea/GitHub), lifecycle hooks, bash policy |
| [Testing embedders](testing.md) | Network-free turn tests with `FunctionModel` / `TestModel` |
| [Tutorial: a real embedder](tutorial-daily-report.md) | Walkthrough of the daily-report agent, end to end |

## What the SDK deliberately does not do

- **No `MARIM_*` env reads.** Model, mode, tool selection, and every other
  knob are explicit constructor/`with_*` arguments. (Model API keys are the
  one exception — those follow pydantic-ai's own per-provider env-var
  convention, not marim's.)
- **No uninvited XDG reads or writes.** A bare `build()` never touches
  `~/.config/marim` or `$XDG_DATA_HOME`; `with_defaults()` and
  `with_sessions()` are the opt-ins. See
  [Sessions & state](sessions-and-state.md) for the one workspace-local
  exception (`.marim/last-provider-error.json`).
- **No `claude-cli` backend.** That provider shells out to a Claude Code
  subscription as a launcher; it's a CLI-only mode.
- **No runtime model switching.** The model is fixed at `build()`; the CLI's
  `/model` picker is a CLI-layer feature built on a fresh `Harness`.

For any of the above, use the `marim` CLI (see the top-level `README.md`) —
or drive `runtime/bootstrap.py`'s `build_harness` directly, which is the same
builder with the CLI presets layered on.

## Roadmap

Phase 2 of the SDK (a `stream_turn` async-iterator API for consuming
tool-call/text events incrementally) is planned but not yet implemented.
Today, incremental events are available by passing pydantic-ai's
`event_stream_handler` to `run_turn` — see [Turns](turns.md#streaming).
