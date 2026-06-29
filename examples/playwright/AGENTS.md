# Playwright test agents

This plugin provides three sub-agents that plan, generate, and heal Playwright
end-to-end tests by driving a real browser through the `playwright_test` MCP
server. It is a marim port of Playwright's official test agents
(`npx playwright init-agents`).

## The agents

- `playwright:planner` — explores a running web app and saves a Markdown test
  plan under `specs/`.
- `playwright:generator` — turns one plan scenario into a single executable
  `*.spec.ts`, performing the steps live in the browser.
- `playwright:healer` — runs the suite, debugs failures, and edits the specs
  until they pass.

## How to drive them

The browser/test tooling lives in the `playwright_test` MCP server. A sub-agent
only receives it when you grant it at spawn time. **Always spawn these agents
with `mcp=["playwright_test"]`** — without it they have no browser and will stop
and say so. Run one scenario per `generator` spawn; fan them out in parallel.

Typical loop:

1. `spawn_agent(type="playwright:planner", mcp=["playwright_test"], task="Plan
   tests for <app URL>. Seed file: tests/seed.spec.ts")` → a plan in `specs/`.
2. For each scenario in the plan:
   `spawn_agent(type="playwright:generator", mcp=["playwright_test"], task=<the
   scenario, with suite name, test name, target file, seed file>)`.
3. `spawn_agent(type="playwright:healer", mcp=["playwright_test"], task="Run the
   suite and fix any failing tests")`.

## Prerequisites (in the target project)

- `@playwright/test` installed and a Playwright config present.
- A seed file at `tests/seed.spec.ts` that sets up the environment. The
  fastest way to scaffold the seed + `specs/` directory is to run
  `npx playwright init-agents --loop=claude` once in the project; you can
  ignore the `.claude/agents/` and `.mcp.json` it also writes — this plugin
  supplies those for marim.
- Node/`npx` on PATH (the MCP server runs `npx playwright run-test-mcp-server`).

## Caveats

- **Healer needs auto mode.** It edits spec files with marim's gated
  `edit_file`/`write_file`, which sub-agents only receive in **auto** mode. In
  ask/plan mode the healer can run and debug tests but cannot apply fixes.
- **Trust required.** This plugin ships an MCP server, so it loads only once the
  plugin is trusted (`marim plugin install … --trust`, or accept the trust
  prompt). The server launches `npx` in your workspace.
- **Provider matters.** This relies on marim's own MCP + sub-agent machinery, so
  it applies on the `openrouter`, `local`, and `google` providers. Under the
  `claude-cli` provider marim is only a launcher (Claude runs its own loop), so
  this plugin does not take effect there.
