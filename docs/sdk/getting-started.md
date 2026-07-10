# Getting started

## Install

```bash
pip install marim-harness   # or: uv add marim-harness
```

No extra is required — `tui` and `serve` are for the console app, not the
library surface. `requires-python` is `>=3.10`.

For a path dependency during development (the pattern the first real embedder
used):

```toml
# pyproject.toml
[project]
dependencies = ["marim-harness"]

[tool.uv.sources]
marim-harness = { path = "/path/to/marim-harness" }
```

## Quickstart

```python
import asyncio
from pathlib import Path

from marim_harness import HarnessBuilder


async def main() -> None:
    harness = HarnessBuilder(
        workspace=Path("."),                    # tool sandbox root
        model="anthropic:claude-sonnet-4-6",    # any pydantic-ai model string
    ).build()

    reply = await harness.run_turn("list the files in this directory")
    print(reply)


asyncio.run(main())
```

`workspace` is the root every file tool resolves against — reads and writes
outside it are refused by the tool layer.

## Models and API keys

`model` accepts either:

- **A pydantic-ai model string** (`"anthropic:claude-sonnet-4-6"`,
  `"openrouter:xiaomi/mimo-v2.5"`, `"openai:gpt-4o"`, …), resolved with
  pydantic-ai's `infer_model`. API keys follow pydantic-ai's own env-var
  convention per provider (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`,
  `OPENAI_API_KEY`, `GEMINI_API_KEY`, …) — marim never reads its own
  `MARIM_*`/`.env` config on this path.

  Note: under pydantic-ai 2.x the `openai:` prefix resolves to the
  **Responses API** (`OpenAIResponsesModel`). If your endpoint only speaks
  Chat Completions (most OpenAI-compatible proxies and local servers), use
  the `openai-chat:` prefix — or pass a constructed `OpenAIChatModel` as
  below.
- **An already-constructed `Model` instance.** Use this for local/OpenAI-
  compatible endpoints, or to inject a scripted model in tests:

  ```python
  from pydantic_ai.models.openai import OpenAIChatModel
  from pydantic_ai.providers.openai import OpenAIProvider

  local = OpenAIChatModel(
      "some-local-model",
      provider=OpenAIProvider(base_url="http://localhost:1234/v1",
                              api_key="lm-studio"),
  )
  harness = HarnessBuilder(workspace=Path("."), model=local).build()
  ```

An unresolvable model string is reported at `build()` time as a
`BuilderError` problem, not at first turn.

## Bare-build defaults

`HarnessBuilder(workspace=..., model=...).build()` with no `with_*` calls
gives you:

| On by default | Off by default (opt in via `with_*`) |
| --- | --- |
| File reads: `read_file`, `glob`, `tree`, `grep` | `bash` |
| File writes (gated): `write_file`, `edit_file` | `net` (`web_search`, `fetch_url`, also gated) |
| Mode `auto` (gated tools run unprompted) | `memory` (`remember`, `recall`) |
| An in-memory session (nothing touches disk) | `skills` (`activate_skill`, `read_skill_file`) |
| | `tasks` (`update_tasks`, `ask_user`, `present_plan`) |
| | `jobs` (background job tools) |
| | `spawn` (`spawn_agent` — implied by `with_subagent`) |
| | LSP (manager + the six navigation tools) |
| | MCP servers, forge (Gitea/GitHub), hooks, extra instructions |

Everything with reach beyond reading/writing files in the workspace is
opt-in. The system prompt is gated the same way: a bare build's instructions
never advertise `spawn_agent`, `activate_skill`, or `recall`, because those
closures only register when the matching group is loaded.

`with_defaults()` flips every tool group on plus LSP-with-tools and the
user-level global instructions — the "give me everything the CLI has, minus
workspace scanning" shortcut. It is also the one builder call that performs
XDG reads; see [Sessions & state](sessions-and-state.md).

## Where to next

- The full `with_*` catalog: [Builder reference](builder.md)
- How turns and approval actually work: [Turns, modes & approval](turns.md)
- Registering your own tools: [Custom tools](custom-tools.md)
- Testing without a network: [Testing embedders](testing.md)
