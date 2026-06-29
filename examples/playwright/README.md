# playwright plugin

A marim plugin bundling Playwright's test agents — **planner**, **generator**,
and **healer** — so marim can plan, write, and repair end-to-end browser tests.
It is a port of Playwright's official agents (`npx playwright init-agents`) onto
marim's sub-agent + MCP machinery.

## What's inside

    examples/playwright/
    ├── .marim-plugin/plugin.json   # manifest
    ├── agents/planner.md           # → playwright:planner
    ├── agents/generator.md         # → playwright:generator
    ├── agents/healer.md            # → playwright:healer
    ├── mcp.json                    # the playwright_test MCP server
    ├── AGENTS.md                   # how the main agent should drive the agents
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
ask marim to plan/generate/heal tests. The main agent spawns the three
sub-agents, granting `mcp=["playwright_test"]` each time. See `AGENTS.md` for the
exact loop and the caveats (the healer needs **auto** mode to edit files; this
applies on the openrouter/local/google providers, not `claude-cli`).
