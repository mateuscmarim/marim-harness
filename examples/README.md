# Examples

Worked, self-contained reference material you copy, adapt, or run — not code
that marim bundles or auto-loads. Most subdirectories are installable **plugins**
(`marim plugin install <path>`; see [`docs/plugins.md`](../docs/plugins.md) for
scopes, trust, and `--link`); [`embedding`](./embedding/) is a different kind of
sample — Python that embeds marim as a library via `HarnessBuilder`.

## Plugins

| Plugin | What it is | Bundles |
|--------|------------|---------|
| [`playwright`](./playwright/) | Playwright test agents (planner, generator, healer) driven by the Playwright run-test MCP server. | 1 skill · 3 agents · 1 MCP server |
| [`scraper-gen`](./scraper-gen/) | Turns a plain-language data request into self-validating Python scrapers via planner/generator/healer agents. | 1 skill · 3 agents |
| [`superpowers`](./superpowers/) | Trimmed vendor of [obra/superpowers](https://github.com/obra/superpowers): TDD, debugging, brainstorming, and collaboration skills, with a marim-native SessionStart hook. | 14 skills · 1 hook |
| [`agentmemory`](./agentmemory/) | Persistent agent memory — lifecycle auto-capture plus `memory_*` recall/save tools — wiring marim into an external agentmemory install. | 9 hooks · 1 MCP server |

## Install

```bash
marim plugin install ./examples/playwright   --trust                 # skills + agents + MCP
marim plugin install ./examples/scraper-gen  --link                  # pure markdown, no trust prompt
marim plugin install ./examples/superpowers  --link                  # skills + a SessionStart hook
marim plugin install ./examples/agentmemory  --scope global --trust  # hooks + MCP; needs external setup
```

A plugin that bundles hooks or MCP servers is executable surface, so its install
prompts once for trust. `--link` installs in place (edits to the source are
picked up); omit it to copy. `superpowers` and `agentmemory` depend on external
tooling — read their READMEs for prerequisites before installing.

These live under `examples/` rather than `plugins/` on purpose: marim's own
plugin engine is `src/marim_harness/plugins/`, and nothing here is loaded until
you install it.

## Embedding marim as a library

[`embedding`](./embedding/) is not a plugin — it is a runnable sample that builds
its own agent with `HarnessBuilder` (a tiny architecture-decision assistant with
a gated and a read-only custom tool). It is the smallest complete tour of the SDK
surface an embedder reaches for.

```bash
uv run pytest tests/test_examples_embedding.py          # network-free guard test
ANTHROPIC_API_KEY=... uv run python examples/embedding/assistant.py "…"   # run it live
```

See [`docs/embedding.md`](../docs/embedding.md) and [`docs/sdk/`](../docs/sdk/)
for the full SDK docs.
