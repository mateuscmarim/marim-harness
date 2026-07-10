# embedding

A worked example of **embedding marim-harness as a library** — not a plugin you
install, but Python that builds its own agent with `HarnessBuilder`. It stands
up a tiny *architecture-decision assistant*: ask it to record a decision and it
appends an ADR-style entry to `DECISIONS.md` in the workspace.

It is the smallest complete thing that exercises the parts of the SDK an
embedder actually reaches for:

- **`HarnessBuilder`** — explicit composition, no `MARIM_*` env reads.
- **A gated custom tool** (`record_decision`) — mutates the workspace, so it is
  registered `requires_approval=True` and runs through the approval loop.
- **A read-only custom tool** (`list_decisions`) — registered ungated, no
  approval round.
- **The `model=` seam** — `build_assistant(workspace, *, model=None)` defaults
  to a real model but lets a test inject a scripted one.

## Run it

pydantic-ai reads the provider's own env var for credentials (e.g.
`ANTHROPIC_API_KEY`); the model is any pydantic-ai model string.

```bash
ANTHROPIC_API_KEY=... uv run python examples/embedding/assistant.py \
    "Record that we picked SQLite for the cache — it's zero-ops." .
cat DECISIONS.md
```

The second argument is the workspace the assistant reads and writes (defaults to
the current directory). In `Mode.auto` the gated tool runs without prompting;
switch `build_assistant` to `Mode.ask` to gate writes behind a human, or
`Mode.plan` for a read-only turn.

## Test it (no network, no key)

The guard test drives the embedder through real turns with a scripted
`FunctionModel`, so it needs no API key — the same approach `docs/sdk/testing.md`
recommends for your own embedders:

```bash
uv run pytest tests/test_examples_embedding.py
```

It asserts both custom tools register, that a gated `record_decision` turn lands
`DECISIONS.md` on disk under `Mode.auto`, and that empty input is rejected
without writing.

## Where this maps in the docs

- [`docs/embedding.md`](../../docs/embedding.md) — the SDK overview and doc map.
- [`docs/sdk/custom-tools.md`](../../docs/sdk/custom-tools.md) — the tool shape,
  the factory-closure `make_*_tool` pattern, and the `RunContext`/`Deps`
  real-import gotcha this file calls out in a comment.
- [`docs/sdk/testing.md`](../../docs/sdk/testing.md) — the `model=` seam and the
  `FunctionModel`/`TestModel` patterns the guard test uses.
- [`docs/sdk/tutorial-daily-report.md`](../../docs/sdk/tutorial-daily-report.md)
  — a larger end-to-end embedder if you want more than this minimal sample.
