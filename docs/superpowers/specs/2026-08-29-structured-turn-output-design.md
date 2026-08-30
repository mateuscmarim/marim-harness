# Structured Output for Embedder Turns — Design

Date: 2026-08-29
Branch: `feat/structured-turn-output` (off master at b269097, v0.4.0)
Status: approved 2026-08-29 — implemented on `feat/structured-turn-output`
(plan: `docs/superpowers/plans/2026-08-29-structured-turn-output.md`, executed
via subagent-driven development; two post-approval amendments landed: dict
validator resolves drafts via `validator_for`, exhaustion classification is
message-based — both reflected below)

## Goal

Give SDK embedders Claude-Agent-SDK-style structured output: a schema set on
the harness, tools and approval unaffected mid-run, validated data on the
final result, retry-until-valid semantics, and a typed outcome instead of a
bare string. Today the agent's output union is hard-coded to
`[str, DeferredToolRequests]` (`runtime/harness.py`) because the approval
loop lives in that union, and `Harness.run_turn` returns `str` — embedders
who need typed data must prompt-and-parse.

## Background — verified pydantic-ai behavior (floor 2.28)

These probes against the installed pydantic-ai shape every decision below:

1. `Agent.run(output_type=...)` overrides the agent-level output type
   **per run**. Overriding with `[Schema, DeferredToolRequests]` on an agent
   built with `[str, DeferredToolRequests]` keeps deferred-tool approval
   firing on round one and returns the validated object on the continuation
   round. The approval loop therefore needs no structural change.
2. `output_type` without `DeferredToolRequests` in the union raises
   `UserError` at the first deferred call — the override must always carry
   the deferred arm.
3. `StructuredDict(schema)` (JSON-Schema-dict path) attaches the schema for
   provider-side constrained generation but **does not validate content**
   (both `validate_python` and `validate_json` accept non-conforming values).
   A pydantic `BaseModel` output type, by contrast, validates through
   pydantic-ai's output machinery, with in-run retries via the agent's
   `retries` (already pinned to 2) and `UnexpectedModelBehavior` on
   exhaustion.

Consequence: the two schema kinds get two enforcement tiers (Section 4).

## Section 1 — API surface

```python
class Report(BaseModel):
    summary: str
    files_changed: list[str]

harness = (
    HarnessBuilder(workspace=Path("."), model="anthropic:claude-sonnet-4-6")
    .with_output_type(Report)          # or a JSON Schema dict
    .build()
)

outcome = await harness.run_turn("audit the last commit")
# outcome.subtype == "success"
# outcome.structured_output -> Report(summary=..., files_changed=[...])
# outcome.result -> str | None
```

- `HarnessBuilder.with_output_type(schema)` accepts a pydantic `BaseModel`
  subclass or a JSON Schema dict. Dicts must be object-rooted; anything else
  raises `BuilderError` at `build()` (mirrors `subagents/output_schema.py`'s
  `resolve_output_schema` root check). The schema is fixed per harness — a
  property of the composition, Claude-style, not a per-call argument.
- The agent-level union stays `[str, DeferredToolRequests]`. Each structured
  turn passes `output_type=[SchemaType, DeferredToolRequests]` per run.
