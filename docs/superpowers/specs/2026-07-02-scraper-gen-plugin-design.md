# scraper-gen plugin — design

**Date:** 2026-07-02
**Status:** approved design, pre-implementation

## What this is

A marim plugin that generates webpage scrapers from a user's plain-language data
request, modeled on the Playwright e2e-tests skill pattern: an orchestrating
skill drives three sub-agent roles — **planner** (explores the site, writes the
extraction plan), **generator** (writes one scraper script per plan task), and
**healer** (runs sample scrapes and repairs until everything passes). Browser
automation is used only when plain HTTP and API extraction fail.

It doubles as the reference plugin for marim's plugin system, exercising
namespaced skills, custom agent definitions, and per-spawn MCP granting.

## Decisions (with reasons)

- **Output form: standalone Python scripts** using httpx + parsel/BeautifulSoup
  + pydantic, with playwright-for-Python only for tasks that need a browser.
  Fits marim's uv/Python world; the healer validates by running them.
- **Exploration: HTTP-first with auto-escalation.** Plain fetch → hunt
  underlying JSON/API endpoints → browser only as last resort. Each task
  records its `strategy: http | api | browser` in the plan.
- **Validation: schema + sample checks.** Each task declares a field schema and
  minimum record count; scripts validate themselves and exit non-zero on
  failure. Fully mechanical pass/fail for the healer.
