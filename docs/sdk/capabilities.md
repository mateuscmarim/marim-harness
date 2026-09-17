# Upstream capabilities

## Advisor

Use [upstream Advisor](https://pydantic.dev/docs/ai/harness/advisor/) with a plain
Pydantic AI agent or `HarnessBuilder.with_capability`:

```python
from pydantic_ai import Agent
from pydantic_ai_harness import Advisor

agent = Agent(
    "anthropic:claude-sonnet-4-6",
    capabilities=[Advisor("openai:gpt-5.2", max_uses=5, forward_history=True)],
)
```

The local tool is `advisor(prompt: str)`: include a self-contained question and
all current evidence. `forward_history=True` also forwards completed history,
excluding the current executor response. Its default is False.

Upstream defaults are `mode="auto"`, `max_uses=None`, `max_tokens=None`,
`caching=None`, `forward_history=False`. A positive `max_uses` caps calls per
model request, resetting on the next executor response; None means unlimited.
An output limit must be at least 1024 tokens. Provider and usage-limit errors
propagate; malformed advisor output uses upstream retries. Nested usage joins the
parent accumulator and remaining usage limits. Mixed-model costs are estimates
or unavailable; provider-billed details may cover only part of the aggregate.

Concrete Model objects preserve their configured clients and use local execution.
Upstream strings support auto/native routing for compatible providers. In native
mode, provider restrictions apply (for example OpenRouter does not support
`max_uses`); consult upstream documentation for native behavior.

### Marim runtime selection

Choose one integration:

- `with_advisor(model, max_tokens=2048, max_uses=None)` resolves Marim's model
  source to a concrete model and selects upstream local execution with
  `forward_history=True`. Saved selection, `/advisor`, and
  `set_advisor_model(id_or_none)` apply on the next turn without rebuilding the
  Agent. Approval, retry and output-correction rounds keep the initial selection.
- `with_capability(Advisor(...))` composes the upstream capability directly,
  with native/local policy and provider resolution owned by upstream.

Enabling both rejects the turn before a provider request. A Claude/Codex CLI main
executor cannot use Marim's runtime advisor; either CLI can be an advisor through
a separate ephemeral read-only conversation.

### Migration from Marim's custom Advisor

The deprecated `marim_harness.capabilities.Advisor` and
`marim_harness.capabilities.advisor.Advisor` aliases shipped in 0.12.0 and
have now been removed. Import `Advisor` directly from `pydantic_ai_harness`;
Marim no longer ships a `capabilities` package.
Retired `id`, `description`, and `defer_loading` constructor arguments now
raise TypeError. Calls require a prompt; per-turn caps, clipping retries,
errors-as-advice strings, and usage trailers are removed. Existing saved
no-argument calls and historical usage trailers remain readable verbatim.

Pydantic AI Harness is a base dependency. Advisor works without the workflows
extra or Monty. Overall turn usage limits remain independent of the per-request
consultation cap.
