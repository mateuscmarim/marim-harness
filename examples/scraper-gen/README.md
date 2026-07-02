# scraper-gen

A marim plugin that turns a plain-language data request ("get me every
product's name, price, and rating from this site") into working, self-testing
Python scrapers. Three sub-agents do the work, driven by the
`scraper-gen:web-scrapers` skill:

- **planner** — explores the target site HTTP-first (plain fetch → underlying
  JSON/API endpoints → browser only as a last resort) and writes an extraction
  plan with a field schema per task.
- **generator** — writes one standalone Python script per plan task
  (httpx + parsel + pydantic) and iterates until its own sample run passes.
- **healer** — runs every script fresh against the live site and repairs
  failures until the whole set passes twice in a row. Also the re-entry point
  when a site changes and scrapers break later.

Scrapers live in a self-contained `scrapers/` uv project that the skill
scaffolds (with your consent) — it never installs anything into your repo.

## Install

    marim plugin install examples/scraper-gen --link

Pure markdown — no hooks, no MCP servers — so no trust prompt. If a browser
MCP server (e.g. Playwright MCP) is connected in your session, the skill
grants it to the planner for live exploration of JS-rendered sites; without
one, JS-heavy tasks are planned as inferred and verified by the healer's
sample runs instead.

## Requirements

- Session in **auto** mode (the agents need gated `bash`/`write_file`/
  `edit_file`, which marim strips from sub-agents in other modes).
- `uv` on PATH (scaffolding and script runs go through it).
- Native providers (`openrouter`/`local`/`google`) — not `claude-cli`, which
  is a launcher and bypasses marim's sub-agent machinery.
