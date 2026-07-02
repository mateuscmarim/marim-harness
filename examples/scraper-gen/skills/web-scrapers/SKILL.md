---
name: web-scrapers
description: Use when asked to scrape a website, extract structured data from web pages, build/generate a web scraper or crawler, or fix broken scrapers. Orchestrates the planner, generator, and healer sub-agents.
---

# Generating web scrapers

You drive three sub-agents — `scraper-gen:planner`, `scraper-gen:generator`,
`scraper-gen:healer` — that plan, write, and repair standalone Python
scrapers (httpx + parsel + pydantic; playwright only when a task truly needs
a browser). Every script is its own test: it validates records against a
pydantic schema and exits non-zero on failure, so "the suite is green" means
every script exits 0.

## Step 0 — mode & capability check, before anything else

- The agents need gated `bash`/`write_file`/`edit_file`, which marim strips
  from sub-agents outside **auto** mode. Not in auto mode? Say so and stop.
- Check the MCP servers enabled in this session (the sub-agents index lists
  them). If one is a browser-automation server (Playwright MCP or similar),
  remember its name — you will grant it to the planner. If none, proceed
  anyway and note it: browser-strategy tasks will be planned as *inferred*
  and corrected by the healer's live runs.
- This skill relies on marim's sub-agent + MCP machinery, so it applies on
  the native providers (`openrouter`/`local`/`google`), not `claude-cli`.

## Step 1 — preconditions & consent

Scrapers live in a self-contained `scrapers/` uv project. Check for
`scrapers/pyproject.toml` and `scrapers/specs/plan.md`:

- **Neither exists:** ask the user before scaffolding — "I'll create a
  self-contained `scrapers/` project (uv + httpx/parsel/pydantic) — nothing
  is installed into your repo. OK?" Only after consent:
  `uv init scrapers && cd scrapers && uv add httpx parsel pydantic && mkdir -p specs samples`.
  Never run installs or create files anywhere else in the workspace.
- **Both exist:** ask whether this is a *new extraction request* (extend the
  plan — go to Step 2) or *repair of broken scrapers* (skip to Step 4).

## Step 2 — plan

Snapshot existing files first: `find scrapers -type f | sort` (you will diff
against this at the end to catch drift).

Spawn the planner with the user's request verbatim:
`spawn_agent(type="scraper-gen:planner", mcp=[<browser server>] if available,
task="Plan scrapers for: <the user's data request>. Work in scrapers/; write
specs/plan.md.")`

Read the returned `specs/plan.md` yourself, then show the user a short
summary — each task's name, strategy (http/api/browser/blocked), fields, and
min_records — and get their nod before generating. This is the cheap moment
to catch "I wanted the price *history*, not the current price." Surface any
`blocked` tasks (robots.txt, auth walls, CAPTCHAs) now; do not generate them.

## Step 3 — generate, fan out

One generator spawn per non-blocked task. Each task owns its own script
file, so they are independent — spawn them all in one turn (leave
`background` unset; that already runs them in parallel):
`spawn_agent(type="scraper-gen:generator", task=<the full task block pasted
verbatim, plus the plan's header lines (base_url, robots, politeness)>,
returns="script path, final exit code, record count, deviations from plan")`.

Don't batch two tasks into one spawn. If two tasks ever share a script file,
run those sequentially.

## Step 4 — heal

One spawn: `spawn_agent(type="scraper-gen:healer", task="Run every
scrape_*.py in scrapers/ with --limit 10, on different pages/offsets than
generation where pagination allows; fix failures until every script passes
twice consecutively.")`

Entering here in repair mode (from Step 1), pass along what the user said is
broken.

## Step 5 — report

- Diff `find scrapers -type f | sort` against the Step 2 snapshot; flag any
  file not accounted for by the plan instead of folding it in silently.
- Give the user: a table of task → script → strategy → sample record count;
  how to run each script for real (`cd scrapers && uv run python
  scrape_<task>.py --limit 0 --out data.jsonl` — note `--limit` default is
  20); any scripts left `# STATUS: failing` and why; any `blocked` tasks and
  why.
- If browser tasks were planned *inferred* (no browser MCP), add once: a
  Playwright MCP server registered globally would let the planner explore
  JS-rendered pages live on future runs.

## Guardrails

- Sub-agents write only inside `scrapers/`. You never install or create
  files in the host repo — scaffolding is consent-gated and confined to
  `scrapers/`.
- Politeness is plan-enforced: honest User-Agent, ~1s delays, modest
  `--limit` during generation/healing. Respect robots.txt verdicts.
- Refuse tasks requiring auth-wall bypass, CAPTCHA circumvention, or
  paywalled content — say so instead of generating workarounds.
- A generator/healer that gives up leaves `# STATUS: failing` in the script;
  report those honestly, never as green.
- If a spawn dies (job error, not a task failure), re-spawn that one task
  once, then report.
