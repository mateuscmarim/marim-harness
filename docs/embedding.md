# Embedding

`marim-harness` is also a library: `HarnessBuilder` composes a `Harness` — the
same turn-execution engine the `marim` CLI drives — with explicit choices, no
env reads, and no writes outside the workspace unless you opt in. Use it to
run marim's agent loop, tools, and approval model inside your own process.

**The full SDK documentation lives in [`docs/sdk/`](sdk/README.md).** This
page is the quickstart plus a map.

## Quickstart

```bash
pip install marim-harness   # or: uv add marim-harness
```

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

That bare build gives you file reads plus gated `write_file`/`edit_file`,
`Mode.auto`, and an in-memory session — nothing else, and nothing touches
disk outside the workspace. Everything with more reach (bash, net, memory,
sessions, LSP, MCP, sub-agents, …) is an explicit `with_*` opt-in, and
`build()` validates the whole composition at once (`BuilderError`).

Model API keys follow pydantic-ai's own per-provider env-var convention
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) — marim never reads its own
`MARIM_*`/`.env` config on this path; that's a CLI-only concern.

### `with_capability(capability)`

Attaches a pydantic-ai `AbstractCapability` to the underlying agent — the
seam for [Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/)
modules or your own capability classes. marim's built-in capabilities (the
history sanitizers and MCP discovered-instructions injection) always run
first; your capabilities follow in the order you chained them.

```python
from pydantic_ai_harness.planning import Planning

harness = (
    HarnessBuilder(workspace=Path("."), model="anthropic:claude-sonnet-4-6")
    .with_capability(Planning())   # the agent gains Planning's write_plan tool
    .build()
)
```

Note the reach trade-off: a capability that ships its own tools (file access,
shell, code execution) attaches those tools *as-is* — they ride pydantic-ai's
plain tool path, not marim's approval gating or `CommandPolicy`. Prefer
marim's own groups (`with_bash`, `with_defaults`) where they overlap, and
reserve capabilities for what marim doesn't provide.

marim also ships its own capabilities to attach here — see
[Exported capabilities](sdk/capabilities.md).

### `with_advisor(model, *, max_tokens=2048, max_uses=None)`

Gives the main agent an `advisor` tool: calling it forwards the full
conversation transcript to `model` (a pydantic-ai model string) and returns
its strategic guidance as the tool result. The tool is only advertised while
an advisor is configured (`harness.set_advisor_model(None)` disables it live),
advice output is capped at `max_tokens`, and `max_uses` caps calls per turn.
Advisor failures come back as text inside the tool result — a broken advisor
never fails the turn. Note: the transcript is sent to `model`'s provider.

### `with_thinking(level)`

Sets the thinking level (reasoning effort) applied to the main model each turn
via `ModelSettings.thinking`. `level` is one of `off`, `minimal`, `low`,
`medium`, `high`, `xhigh` (`off` omits the setting — the default). The level
persists per session and can be switched live with
`harness.set_thinking_level(...)`; sub-agents inherit it unless their spec or
the spawn call overrides it. Providers that don't support reasoning effort
ignore the setting.

## The SDK docs

| Page | Covers |
| --- | --- |
| [Getting started](sdk/getting-started.md) | Install, models & keys, the bare-build contract |
| [Builder reference](sdk/builder.md) | Every `with_*` method and `build()` validation |
| [Turns](sdk/turns.md) | `run_turn`, modes & the approval loop, `bind_ui`, streaming, errors |
| [Custom tools](sdk/custom-tools.md) | Tool shape, the TYPE_CHECKING import gotcha, gating |
| [Sub-agents](sdk/subagents.md) | `AgentDef`, tool grants, the depth ceiling |
| [Sessions & state](sdk/sessions-and-state.md) | Persistence, memory, skills, the XDG boundary, the `.marim/` spill |
| [Integrations](sdk/integrations.md) | MCP, LSP, forge, hooks, `CommandPolicy` |
| [Testing](sdk/testing.md) | Network-free turn tests with `FunctionModel` / `TestModel` |
| [Tutorial](sdk/tutorial-daily-report.md) | A real embedder, end to end |

## Relationship to the CLI

The CLI (`runtime/bootstrap.py`'s `build_harness`) is a preset built on this
same `HarnessBuilder` — env-var config, workspace scanning (project hooks,
`.marim/mcp.json`, plugin discovery), and the TUI/headless front-ends are
all CLI concerns layered on top. None of that runs when you build directly.
What the SDK deliberately does not do (env reads, uninvited XDG access,
`claude-cli` backend, runtime model switching) is spelled out in the
[SDK index](sdk/README.md#what-the-sdk-deliberately-does-not-do).