- **Scaffolding: self-contained `scrapers/` uv project**, created only with
  user consent. Never pollutes the host repo (the e2e skill's core guardrail).
- **No bundled `mcp.json`.** Plugin MCP servers are namespaced
  `<plugin>_<server>` (`plugins/discovery.py`), so a bundled Playwright server
  would *duplicate* — not dedupe against — a user's own `playwright` server
  (the `load_mcp_config` name-clash precedence never fires across different
  names), and plugin-server toggles via `/mcp` don't persist. Instead the skill
  detects any available browser-MCP at run time and grants it to the planner.
  Side benefit: with no MCP/hooks the plugin has no code-executing parts and
  installs without a trust prompt.
- **Location: `examples/plugins/scraper-gen/` in the marim-harness repo**,
  installed for dogfooding via `marim plugin install <path> --link`.

## Plugin layout

    examples/plugins/scraper-gen/
    ├── .marim-plugin/plugin.json     # {"name": "scraper-gen", ...}
    ├── skills/scraper-gen/SKILL.md   # the orchestrator
    ├── agents/planner.md             # scraper-gen:planner
    ├── agents/generator.md           # scraper-gen:generator
    └── agents/healer.md              # scraper-gen:healer

Pure markdown — no hooks, no mcp.json.

## Scaffolded workspace

    scrapers/
    ├── pyproject.toml       # httpx, parsel, pydantic; playwright only if needed
    ├── specs/plan.md        # planner output — the downstream contract
    ├── scrape_<task>.py     # one self-contained script per plan task
    └── samples/<task>.jsonl # sample-run outputs

**Each script is its own test.** Every script defines its pydantic record model
inline, takes `--limit N --out samples/<task>.jsonl`, validates every record at
extraction time, and exits non-zero on validation failure or a record count
below the plan's minimum. Healer pass/fail = exit code + stderr. Scripts are
standalone by design (duplication between them is acceptable); no shared
`common.py` until a real need appears.

## Agent contracts

### planner (`agents/planner.md`)

- `tools:` read/search + bash + net tools + `write_file` (prompt-scoped to
  `specs/`). If a browser MCP is connected in the session, the skill grants
  that server to this spawn.
- System prompt encodes the escalation ladder: HTTP fetch first → if the HTML
  carries the data, `strategy: http` with CSS/XPath selectors → if it's a JS
  shell, hunt JSON/API endpoints (`__NEXT_DATA__`/state blobs, script tags,
  obvious XHR paths), prefer `strategy: api` → only when both fail,
  `strategy: browser` (live MCP exploration if granted; otherwise marked
  *inferred, not observed*). Checks `robots.txt` and notes disallowed paths.
- Output contract — `specs/plan.md`, one block per task: task name, target
  script path, strategy, entry URLs, pagination rule, field schema
  (name/type/required), minimum record count, selector/endpoint notes.

### generator (`agents/generator.md`)

- `tools:` read + write + edit + bash.
- Input: one task block pasted into the spawn prompt plus the plan's header
  notes. Writes the script, runs it with a small `--limit` against the real
  site, iterates until its own script exits clean (cap ~5 attempts). Never
  touches other tasks' scripts or the plan.

### healer (`agents/healer.md`)

- `tools:` read + edit + bash. **No write** — repairs existing scripts, never
  creates new ones.
- Runs every script fresh, on different pages/offsets than generation used
  where pagination allows (catches selectors that only worked on page 1).
  Fixes failures and re-runs until all scripts pass **twice consecutively**.
- Also the standalone re-entry point: "my scrapers broke" re-invokes the skill
  in heal mode, skipping plan/generate.

**Mode caveat:** `bash`/`write`/`edit` are gated tools and marim strips them
from sub-agents outside `auto` mode (`effective_tools`). The skill states up
front that it requires `auto` mode and stops otherwise.

## Orchestration flow (SKILL.md)

1. **Mode & capability check.** Confirm `auto` mode (else warn and stop).
   Detect any connected browser-MCP server — decides the planner's grant and
   is recorded so the plan can flag inferred-vs-observed browser tasks.
2. **Preconditions & consent.** No `scrapers/` → ask before scaffolding
   (`uv init` + deps inside `scrapers/` only). Existing `scrapers/` with a
   plan → ask whether this is a new extraction request (extend plan, go to 3)
   or a repair (skip to 5).
3. **Plan.** Spawn `scraper-gen:planner` with the user's request verbatim.
   Read the returned `specs/plan.md`, summarize tasks/strategies/schemas to
   the user, and get a nod before generating (cheap moment to catch intent
   mismatches).
4. **Generate — fan out.** One `scraper-gen:generator` spawn per task. Tasks
   own distinct script files, so spawn all as `background=True` jobs in one
   round and `wait_for_job` each; tasks sharing a file (rare) run
   sequentially. Snapshot `find scrapers -type f` before spawning.
5. **Heal.** One `scraper-gen:healer` spawn: run every script with a modest
   `--limit`, on different pages than generation where possible, fix until
   everything passes twice in a row. If playwright is needed, verify the dep
   is in `pyproject.toml` and browsers are installed
   (`uv run playwright install chromium`).
6. **Report.** Diff the file snapshot (flag unaccounted files). Deliver: table
   of task → script → strategy → sample record count; how to run each script
   for real; and, if any task fell back to inferred-browser planning, a
   one-time note on adding a Playwright MCP globally for better future runs.

## Guardrails & error handling

- **Scope containment.** Sub-agents write only inside `scrapers/`; the skill
  never installs or creates files in the host repo. Pre-generate snapshot +
  post-heal diff catches drift.
- **Politeness & ethics (encoded in agent prompts).** Planner checks
  `robots.txt`; disallowed paths are surfaced, not silently scraped. Scripts
  set an honest User-Agent, sleep between requests (default ~1s, noted in the
  plan), and cap sample runs with `--limit`. The skill refuses auth-wall
  bypass, CAPTCHA circumvention, and paywalled content.
- **Failure handling per phase.**
  - Planner can't reach / hostile site: task marked `blocked` + reason in the
    plan; skill reports instead of generating.
  - Generator exhausts iterations: reports what it tried, leaves
    `# STATUS: failing` at the top of the script; healer gets a shot; final
    report lists it honestly as broken.
  - Flake rule: pass-then-fail on consecutive runs counts as failing (rate
    limiting, unstable ordering) — fixed or reported, never "works."
  - Background job dies: `wait_for_job` surfaces it; re-spawn that task once,
    then report.
- **Session hygiene.** Browser MCP exploration stays in the planner only;
  generators and the healer validate exclusively by running the scripts, so
  results stay reproducible outside the session.

## Testing

- `marim plugin validate examples/plugins/scraper-gen` passes.
- Plugin discovery unit coverage if gaps appear (namespacing of the three
  agents, skill discovery) — most is already covered by existing tests.
- Manual dogfood: install with `--link`, run the skill against a simple static
  site (http strategy) and a JS-rendered site (api/browser strategy), verify
  scaffold consent, plan summary gate, fan-out, heal loop, and final report.
