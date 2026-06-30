# Playwright test agents

This plugin provides three sub-agents — `playwright:planner`, `playwright:generator`,
`playwright:healer` — that plan, generate, and heal Playwright end-to-end tests by
driving a real browser through the `playwright_test` MCP server. It is a marim port
of Playwright's official test agents (`npx playwright init-agents`).

**For the full workflow, use the `playwright:e2e-tests` skill** — it carries the
plan → generate → heal procedure.

## Three rules that always apply

1. **Grant the MCP server on every spawn.** These agents have no browser unless you
   pass `mcp=["playwright_test"]` to `spawn_agent`. Without it they stop and say so.

2. **Never run these agents in parallel — one browser session is shared.** All
   spawns share a single `playwright_test` server and therefore a single browser
   page, with no per-caller isolation. Two agents driving at once reset and
   navigate the page out from under each other ("Must setup test before
   interacting with the page" / "page session lost"). Run the planner, then each
   generator, then the healer **strictly one at a time** — wait for each to finish
   before spawning the next.

3. **Require a real Playwright project — never scaffold into the host repo.** The
   `playwright_test` server needs `@playwright/test` + a `playwright.config.*` + a
   seed spec in the workspace. If they're missing, **STOP and tell the user** — do
   NOT run `npm init`/`npm install`, create `package.json`/`node_modules`, or edit
   `.gitignore` in this repo. Scaffolding a Node/Playwright setup into an unrelated
   project (e.g. a Python repo) is a destructive mistake. Offer to set up a
   **dedicated** subdirectory only after the user agrees.

## Caveats

- The healer edits spec files with gated `edit_file`/`write_file`, so it only works
  in **auto** mode.
- Trust is required (this plugin ships an MCP server that launches `npx`).
- Applies on the `openrouter`/`local`/`google` providers, not `claude-cli`
  (a launcher where marim's own MCP/sub-agents don't take effect).
