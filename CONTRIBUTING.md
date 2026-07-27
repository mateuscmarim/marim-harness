# Contributing to marim-harness

Thanks for your interest in contributing! This document covers the practical
things you need to know to get a change from idea to merged.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/) for everything — don't invoke
`pip` or a bare `python`/`pytest` directly.

```bash
git clone https://git.marim.dev/mateuscmarim/marim-harness.git
cd marim-harness
uv sync                # install deps into .venv (includes the dev group + TUI)
```

Useful commands:

```bash
uv run pytest                          # full test suite (parallel + coverage on by default)
uv run pytest --no-cov tests/test_x.py # fast single-file run, no coverage
uv run pytest -n 0 -x --pdb tests/...  # serial run for debugging (disables xdist)
uv run ruff check src tests            # lint
uv run ruff check --fix src tests      # lint + autofix
uv run pyright                         # type-check (standard mode, src only)
uv run marim                           # run your checkout's TUI
```

Optional extras: `uv sync --extra lsp-python` adds basedpyright for the Python
language server (without it, Python LSP falls back to jedi); `--extra serve`
adds the HTTP daemon; `--extra workflows` adds the dynamic-workflows sandbox.

Set `MARIM_DEBUG=1` for DEBUG logging. Provider credentials go in `.env` — see
`.env.example`. You don't need a paid API key to hack on marim: any local
OpenAI-compatible server (Ollama, LM Studio) works via `MARIM_PROVIDER=local`,
and most of the test suite runs against test doubles with no model at all.

## Before you open a PR

CI runs, in order: **ruff → pyright → pytest**, on Python 3.10, 3.12, and 3.14
(plus a `uv build` packaging check). Run the same order locally first:

```bash
uv run ruff check src tests && uv run pyright && uv run pytest
```

Things CI will reject:

- **Python 3.11+-only syntax.** `requires-python` is `>=3.10`, and the 3.10 CI
  leg is real. No `Self` without `typing_extensions`, no `except*`, etc.
- **Functions above cyclomatic complexity 10** (`C901`). When a function trips
  the ceiling, extract cohesive branch-clusters into named helpers — don't add
  a blanket `# noqa: C901`. The cap bounds *branch count*, not length; a long,
  straight-line, well-commented function is fine.
- **Lint violations.** Ruff line length is 100; the lint set is
  `E,F,I,UP,B,SIM,C901` (import sorting, pyupgrade, bugbear, flake8-simplify).

## Code conventions

- **Read [`docs/architecture.md`](docs/architecture.md) first** for the lay of
  the land — especially before touching the turn loop (`runtime/harness.py`),
  which encodes non-obvious invariants around resumability and approval rounds.
- **Follow the three-way tool split.** Pure decision/parse helpers are
  side-effect-free and unit-tested directly; the effectful I/O lives in
  `tools/impl/` and is exercised against a tmp workspace; the tool layer above
  it (`fs_tools.py`, `edit_tools.py`, …) is thin `ctx.deps`-unwrapping wiring.
- **Tool docstrings are product.** They are the model-facing tool
  descriptions — write and review them with that in mind.
- **Preserve the "why" comments.** The codebase favors long, explanatory
  comments on why a non-obvious invariant holds (resumability, the
  deps/services cycle). Keep them intact when editing nearby code.
- **New construction wiring** (a tool group, a config knob) goes in
  `runtime/builder.py`; env/discovery reading stays in `runtime/bootstrap.py`,
  so the embedding and CLI paths cannot drift.
- [`coding-guidelines.md`](coding-guidelines.md) collects the design
  principles the codebase aims for. It's guidance, not dogma — break a rule
  when the tradeoff is clear and say why.

## Submitting changes

1. Branch from `master`, keep the change scoped to one concern.
2. Add or update tests — behavior changes without test coverage are unlikely
   to be merged.
3. Make sure the local check sequence above passes.
4. Open a pull request against `master` describing *why*, not just *what*.

Bug reports and feature discussions are welcome as issues. For security
issues, see [`SECURITY.md`](SECURITY.md) — please don't open a public issue.
