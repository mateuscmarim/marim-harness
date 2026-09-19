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

    outcome = await harness.run_turn("list the files in this directory")
    print(outcome.result)


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

For upstream Advisor composition and migration from the removed import aliases, see
[Upstream capabilities](sdk/capabilities.md).

Persistent sessions compact through Pydantic AI Harness strategies. Advanced
embedders can pass `compaction_strategy=` through `with_config_overrides`; use a
`SummarizingCompaction` in place of the removed low-level `summarizer=` callback.
Setting the strategy to `None` keeps deterministic sliding-window trimming.

### `with_advisor(model, *, max_tokens=2048, max_uses=None)`

Gives the main agent upstream `advisor(prompt: str)`. Include a self-contained
question and current evidence; completed history is forwarded, excluding the
current response. Marim resolves a concrete model through its configured model
source and selects upstream local execution, preserving provider/client settings.
`max_tokens` defaults to 2048 and must be at least 1024. `max_uses` is a
per-model-request cap (`None` = unlimited), reset on each executor request.
Provider and usage-limit errors propagate through normal turn handling; usage
joins the parent turn budget. Aggregate mixed-model cost is estimated or unknown.

`harness.set_advisor_model(id_or_none)` persists immediately and applies on the
next turn, including when called during an approval wait. An active turn retains
its choice through retries and output correction without rebuilding the Agent.
Claude/Codex CLI main executors cannot use Marim's runtime advisor; either CLI
can be an advisor through an ephemeral read-only clone. For upstream native/auto
routing use `with_capability(Advisor(...))` instead; enabling both is rejected
before a provider request. See [SDK migration](sdk/capabilities.md).

### `with_thinking(level)`

Sets the thinking level (reasoning effort) applied to the main model each turn
via `ModelSettings.thinking`. `level` is one of `off`, `minimal`, `low`,
`medium`, `high`, `xhigh` (`off` omits the setting — the default). The level
persists per session and can be switched live with
`harness.set_thinking_level(...)`; sub-agents inherit it unless their spec or
the spawn call overrides it. Providers that don't support reasoning effort
ignore the setting.

### Structured output

`with_output_type(schema)` (a pydantic `BaseModel` subclass or an
object-rooted JSON Schema dict) makes `run_turn` return a `TurnOutcome`
whose `structured_output` is the validated object — tools and approval
unaffected mid-run. Retries are bounded (BaseModel: pydantic-ai in-run
retries; dict: one corrective round), and exhaustion surfaces as the
`error_max_structured_output_retries` subtype, so check `outcome.subtype`
before trusting `structured_output`. Every `run_turn` returns a
`TurnOutcome`; plain harnesses get the text in `.result`.

Native Codex subscription models use the same Pydantic AI structured-output
path as other native providers. The retired `CodexCliModel` and `CodexServer`
embedding APIs are removed; use `openai-codex:<model>` instead.

### Session claims

A `Harness` with a `manager` (see [Sessions & state](sdk/sessions-and-state.md))
carries a non-blocking ownership claim on the session it drives — but the
claim attaches the first time the harness *switches*: `switch_session`/
`new_session` claim the target before touching the outgoing session and
release the one being left, so after any switch the active session is always
claimed. A freshly built harness does NOT auto-claim its starting session —
the CLI claims before construction and hands the result to `adopt_claim()`,
while `serve` keeps daemon ownership on the `SessionHost` (released through
its `aclose()` funnel) and never passes it to the harness at all; the
builder has no equivalent, because a build-time auto-claim would collide
with those already-held claims (`flock` locks on separate file descriptors
deny each other even within one process).
Embedders that persist sessions should take starting ownership explicitly:

```python
from marim_harness.session.claim import try_acquire

manager = harness.session.manager
store = harness.session.store
if manager is not None and store is not None:
    claim = try_acquire(manager.session_path(store.session_id), kind="sdk")
    if claim is None:
        ...  # another live process owns it — refuse to drive it
    harness.adopt_claim(claim, kind="sdk")
```

Switching onto a session another live process (a TUI, a headless run, the
`serve` daemon) already owns raises `marim_harness.session.claim.SessionClaimed`
instead of switching — the outgoing session's claim is untouched.
`harness.aclose()` releases the held claim in a `finally`, so a discarded
harness never leaks ownership, even under cancellation. A session an
embedder never claimed is not protected against: another process can claim
it, and a deletion elsewhere can remove the file the harness keeps
persisting to — the same degrade stance `session/claim.py` documents.

## The SDK docs

| Page | Covers |
| --- | --- |
| [Getting started](sdk/getting-started.md) | Install, models & keys, the bare-build contract |
| [Builder reference](sdk/builder.md) | Every `with_*` method and `build()` validation |
| [Turns](sdk/turns.md) | `run_turn`, modes & the approval loop, `bind_ui`, streaming, errors |
| [Custom tools](sdk/custom-tools.md) | Tool shape, the TYPE_CHECKING import gotcha, gating |
| [Sub-agents](sdk/subagents.md) | `AgentDef`, tool grants, the depth ceiling |
| [Sessions & state](sdk/sessions-and-state.md) | Persistence, memory, skills, the XDG boundary, the `.marim/` spill |
| [Integrations](sdk/integrations.md) | MCP, LSP, hooks, `CommandPolicy` |
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
