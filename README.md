# marim-harness

A terminal coding agent built on [Pydantic AI](https://ai.pydantic.dev/) and
[Textual](https://textual.textualize.io/). It reads, searches, and edits files
and runs commands in a workspace, with a live TUI for interactive work and a
headless mode for one-shot prompts and scripting.

## Features

- **Interactive TUI** — streaming responses, tool-call cards, a live token
  counter, and an approval flow for gated actions.
- **Headless mode** — run a single turn and print the result (`text`, `json`,
  or `stream-json`), suitable for pipes and CI.
- **Sessions** — conversations persist per workspace; `--resume` reattaches to
  the latest and replays its history.
- **Permission modes** — `auto` (run tools freely), `ask` (approve each gated
  tool, TUI only), and `plan` (read-only).
- **Command policy** — a configurable allow/deny list for shell commands,
  enforced in both `auto` and `ask` modes (`plan` mode blocks all commands
  outright).
- **MCP** — connect Model Context Protocol servers and grant them selectively
  to spawned sub-agents.
- **Background jobs & sub-agents** — fire-and-forget shell/agent work whose
  results are pulled back into the conversation.

## Install

```bash
uv sync            # or: pip install -e .
```

This exposes the `marim` (and `marim-harness`) console scripts.

## Usage

```bash
marim                       # launch the TUI in the current directory
marim /path/to/workspace    # ...in a specific workspace
marim --resume              # resume this workspace's latest session

# Headless: -p with a prompt, or pipe one on stdin
marim -p "summarize src/marim_harness/agent.py"
echo "what does run_turn do?" | marim
marim -p "list the tools" --output-format json --mode plan
```

### Management subcommands

```bash
marim sessions   # list / inspect saved sessions
marim config     # view configuration
marim models     # list available models for the active provider
```

## Configuration

Configuration is read from the environment and from `.env` files (a project
`.env` in the workspace overrides a global one under `$XDG_CONFIG_HOME/marim/`).
Real shell environment variables win over both. **API keys are only ever read
from the environment — they are never written to session files or logs.**

| Variable | Purpose |
| --- | --- |
| `MARIM_PROVIDER` | `openrouter` (default), `google`, or `local` |
| `MARIM_MODEL` | Model id override |
| `MARIM_BASE_URL` | Base URL for the `local` provider |
| `OPENROUTER_API_KEY` | API key for OpenRouter |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | API key for the Google provider |
| `MARIM_API_KEY` | Generic API key fallback |
| `MARIM_MAX_CONTEXT_TOKENS` | Context budget before compaction |
| `MARIM_PROACTIVE_MEMORY` | Enable proactive memory writes (truthy to enable) |
| `MARIM_COMMAND_DENYLIST` | Comma/newline-separated patterns to block |
| `MARIM_COMMAND_ALLOWLIST` | If non-empty, only matching commands are allowed |

`MARIM_COMMAND_DENYLIST` / `MARIM_COMMAND_ALLOWLIST` entries are regular
expressions (a literal substring also works); deny takes precedence over allow.
The policy gates the shell tool in `auto` and `ask` modes; `plan` mode is
read-only and denies the shell tool outright.

## Development

```bash
uv run pytest        # run the test suite
uv run ruff check    # lint
```

The codebase is organized into bounded subpackages under `src/marim_harness/`:
`config/`, `session/`, `mcp/`, `workspace/`, `tools/`, and `interfaces/`
(`tui/` and `cli/`). The agent loop lives in `agent.py`.
