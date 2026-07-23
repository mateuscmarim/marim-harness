# Documentation

## For users and embedders

- [`architecture.md`](architecture.md) — how the codebase is put together and
  the invariants behind the turn loop. Start here before contributing.
- [`embedding.md`](embedding.md) — using `HarnessBuilder` to compose the agent
  loop as a library.
- [`sdk/`](sdk/README.md) — the embedding SDK guide: getting started, custom
  tools, sessions, sub-agents, testing, and a full tutorial.
- [`plugins.md`](plugins.md) — authoring plugins (skills, sub-agents, hooks,
  MCP servers, `AGENTS.md`).
- [`lsp-plugins.md`](lsp-plugins.md) — adding language-server support via LSP
  provider plugins.
- [`examples/`](examples/) — example configurations and agent specs.

## Internal

- [`internal/`](internal/) — dated codebase reviews and research reports kept
  for the record; not maintained as documentation.
- [`superpowers/`](superpowers/) — design specs and implementation plans
  produced during development. Some source docstrings reference specs here for
  the original design rationale.