- `run_turn` returns a new `TurnOutcome` in **all** cases (schema'd or not).
  This is the breaking change for 0.x embedders (~150 test callsites across
  ~20 files plus two interface callsites migrate to `.result`).
- `TurnOutcome` mirrors Claude Agent SDK's `ResultMessage`:

```python
@dataclass(frozen=True)
class TurnOutcome:
    subtype: Literal["success", "error_max_structured_output_retries",
                     "error_during_execution"]
    result: str | None          # final assistant text; None when the run
                                # ended in pure structured output
    structured_output: Any      # validated model/schema instance, or None
    errors: list[str] | None    # failure detail on error subtypes
```

`error_during_execution` is in the literal for Claude parity but is not
emitted in v1 (marim's tool failures feed back to the model as tool results
rather than ending the turn).

## Section 2 — Turn mechanics

- `run_turn(prompt, event_stream_handler=None, attachments=None) ->
  TurnOutcome` for plain and schema'd harnesses alike. Plain harnesses:
  `TurnOutcome("success", result=<text>, structured_output=None, errors=None)`.
- Schema'd harnesses: `TurnController._run_with_approval` gains a per-run
  `output_type=[SchemaType, DeferredToolRequests]` argument to `agent.run`
  (and to every continuation round). The `isinstance(result.output,
  DeferredToolRequests)` branch and all dirty-history / persist / rollback
  machinery are untouched.
- `SchemaType` is the `BaseModel` subclass as-is, or
  `StructuredDict(dict_schema)` for dict schemas.
- Session persistence, compaction, checkpoints, and masking are unaffected:
  structured output rides the same tool-call/history path the approval loop
  already persists.

## Section 3 — Enforcement tiers

| Schema kind | Enforcement | Retry |
|---|---|---|
| pydantic `BaseModel` | pydantic-ai output validation | in-run retries (`retries=2`); exhaustion raises `UnexpectedModelBehavior` → classified into the error outcome (Section 4) |
| JSON Schema dict | provider-side constraint during generation + post-turn jsonschema validation of the emitted object | one corrective turn (user message carrying the validation errors), capped → error outcome |

The dict tier needs jsonschema in core: promote `jsonschema>=4` from the
`[workflows]` extra to `dependencies` in `pyproject.toml` (it is already a
dev dependency). Dict validation lives in a new pure helper,
`runtime/structured.py::validate_dict_output(output, schema) -> list[str]`
(empty list = valid) — validator class resolution matching the workflow
validator in `workflows/schema.py` (`jsonschema.validators.validator_for`,
which honors the schema's own `$schema` and defaults to the latest draft),
but independent of it (the workflows package stays extra-gated; core must
not import it).

## Section 4 — Error handling

- Infra/provider errors raise out of `run_turn` exactly as today. Only
  schema failures become outcomes.
- BaseModel exhaustion: the controller classifies `UnexpectedModelBehavior`
  whose message carries pydantic-ai's output-retry-exhaustion phrase
  ("maximum output retries" — message-matching, NOT cause-chain walking:
  tool-arg retry exhaustion also chains a `ValidationError` but must keep
  the infra failure path) while a structured output is active into
  `TurnOutcome("error_max_structured_output_retries", result=None,
  structured_output=None, errors=[...])`. `_handle_run_failure`'s one-shot
  recovery latches must NOT treat this exception as retryable infra failure.
- Dict-schema failure: post-turn validation failure triggers one corrective
  turn; a second failure yields the same error subtype with the validation
  errors listed. An infra failure during the corrective turn raises.
- Error outcomes carry `structured_output=None`; `result` holds the last
  model text when one exists.
- Abort/Ctrl-C: unchanged — `_flush_resumable` and rollback baselines behave
  as today; a structured turn aborted mid-round resumes as any other turn.

## Section 5 — Components

| Where | What |
|---|---|
| `runtime/outcome.py` (new) | `TurnOutcome` dataclass + subtype literal; pure, unit-tested |
| `runtime/builder.py` | `with_output_type(schema)`; `BuilderError` checks (non-object-rooted dict, non-BaseModel/non-dict type) |
| `runtime/harness.py` | `HarnessConfig.output_type` field; wiring through `build_collaborators` |
| `runtime/controller.py` | `run_turn -> TurnOutcome`; per-run output override; exhaustion classification; dict corrective loop |
| `runtime/structured.py` (new) | `validate_dict_output` — pure jsonschema wrapper |
| `__init__.py` | export `TurnOutcome` |
| `interfaces/cli/headless.py`, `interfaces/tui/app.py` | consume `outcome.result` |
| `pyproject.toml` | `jsonschema` promoted to core deps |
| `tests/test_structured_turn.py` (new) | feature suite |
| docs: `sdk/turns.md`, `sdk/builder.md`, `embedding.md`, `CHANGELOG.md` | SDK docs + breaking-change migration note |

## Section 6 — Testing

- Unit: outcome shaping; builder validation errors; `validate_dict_output`
  (valid / wrong type / missing required / non-dict output); exhaustion
  classification.
- Integration (TestModel, per `docs/sdk/testing.md`): happy path with
  `custom_output_args`; deferred approval + structured output across both
  rounds; BaseModel exhaustion → error subtype; dict corrective retry →
  success; dict exhaustion → error subtype; plain harness outcome shape.
- Migration sweep: ~150 test callsites move to `.result` where a string is
  asserted; verify ruff → pyright → pytest (parallel, plus `-n 0` for the
  serial-sensitive files) per CI order.

## Section 7 — Rollout

- Release as 0.5.0 with a CHANGELOG breaking-change entry and a migration
  note (`run_turn` now returns `TurnOutcome`; use `.result` for the text).
- Phase 2, deliberately out of scope here (future specs): a schema param on
  the `spawn_agent` tool (model-initiated structured spawns, parity with
  workflow `agent()`), a `claude-cli` prompt-contract fallback, and a
  headless CLI flag for one-shot structured output.
