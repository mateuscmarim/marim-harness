# Exported capabilities

marim exports parts of itself as standard
[pydantic-ai capabilities](https://ai.pydantic.dev/) so they can be used with
**any** pydantic-ai agent — no marim harness required. They live under
`marim_harness.capabilities` and depend only on pydantic-ai plus marim's pure
helpers — no functional dependency on marim's runtime (they never touch
`Deps`, services, or TUI objects), even though importing the package
transitively loads some of those runtime modules.

## Advisor

A second, separately-configured (typically stronger) model the agent can
consult mid-task. Calling the `advisor` tool forwards the run's transcript to
the reviewer model and returns strategic guidance; the capability also adds
instructions telling the model *when* consulting is worth it.

### With a plain pydantic-ai agent

```python
from pydantic_ai import Agent
from marim_harness.capabilities import Advisor

agent = Agent(
    "anthropic:claude-sonnet-4-6",
    capabilities=[Advisor(model="openai:gpt-5.2", max_uses=5)],
)
```

Parameters:

- `model` — the advisor model: a `provider:model` string (resolved lazily on
  the first consultation, so construction never needs credentials) or a
  pydantic-ai `Model` instance.
- `max_uses` — per-run cap on consultations (default `5`; `None` =
  unlimited). Hitting the cap returns a "continue without advice" tool
  result, never an error.
- `max_tokens` — advice budget for the one-shot advisor run (default `2048`).
- Plus the standard capability keywords: `id`, `description`, and
  `defer_loading` (with `defer_loading=True` the tool is deferred — not
  loaded — until the model loads the capability via `load_capability`;
  requires `id`).

Failures follow the errors-as-text contract: a broken or unresolvable advisor
model degrades the advice ("Advisor unavailable: … Continue without
advice."), never the run.

Because `model` is a plain string, the capability also works in
`Agent.from_file` YAML specs. pydantic-ai only auto-resolves its own
built-in capability names, though — a custom capability like `Advisor` must
be passed explicitly via `custom_capability_types`, or loading raises
`ValueError: Capability 'Advisor' is not in the provided
custom_capability_types`:

```yaml
model: anthropic:claude-sonnet-4-6
capabilities:
  - Advisor:
      model: openai:gpt-5.2
```

```python
agent = Agent.from_file("agent.yaml", custom_capability_types=[Advisor])
```

### With the marim harness

Embedders using [`HarnessBuilder`](builder.md) should pick **one** of:

- `with_advisor(model, ...)` — marim-native: live `/advisor` toggling,
  session persistence, claude-cli handling.
- `with_capability(Advisor(...))` — the portable, statically-configured
  capability described here.

Attaching both registers two tools named `advisor`, which pydantic-ai
rejects at run time.

Both paths share the same consult core (`marim_harness.advisor.consult`), so
behavior cannot drift between them.
