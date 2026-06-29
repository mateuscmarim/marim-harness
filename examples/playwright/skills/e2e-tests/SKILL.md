---
name: e2e-tests
description: Use when asked to write, generate, plan, or fix end-to-end / browser / Playwright tests for a web app. Orchestrates the planner, generator, and healer sub-agents.
---

# Playwright end-to-end testing

You drive three sub-agents — `playwright:planner`, `playwright:generator`,
`playwright:healer` — that plan, write, and repair Playwright tests by driving a
real browser through the `playwright_test` MCP server.

## Precondition check — DO THIS FIRST, before spawning anything

These agents only work inside a real Playwright project. Before any spawn, verify
the **current workspace** has all of:

- `@playwright/test` resolvable (a `node_modules/@playwright/test` or a
  `package.json` that depends on it), and
- a Playwright config (`playwright.config.{ts,js,mjs}`), and
- a seed spec (commonly `tests/seed.spec.ts`).

Quick check: `ls package.json playwright.config.* tests/seed.spec.ts 2>&1`.

**If any are missing, STOP. Do not improvise.** Specifically you must NOT:

- run `npm init`, `npm install`, or any package install in the workspace,
- create `package.json` / `node_modules` / config files in the host repo,
- edit the host repo's `.gitignore`.

Polluting an unrelated repo (e.g. a Python project) with a Node/Playwright
setup is the failure this skill exists to prevent. Instead, tell the user:

> "This workspace isn't a Playwright project. Run me inside a JS/TS project that
> has `@playwright/test` + a config + a seed spec, or say the word and I'll
> scaffold a **dedicated** project in a subdirectory (e.g. `e2e/`) — I won't
> install into this repo."

Only scaffold after the user agrees, and only inside a clearly separate
subdirectory you create for it.

## The loop (once preconditions hold)

Spawn every agent with the MCP grant — without it they have no browser:

1. **Plan.** `spawn_agent(type="playwright:planner", mcp=["playwright_test"],
   task="Plan tests for <app URL>. Seed: tests/seed.spec.ts. Keep it focused:
   N core scenarios.")` → a Markdown plan under `specs/`.
2. **Generate.** Read the saved plan. For each scenario, one spawn:
   `spawn_agent(type="playwright:generator", mcp=["playwright_test"], task=<the
   scenario: suite name, test name, target file under tests/, seed file>)`.
   Fan these out in parallel.
3. **Heal.** `spawn_agent(type="playwright:healer", mcp=["playwright_test"],
   task="Run the suite and fix failing tests")`. The healer edits spec files, so
   it needs gated `edit_file`/`write_file` — that means the session must be in
   **auto** mode. If you're not in auto mode, say so rather than letting the
   healer fail silently.

## Notes

- Run one scenario per generator spawn; don't batch a whole plan into one.
- The agents write/edit **only test artifacts** (`specs/`, `tests/`). They never
  touch project setup — that's the precondition's job, gated on user consent.
- This relies on marim's MCP + sub-agent machinery, so it applies on the
  `openrouter`/`local`/`google` providers, not `claude-cli` (a launcher).
