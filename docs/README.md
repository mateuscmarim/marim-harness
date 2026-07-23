# Documentation

## Guides

- [`guides/tui.md`](guides/tui.md) — the interactive TUI: slash commands,
  key bindings, approvals, the sub-agents screen, settings.
- [`guides/headless.md`](guides/headless.md) — one-shot mode for scripts and
  CI: output formats, exit codes, the management subcommands.
- [`guides/sessions.md`](guides/sessions.md) — sessions, resuming, compaction,
  checkpoints, and `/rewind`.
- [`guides/subagents.md`](guides/subagents.md) — sub-agents and background
  jobs: spawning, agent specs, model tiers, the `claude-cli` backend.
- [`guides/workflows.md`](guides/workflows.md) — dynamic workflows: the
  sandboxed orchestration scripts, host functions, budgets, and safety.
- [`guides/hooks.md`](guides/hooks.md) — lifecycle hooks: every event, the
  config format, context injection, and Claude Code compatibility.
- [`guides/mcp.md`](guides/mcp.md) — MCP servers: config scopes, the
  `marim mcp` CLI, trust, tool exposure, and sub-agent grants.
- [`guides/skills-and-memory.md`](guides/skills-and-memory.md) — AGENTS.md
  instructions, skills, persistent memory, and the session scratchpad.
- [`guides/trust.md`](guides/trust.md) — the trust and permission model:
  modes, command policy, path guards, and the project trust gate.

## Reference

- [`reference/configuration.md`](reference/configuration.md) — every
  `MARIM_*` environment variable, with defaults and formats. CI enforces
  completeness (`tests/test_docs_reference.py`).
- [`reference/serve-api.md`](reference/serve-api.md) — the `marim serve`
  HTTP daemon: REST endpoints, streaming, auth, lifecycle.

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
