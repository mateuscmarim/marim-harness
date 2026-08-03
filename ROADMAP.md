# Roadmap

Where marim-harness is headed. This is direction, not a queue: there are no
dates, items can move between buckets, and things that aren't listed here
still ship all the time. The one fixed point is the identity — *a coding
agent for your terminal, and an embeddable harness for building your own* —
and items earn a place here by making that story stronger.

Feedback on any of this is welcome — open an issue.

## Now

- **Documentation site** — publish the existing guides and reference pages
  (`docs/`) as a proper site at docs.marim.dev (mkdocs-material).
- **Small, frequent releases** — batch the unreleased changelog into the next
  version rather than saving up for a big one. v0.3.0 (the usage ledger, the
  session picker, `marim serve qr` pairing, interactive project trust, the
  approval-panel repair) followed v0.2.0 by five days.

## Next

- **Public API and stability statement** — declare exactly which surface the
  embedding story covers (`HarnessBuilder`, `Harness`, `Mode`, `AgentDef`,
  the custom-tool path), what semver means for it pre-1.0, and what 1.0 will
  promise. If you build on marim, this is the contract you're owed.
- **Contribution flow on GitHub** — the development forge is private and
  [github.com/mateuscmarim/marim-harness](https://github.com/mateuscmarim/marim-harness)
  is a mirror. Define how issues and PRs opened there get triaged and landed,
  and automate the mirror sync.
- **`marim import claude`** — one command that finds an existing Claude Code
  setup and carries it over. The **memory store** ships today (`marim import
  claude`, see `docs/guides/skills-and-memory.md`); hooks configuration,
  skills, sub-agents, MCP servers and user-level `CLAUDE.md` are still to do.
  marim's formats deliberately mirror Claude Code's so user investment stays
  portable; an importer makes that promise checkable — any format drift shows
  up as an import gap, not as a surprise on switching day.
- **`gh` forge backend** — the forge tools (list/view/check out PRs, CI
  status) currently ship a `tea`/Gitea backend behind a forge-agnostic
  protocol; a GitHub backend via the `gh` CLI is the obvious drop-in.
- **Direct Anthropic / OpenAI providers** — today's providers are OpenRouter
  (default), local OpenAI-compatible servers, Google, and `claude-cli`.
  "Bring your own Anthropic or OpenAI key" should work without a gateway in
  between.

## Later / exploring

- **Capability ecosystem** — `with_capability()` attaches any pydantic-ai
  capability (e.g. Pydantic AI Harness modules) to the embedded agent.
  Deepen that: approval-gating for capability-provided tools, and a cookbook
  of composed-harness examples.
- **Workflow resumability** — dynamic workflows survive an interrupted
  session and resume from a journal instead of restarting.
- **TUI visual refresh** — a broader restyle of the Textual interface.
- **Windows support** — currently developed and tested on Linux/macOS;
  Windows is unverified. Exploratory until someone (maybe you) wants to
  champion it.

## Non-goals

- **Agent-as-a-service as the product.** `marim serve` exists and will keep
  working, but the center of gravity is the in-process Python library and
  the terminal UI — not a hosted server platform.
- **Roadmap dates.** This is a one-maintainer project; buckets communicate
  intent, dates would communicate fiction.
