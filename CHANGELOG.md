# Changelog

All notable changes to marim-harness are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/) —
pre-1.0, minor versions may contain breaking changes.

## [Unreleased]

## [0.1.0.post1] - 2026-07-23

- Packaging only: the PyPI project page now renders the README's relative
  links and the demo GIF as absolute forge URLs (rewritten at build time via
  `hatch-fancy-pypi-readme`). No code changes.

## [0.1.0] - 2026-07-23

The first tagged release. Highlights of what exists today:

- **Interactive TUI** (Textual): streaming responses, tool-call cards, live
  token counter, inline approval / ask-user / plan panels, sub-agents screen,
  settings, model picker, themes.
- **Headless mode**: one-shot prompts with `text`, `json`, or `stream-json`
  output; `marim serve` HTTP daemon (REST + WebSocket) in the `[serve]` extra.
- **Providers**: OpenRouter (default), any local OpenAI-compatible server
  (Ollama, LM Studio), Google Gemini, and a `claude-cli` provider that
  delegates turns to Claude Code on a Claude subscription.
- **Permission modes** (`auto` / `ask` / `plan`), a configurable shell command
  policy, and workspace-confined file tools.
- **Sessions**: per-workspace persistence, resume, compaction with observation
  masking, checkpoints and `/rewind` backed by git snapshots.
- **Sub-agents**: parallel spawns with granted tool reach, model tiers
  (`cheap`/`med`/`high`), nesting with a depth ceiling, detached fan-out with
  live cards, and an optional `claude -p` backend.
- **Dynamic workflows**: a gated `run_workflow` tool executing model-authored
  scripts in a pydantic-monty sandbox (`[workflows]` extra).
- **Integrations**: MCP servers (global / project / plugin scope), LSP
  navigation + diagnostics via pluggable providers (Python, TypeScript, C++,
  Java bundled), Gitea/GitHub forge tools via the `tea` CLI, lifecycle hooks
  (Claude-Code-compatible), plugins, skills, and persistent memory.
- **Embeddable**: `HarnessBuilder` composes the same agent loop as a library
  with explicit config and no env reads (see `docs/embedding.md` and
  `docs/sdk/`).
- **Extras**: advisor model, thinking levels, desktop notifications, image
  input, background jobs with autonomous wake, session scratchpad.
