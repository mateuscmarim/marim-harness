# Advisor as a pydantic-ai capability — design

*2026-07-23. Approved scope: shared core (option A of the brainstorm). Companion
context: the pydantic-ai-harness adoption assessment (mddocs gzcxh962, Section D).*

## Goal

Export marim's advisor as a standard pydantic-ai capability —
`marim_harness.capabilities.Advisor` — so any pydantic-ai user can attach it
with one line, while marim's own advisor keeps working exactly as today and
shares the consult logic with the capability (single source of truth, no
parallel implementation).

```python
from pydantic_ai import Agent
from marim_harness.capabilities import Advisor

agent = Agent(
    "anthropic:claude-sonnet-4-6",
    capabilities=[Advisor(model="openai:gpt-5.2", max_uses=5)],
)
```

Strategic context: pydantic-ai-harness (the official capability library)
maintains a README capability matrix listing endorsed community packages;
nothing advisor-shaped exists in it. Publishing first makes marim the
reference implementation of the pattern and is targeted distribution for the
embeddable-harness positioning.

## Non-goals

- Changing marim's runtime advisor wiring beyond the `make_advisor`
  delegation described below. The `services.advise` seam, `/advisor` live
  toggle, session persistence (`ADVISOR_OFF` sentinel), `Deps` per-turn cap,
  prepare-hook tool gating, and TUI stay exactly as they are.
- The `reveal()`-style cache-safe `/advisor` toggle (separate backlog item).
- A separate slim `marim-capabilities` distribution (revisit only on real
  external uptake).
- Guarding against an embedder attaching both `with_advisor(...)` and
  `with_capability(Advisor(...))` (documented as "pick one", not enforced).

## Part 1 — shared core: `consult()` in `advisor.py`

Extract the consult logic both consumers share into one pure async function
in the existing root `advisor.py`:

```python
async def consult(model: Model, messages: list[ModelMessage],
                  *, max_tokens: int = 2048) -> str
```

`consult` owns everything currently inlined in `make_advisor`'s inner
`advise()` **after** model resolution:

- the one-shot tool-free `Agent` built with `_ADVISOR_INSTRUCTIONS` and
  `ModelSettings(max_tokens=...)`;
- `_advise_prompt` wrapping of `render_transcript`, with the `_CLIP_ATTEMPTS
  = (2000, 400)` clip-retry ladder (retry tightens the clip on the theory
  that the likeliest failure is context overflow on the advisor's unknown
  window);
- the errors-as-text contract — every failure path returns a short
  actionable string ("Advisor unavailable: … Continue without advice."),
  never raises;
- the usage footer (`[advisor usage: N in, M out tokens]`).

`make_advisor` shrinks to marim's wrapper concerns: read the live model id
via `get_model_id()` (the per-call resolution that makes `/advisor` switch
without a rebuild), build the model, swap in the `aux_model_for` claude-cli
ephemeral clone, then delegate to `consult`. Behavior is byte-identical;
existing `test_advisor*` tests must pass unchanged — that is the proof the
extraction is faithful.

## Part 2 — the capability

New package directory `src/marim_harness/capabilities/` (so future
capabilities — LSP tools, forge — join it):

- `__init__.py` — re-exports `Advisor`.
- `advisor.py` — the capability.

### API

```python
class Advisor(AbstractCapability[Any]):
    def __init__(self, model: Model | str, *,
                 max_uses: int | None = 5,
                 max_tokens: int = 2048,
                 **kwargs)  # inherited: id, description, defer_loading
```

- `model` — the advisor model. A `str` resolves via `infer_model` **lazily on
  first consult**, so constructing the capability never needs provider
  credentials (and never breaks `Agent.from_file` spec loading). A `Model`
  instance is used as-is.
- `max_uses` — per-**run** consult cap; `None` = unlimited. Default 5.
  Exceeding it returns the "cap reached, continue without advice" string —
  a tool result, never an error.
- `max_tokens` — advice budget, forwarded to `consult` (default 2048,
  matching marim).

### AbstractCapability mapping

| Hook | Behavior |
|---|---|
| `get_instructions()` | the existing `ADVISOR_GUIDANCE` text (static). |
| `get_toolset()` | a `FunctionToolset` with one `advisor` tool; its docstring reuses the model-facing description from `tools/advisor_tools.py`. The tool reads `ctx.messages`, enforces the cap, resolves the model (lazy, cached on the instance), and returns `await consult(model, messages, max_tokens=...)`. |
| `for_run()` | returns a fresh instance (uses counter zeroed) so `max_uses` is per run, mirroring marim's per-turn cap. |
| `from_spec` / `get_serialization_name` | defaults suffice — `model` as a string makes `- Advisor: {model: "openai:gpt-5.2"}` work in YAML specs for free. |
| `defer_loading` | inherited pass-through; with it the tool + guidance stay out of context until the model calls `load_capability`. Requires `id`, per the base class. Covered by a test, no extra code. |

Model resolution failure (bad slug, missing key) follows the errors-as-text
contract: the tool returns "Advisor unavailable: …", the run continues.

### Relationship to marim's own advisor

Marim's runtime does **not** attach this capability. Its `advisor` tool
registration (`tools/provider.py` + `prepare_advisor` gating) stays, and both
paths converge on `consult`. Embedders choose one:

- `HarnessBuilder.with_advisor(...)` — marim-native: live toggle,
  session persistence, `aux_model_for` claude-cli handling.
- `HarnessBuilder.with_capability(Advisor(...))` or plain
  `Agent(capabilities=[Advisor(...)])` — portable, static configuration.

Attaching both would register two advisor tools; the docs say pick one.

## Docs & changelog

- New `docs/sdk/capabilities.md`: what the capability is, plain-pydantic-ai
  usage, `with_capability` usage, the pick-one note, `defer_loading` mention,
  YAML spec snippet.
- Cross-links from `docs/embedding.md` and `docs/sdk/builder.md`.
- CHANGELOG entry (first exported capability — positioning-relevant).

## Testing (TDD)

New `tests/test_capability_advisor.py`, `TestModel`/`FunctionModel` based —
no live providers:

1. Attaching `Advisor` puts the `advisor` tool in the run schema and
   `ADVISOR_GUIDANCE` in the instructions.
2. Consult round-trip: main agent (FunctionModel scripted to call the tool)
   gets advice text produced by a stubbed advisor model; usage footer
   present.
3. `max_uses` enforced within a run (cap-reached string, advisor model not
   invoked) and reset on the next run (`for_run` isolation).
4. Broken advisor model (unresolvable slug / raising model) → tool returns
   "Advisor unavailable" text, run completes.
5. `defer_loading=True` (with `id`) hides the tool until `load_capability`.
6. Existing `test_advisor*` suite green with `make_advisor` delegating to
   `consult` — the shared-core proof.

## Risks

- **pydantic-ai capability API churn** (pinned `>=2.8,<3`): the surface used
  (`AbstractCapability`, `FunctionToolset`, `for_run`, `defer_loading`) is
  the stable documented core; acceptable.
- **Two registrations of a tool named `advisor`** if an embedder ignores the
  pick-one note: pydantic-ai raises on duplicate tool names at run time,
  which is a loud failure, not silent corruption. Acceptable for v1.
