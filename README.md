# marim-harness

**A terminal coding agent you can also build on.**

Use Marim to work on your code with streaming responses, tool approvals, and
resumable sessions—or embed its agent loop in your own Python application.
Connect **Claude Code or Codex** through their CLI backends, or use models
through API providers and local servers.

![Marim fixing a bug with two parallel sub-agents: fan-out cards, an edit approval, and a verification run](docs/assets/demo.gif)

[Quickstart](#quickstart) · [Build with Marim](#build-with-marim) ·
[Documentation](docs/README.md) · [Examples](examples/README.md)

## Quickstart

Requires **Python 3.10+** and [uv](https://docs.astral.sh/uv/).
Install the interactive terminal UI:

```bash
uv tool install 'marim-harness[tui]'
```

In your project directory, choose one of the setups below. Each launches
Marim in `ask` mode so you can review approval requests. Then try:
**“Explain how this project is organized and where its tests live.”**

### Claude Code

Install Claude Code **2.1+** and sign in through `claude` first. The `claude`
executable must be on your PATH; Marim uses the CLI's authentication.

```bash
export MARIM_PROVIDER=claude-cli
marim --mode ask
```

### Codex

Sign in with `codex login`, then select native subscription access:

```bash
export MARIM_PROVIDER=openai-codex
export MARIM_MODEL=gpt-5.6-terra
marim --mode ask
```

Marim runs the agent loop with Pydantic AI and reuses the subscription credentials.

### API provider

For OpenRouter, supply your API key:

```bash
export MARIM_PROVIDER=openrouter
export OPENROUTER_API_KEY='your-api-key'
marim --mode ask
```

Google, OpenCode Zen, and Zen Go are also supported. See
[provider configuration](docs/reference/configuration.md#provider--model)
for credentials and model selection.

### Local model

Start an OpenAI-compatible server such as Ollama or LM Studio with a model
available before launching Marim. No cloud API key is needed:

```bash
export MARIM_PROVIDER=local
export MARIM_BASE_URL=http://localhost:11434/v1  # LM Studio: http://localhost:1234/v1
export MARIM_API_KEY=local
marim --mode ask
```

Choose a model from the server in Marim's model picker. Set `MARIM_MODEL`
explicitly for headless runs.

### Choose your backend

| Backend | Marim manages | Backend manages |
| --- | --- | --- |
| API providers / local models | Agent loop, tools, approvals, sessions, and configured MCP/LSP integrations | Model inference |
| Claude Code (`claude-cli`) | Terminal UI, approval requests, and session resume | Claude's agent loop, tools, and authentication |
| Codex subscription (`openai-codex`) | Agent loop, tools, approvals, sessions, and MCP/LSP integrations | Subscription model inference and upstream OAuth |

Marim's native tools and MCP/LSP configuration apply to API/local and native Codex subscription backends.
The CLI backends have separate integration and configuration limits; see
[provider details](docs/reference/configuration.md#provider--model).

<details>
<summary>Other installation options</summary>

A bare `uv tool install marim-harness` provides headless mode. Add extras as
needed; they can be combined, for example `marim-harness[tui,serve,workflows]`.

| Extra | Adds |
| --- | --- |
| `tui` | Interactive terminal UI |
| `serve` | HTTP daemon with REST and WebSocket access |
| `workflows` | Sandboxed orchestration scripts |
| `lsp-python` | The basedpyright Python language server |

Both `marim` and `marim-harness` invoke the same console app.

</details>

## Why Marim?

- **An agent loop you can embed.** Compose a Python application with
  `HarnessBuilder`, your model, and your tools. Built on
  [Pydantic AI](https://ai.pydantic.dev/), with a
  [Textual](https://textual.textualize.io/) terminal UI.
- **Continuity across tasks.** Resume conversations, steer running work,
  and rewind conversation and workspace checkpoints. File snapshots require
  Git and honor `.gitignore`.
- **Delegation with control.** Run background jobs and sub-agents with
  selected tools and model tiers. Add workflows for scripted orchestration.
- **Context and extensions.** Configure language servers for definitions,
  references, and diagnostics; connect MCP tools; add instructions, skills,
  persistent memory, and plugins.
- **Explicit permissions.** Choose `ask`, `auto`, or read-only `plan` mode.
  Native shell tools support command policies. Project-local hooks and MCP
  require trust, remembered through the first-open prompt, `/trust`, or
  `marim trust grant`. See the [trust guide](docs/guides/trust.md).

## Everyday use

After configuring a provider:

```bash
marim /path/to/workspace    # work in a specific project
marim --resume              # resume this workspace's latest session
marim sessions             # inspect saved sessions
marim config               # view configuration

# Headless prompts for scripts and CI
marim -p "Explain this project's test setup" --mode plan
marim -p "Summarize the README" --output-format json --mode plan
```

In the TUI, use `/model`, `/settings`, and `/help` to explore. Configuration
can live in shell variables or `.env` files; shell variables take precedence.
See the [configuration reference](docs/reference/configuration.md) for defaults.

## Build with Marim

Add the library to your Python project:

```bash
uv add marim-harness
```

With `OPENROUTER_API_KEY` exported, this runs a turn using the same native
agent engine that powers the terminal app:

```python
import asyncio
from pathlib import Path

from marim_harness import HarnessBuilder


async def main() -> None:
    harness = HarnessBuilder(
        workspace=Path("."),
        model="openrouter:anthropic/claude-sonnet-4-6",
    ).build()
    outcome = await harness.run_turn("Explain what the README says this project does")
    print(outcome.result)


asyncio.run(main())
```

A bare builder provides file tools and an in-memory session in `auto` mode.
Add shell tools, sessions, sub-agents, and integrations explicitly. Builder
configuration is explicit; model credentials follow Pydantic AI's provider
conventions, without loading Marim's `.env` files.

Start with the [SDK guide](docs/sdk/README.md),
[custom tools](docs/sdk/custom-tools.md), or
[embedding examples](examples/embedding/README.md).

## Documentation

| I want to… | Start here |
| --- | --- |
| Learn shortcuts, image input, and notifications | [Terminal UI](docs/guides/tui.md) |
| Automate tasks or consume JSON output | [Headless mode](docs/guides/headless.md) |
| Resume, compact, or rewind a session | [Sessions](docs/guides/sessions.md) |
| Configure providers, models, and environment variables | [Configuration](docs/reference/configuration.md) |
| Set permissions and project trust | [Trust](docs/guides/trust.md) · [Security](SECURITY.md) |
| Delegate work or orchestrate agents | [Sub-agents](docs/guides/subagents.md) · [Workflows](docs/guides/workflows.md) |
| Add instructions, skills, memory, or plugins | [Skills and memory](docs/guides/skills-and-memory.md) · [Plugins](docs/plugins.md) |
| Connect tools or lifecycle hooks | [MCP](docs/guides/mcp.md) · [Hooks](docs/guides/hooks.md) |
| Run Marim as a service | [HTTP API](docs/reference/serve-api.md) |

See the [full documentation index](docs/README.md) for more.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and checks,
[architecture](docs/architecture.md) for the codebase map, and the
[quality gate](docs/quality-gate.md) for maintained quality checks.
[Changes](CHANGELOG.md) and [planned work](ROADMAP.md) have their own pages.

## License

[MIT](LICENSE).
