# playwright plugin

A marim plugin bundling Playwright's test agents — **planner**, **generator**,
and **healer** — so marim can plan, write, and repair end-to-end browser tests.
It is a port of Playwright's official agents (`npx playwright init-agents`) onto
marim's sub-agent + MCP machinery.

## What's inside

    examples/playwright/
    ├── .marim-plugin/plugin.json        # manifest
    ├── agents/planner.md                # → playwright:planner
    ├── agents/generator.md              # → playwright:generator
    ├── agents/healer.md                 # → playwright:healer
    ├── skills/e2e-tests/SKILL.md        # → playwright:e2e-tests (the workflow)
    ├── mcp.json                         # the playwright_test MCP server
    ├── AGENTS.md                        # always-on grant + project-required guardrail
    └── README.md

The `test` server in `mcp.json` is namespaced by marim to `playwright_test`, so
its tools surface as `playwright_test_browser_*`, `playwright_test_test_run`,
etc., and you grant it to a sub-agent with `mcp=["playwright_test"]`.

## Install

    marim plugin install ./examples/playwright --trust

`--trust` is needed because the plugin ships an MCP server (it runs `npx` in your
workspace). Use `--scope project` to commit it into a repo's `.marim/plugins/`
for sharing, or `--link` to develop against this directory in place. Then:

    marim plugin list
    marim plugin enable playwright      # if not already enabled

## Use

In a project that has `@playwright/test` and a `tests/seed.spec.ts` seed (run
`npx playwright init-agents --loop=claude` once to scaffold the seed + `specs/`),
ask marim to plan/generate/heal tests — e.g. "write end-to-end tests for
<app URL>". The `playwright:e2e-tests` skill carries the workflow; the main agent
spawns the three sub-agents, granting `mcp=["playwright_test"]` each time.

**Run it inside an actual Playwright project.** If the workspace isn't one, the
agents are told to stop and ask rather than `npm install` into it — pointing them
at a non-Node repo (e.g. this Python one) otherwise sends them scaffolding
Playwright into a repo that shouldn't have it. The healer also needs **auto** mode
to edit files, and this applies on the openrouter/local/google providers, not
`claude-cli`.
