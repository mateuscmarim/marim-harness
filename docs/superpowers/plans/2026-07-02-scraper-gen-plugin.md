# scraper-gen Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a bundled example plugin `examples/scraper-gen/` whose skill orchestrates planner/generator/healer sub-agents to generate self-validating Python web scrapers, plus a regression test keeping it parseable.

**Architecture:** Pure-markdown plugin (manifest + 3 agent definitions + 1 skill, no hooks/MCP, so no trust prompt). The skill drives `spawn_agent` rounds; agents encode the HTTP-first escalation ladder, self-testing script conventions, and the heal loop. A pytest regression test mirrors `tests/test_examples_playwright_plugin.py` so the example can't rot.

**Tech Stack:** Markdown/JSON plugin files; pytest for the regression test. Generated scrapers (at *runtime*, not in this repo) use uv + httpx + parsel + pydantic.

**Spec:** `docs/superpowers/specs/2026-07-02-scraper-gen-plugin-design.md`. Two deliberate deviations, both to match house conventions discovered in the repo:
1. **Location** is `examples/scraper-gen/` (not `examples/plugins/scraper-gen/`) — the existing bundled plugin lives at `examples/playwright/`.
2. **Skill directory** is `skills/web-scrapers/` (not `skills/scraper-gen/`) — a skill named after its plugin would render the stuttering qualified name `scraper-gen:scraper-gen`; the playwright plugin uses the same split (`playwright:e2e-tests`).
3. **Fan-out** uses plain multi-spawn (leave `background` unset) — the `spawn_agent` docstring says unset already fans out in parallel with better display; `background=True` is for fire-and-forget only.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check`, `uv run pyright`. Never bare `python`/`pip`.
- `requires-python >=3.10`: no 3.11+-only syntax in the test file.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (imports sorted).
- CI order before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Sub-agent `tools:` frontmatter may only use names from `src/marim_harness/tools/names.py` `SUBAGENT_TOOLS`: `read_file, glob, tree, grep`, the six LSP tools, `web_search, fetch_url`, `write_file, edit_file, bash`. Unknown names are silently dropped (`_parse_tools`), so a typo'd tool disappears — the regression test pins the exact sets to catch this.
- Agent/skill frontmatter must carry a non-empty `description`; if `name:` is present it must equal the file stem / directory name (`_parse_agent`, `_parse_skill`).
- This repo has concurrent agent sessions: `git add` only the files each task names, never `git add -A`.

---

### Task 1: Plugin scaffold — manifest, README, regression-test base

**Files:**
- Create: `examples/scraper-gen/.marim-plugin/plugin.json`
- Create: `examples/scraper-gen/README.md`
- Test: `tests/test_examples_scraper_gen_plugin.py`

**Interfaces:**
- Produces: plugin root `examples/scraper-gen/` with manifest name `scraper-gen`, version `0.1.0`. `PLUGIN_ROOT` constant in the test file, reused by Tasks 2–3 test additions.

- [ ] **Step 1: Write the failing test**

Create `tests/test_examples_scraper_gen_plugin.py`:

```python
"""Regression guard for the bundled ``examples/scraper-gen`` plugin: keep its
manifest, agents, and skill parseable by marim's own loaders so the example
can't silently rot as the plugin format evolves."""

from pathlib import Path

from marim_harness.plugins.manifest import load_manifest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "examples" / "scraper-gen"


def test_manifest_loads():
    m = load_manifest(PLUGIN_ROOT)
    assert m.name == "scraper-gen"
    assert m.version == "0.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_examples_scraper_gen_plugin.py -v`
Expected: FAIL — `ManifestError` (no readable manifest).

- [ ] **Step 3: Create the manifest**

Create `examples/scraper-gen/.marim-plugin/plugin.json`:

```json
{
  "name": "scraper-gen",
  "version": "0.1.0",
  "description": "Web-scraper generation agents (planner, generator, healer): plan extraction HTTP-first, generate self-validating Python scrapers, heal them against live sample runs.",
  "author": {"name": "marim"},
  "license": "MIT",
  "keywords": ["scraping", "extraction", "httpx", "parsel", "playwright"]
}
```

- [ ] **Step 4: Create the README**

Create `examples/scraper-gen/README.md`:

```markdown
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_examples_scraper_gen_plugin.py -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add examples/scraper-gen/.marim-plugin/plugin.json examples/scraper-gen/README.md tests/test_examples_scraper_gen_plugin.py
git commit -m "feat(examples): scaffold the scraper-gen plugin (manifest, README, test guard)"
```

---

### Task 2: The three agent definitions

**Files:**
- Create: `examples/scraper-gen/agents/planner.md`
- Create: `examples/scraper-gen/agents/generator.md`
- Create: `examples/scraper-gen/agents/healer.md`
- Modify: `tests/test_examples_scraper_gen_plugin.py` (append one test)

**Interfaces:**
- Consumes: `PLUGIN_ROOT` from Task 1's test file.
- Produces: agents spawnable as `scraper-gen:planner` / `scraper-gen:generator` / `scraper-gen:healer` (Task 3's skill references these exact names). The plan-block format defined in `planner.md` is the contract `generator.md`, `healer.md`, and the skill all restate.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_examples_scraper_gen_plugin.py`:

```python
def test_three_agents_parse_with_expected_tools():
    from marim_harness.workspace.agents import _parse_agent

    expected = {
        # Explores over HTTP (bash/fetch_url) and writes only specs/plan.md.
        "planner": {
            "read_file", "grep", "glob", "tree",
            "fetch_url", "web_search", "bash", "write_file",
        },
        # Writes and iterates on its one script.
        "generator": {
            "read_file", "grep", "glob", "tree", "write_file", "edit_file", "bash",
        },
        # Repairs existing scripts; deliberately no write_file.
        "healer": {"read_file", "grep", "glob", "tree", "edit_file", "bash"},
    }
    for name, tools in expected.items():
        defn = _parse_agent(
            "plugin:scraper-gen", PLUGIN_ROOT / "agents" / f"{name}.md", plugin="scraper-gen"
        )
        assert defn is not None, f"{name}.md failed to parse"
        assert defn.qualified_name == f"scraper-gen:{name}"
        assert set(defn.tools) == tools, f"{name} tools drifted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_examples_scraper_gen_plugin.py -v`
Expected: FAIL — `planner.md failed to parse` (file absent).

- [ ] **Step 3: Create planner.md**

Create `examples/scraper-gen/agents/planner.md` with exactly this content:

````markdown
---
name: planner
description: Explore a target website HTTP-first and write an extraction plan to specs/plan.md. The spawner grants a browser MCP server (mcp=[...]) only when one is available; work without it.
tools: read_file, grep, glob, tree, fetch_url, web_search, bash, write_file
---

You are an expert web-scraping planner. You explore a target site, decide the
cheapest reliable extraction strategy for each piece of data the user wants,
and write a precise plan that generator agents implement without further
exploration. You work from the `scrapers/` project directory.

**Escalation ladder — always in this order, per task:**

1. **http** — fetch the page plainly (`curl -sL` via bash, or `fetch_url`)
   with a real User-Agent. If the data is present in the HTML, record CSS or
   XPath selectors for every field and set `strategy: http`.
2. **api** — if the HTML is a JS shell (empty containers, skeleton markup),
   hunt the underlying data: inline state blobs (`__NEXT_DATA__`,
   `window.__INITIAL_STATE__`, `<script type="application/ld+json">`), and
   obvious XHR/fetch endpoints referenced in scripts. Fetch a candidate
   endpoint to confirm it returns the data, record the URL pattern, method,
   required headers, and JSON paths, and set `strategy: api`. Prefer this over
   a browser — it is faster and more stable.
3. **browser** — only when both fail. If the spawner granted a browser MCP
   server, explore live (navigate, snapshot, note the rendered structure and
   selectors) and mark the task `observed`. If no browser tools are in your
   toolset, still plan the task from static evidence, set
   `strategy: browser`, and mark it `inferred` — the healer's sample runs
   will correct the details.

**Politeness and limits — non-negotiable:**

- Fetch `robots.txt` first. Record its verdict for every path you plan to
  scrape. A disallowed path is planned as `blocked`, never worked around.
- Space your own exploration fetches ~1 second apart; never hammer a site.
- If a task requires logging in past an auth wall, solving a CAPTCHA, or
  accessing paywalled content, mark it `blocked` with the reason. Do not plan
  workarounds.
- If the site is unreachable, mark affected tasks `blocked` with the error.

**Output — write `specs/plan.md` (via write_file) in exactly this shape:**

```markdown
# Extraction plan: <site>

- base_url: <scheme://host>
- robots: <summary of relevant allow/disallow rules>
- politeness: delay 1.0s between requests, honest User-Agent
- browser_exploration: observed | unavailable (tasks inferred)

## Task: <kebab-case-name>
- script: scrape_<snake_case_name>.py
- strategy: http | api | browser | blocked
- entry_urls:
  - <url>
- pagination: <rule, e.g. "?page=N until empty" | none>
- min_records: <int — the validation floor for a sample run>
- fields:
  - <field_name>: type=<str|int|float|bool>, required=<yes|no>, from=<selector or JSON path>
- notes: <headers, quirks, blocked-reason, anything a generator needs>
```

One `## Task:` block per scraper. Keep tasks focused — one page type or
endpoint family each. Your final report to the spawner: the plan path plus a
one-line summary per task (name, strategy, field count, min_records).
````

- [ ] **Step 4: Create generator.md**

Create `examples/scraper-gen/agents/generator.md` with exactly this content:

````markdown
---
name: generator
description: Implement one task block from specs/plan.md as a standalone, self-validating Python scraper, and iterate until its own sample run passes.
tools: read_file, grep, glob, tree, write_file, edit_file, bash
---

You are an expert Python scraper developer. Your spawner gives you ONE task
block from `specs/plan.md` (plus the plan's header notes). You write exactly
one script — the `script:` named in the block — inside the `scrapers/`
project, run it, and iterate until it passes. You never touch other scripts
or the plan.

**Task block fields you receive:** `script`, `strategy` (http|api|browser),
`entry_urls`, `pagination`, `min_records`, `fields` (name, type, required,
selector/JSON path), `notes`, and header `politeness`/`base_url`.

**Script conventions — every script is its own test:**

- Standalone: httpx + parsel + pydantic only (`strategy: browser` may add
  playwright — then first ensure `uv add playwright` and
  `uv run playwright install chromium` have been run, and use its sync API).
- Define the record as an inline pydantic model built from the task's
  `fields` (required fields non-optional; types as declared).
- CLI: `--limit N` (max records, default 20) and `--out PATH` (JSONL output).
- Honest `User-Agent: marim-scraper-gen/0.1`, `time.sleep(1.0)` between
  requests, follow redirects, 30s timeout.
- Validate EVERY record through the model at extraction time; exit non-zero
  on any validation error or if fewer than `min_records` records were
  extracted (when `--limit` >= `min_records`).
- Exit codes: 0 success, 1 validation/count failure, 2 fetch or parse error.

Skeleton to follow (adapt fields, fetching, and parsing to the task):

```python
"""Scraper: <task-name>. Generated by scraper-gen. Strategy: <strategy>."""

import argparse
import json
import sys
import time

import httpx
from parsel import Selector
from pydantic import BaseModel, ValidationError


class Record(BaseModel):
    name: str
    price: float
    rating: float | None = None


def iter_records(client: httpx.Client, limit: int):
    url = "<entry url>"
    fetched = 0
    while url and fetched < limit:
        resp = client.get(url)
        resp.raise_for_status()
        sel = Selector(resp.text)
        for row in sel.css("<row selector>"):
            yield Record(
                name=row.css("<name selector>::text").get(default="").strip(),
                price=float(row.css("<price selector>::text").re_first(r"[\d.]+") or 0),
                rating=None,
            )
            fetched += 1
            if fetched >= limit:
                return
        url = sel.css("<next-page selector>::attr(href)").get()  # or None
        time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default="samples/<task>.jsonl")
    args = ap.parse_args()
    headers = {"User-Agent": "marim-scraper-gen/0.1"}
    records = []
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=30) as client:
            for rec in iter_records(client, args.limit):
                records.append(rec)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"fetch/parse error: {exc}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        print(f"validation error: {exc}", file=sys.stderr)
        return 1
    min_records = <min_records from the task>
    if args.limit >= min_records and len(records) < min_records:
        print(f"only {len(records)} records, need >= {min_records}", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(rec.model_dump_json() + "\n")
    print(f"wrote {len(records)} records to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Loop:** write the script → `uv run python <script> --limit 5 --out
samples/<task>.jsonl` → read stderr and the sample file → fix → rerun. Cap
yourself at 5 fix attempts; if still failing, put `# STATUS: failing —
<one-line reason>` as the first line after the docstring and report honestly
what you tried. Never mark a script passing that you did not just see exit 0.

Report back: script path, final exit code, record count, and any deviations
from the plan you had to make (changed selector, different endpoint).
````

- [ ] **Step 5: Create healer.md**

Create `examples/scraper-gen/agents/healer.md` with exactly this content:

````markdown
---
name: healer
description: Run every generated scraper fresh against the live site and repair failures until the whole set passes twice consecutively. Repairs existing scripts only — never creates new ones.
tools: read_file, grep, glob, tree, edit_file, bash
---

You are an expert scraper maintainer. You validate and repair the scripts in
the `scrapers/` project. You may edit existing scripts but never create new
files — if something seems missing, report it instead.

**Procedure:**

1. Read `specs/plan.md` for each task's strategy, schema, and `min_records`.
2. Run every `scrape_*.py`: `uv run python <script> --limit 10 --out
   samples/<task>.jsonl`. Where the task has pagination, vary the entry
   offset/page from what generation likely used, so selectors that only
   worked on page 1 get caught.
3. For each failure: read stderr and the script, diagnose (site drift, wrong
   selector, encoding, rate limiting, missing dep), and fix with edit_file.
   For `strategy: browser` scripts, confirm playwright is a project dep
   (`grep playwright pyproject.toml`) and browsers are installed
   (`uv run playwright install chromium`) before blaming the code.
4. Re-run after every fix. The set is done only when EVERY script passes
   twice consecutively — a pass-then-fail counts as failing (rate limiting,
   unstable ordering) and must be fixed or reported, never shipped.
5. Space runs politely; keep `--limit` modest (10) so heal iterations do not
   hammer the site.

A script that still fails after ~5 fix attempts: ensure its first line after
the docstring is `# STATUS: failing — <one-line reason>` (add or update it)
and move on. Honesty over green.

Report back, per script: pass/fail, record count from the last run, what you
changed (one line each), and any scripts left failing with the reason.
````

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_examples_scraper_gen_plugin.py -v`
Expected: PASS (2 passed). If `assert set(defn.tools) == tools` fails with a *smaller* set than expected, a tool name is typo'd in the frontmatter (unknown names are silently dropped).

- [ ] **Step 7: Commit**

```bash
git add examples/scraper-gen/agents/planner.md examples/scraper-gen/agents/generator.md examples/scraper-gen/agents/healer.md tests/test_examples_scraper_gen_plugin.py
git commit -m "feat(examples): scraper-gen planner/generator/healer agent definitions"
```

---

### Task 3: The orchestrating skill

**Files:**
- Create: `examples/scraper-gen/skills/web-scrapers/SKILL.md`
- Modify: `tests/test_examples_scraper_gen_plugin.py` (append one test)

**Interfaces:**
- Consumes: agent names `scraper-gen:planner`, `scraper-gen:generator`, `scraper-gen:healer` (Task 2); the plan-block format from `planner.md`.
- Produces: skill `scraper-gen:web-scrapers`, lazily loaded by description match.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_examples_scraper_gen_plugin.py`:

```python
def test_web_scrapers_skill_parses():
    from marim_harness.workspace.skills import _parse_skill

    # The workflow lives in a lazy-loaded skill; its description must mention
    # scraping/extraction so the model triggers it on scraper requests.
    skill = _parse_skill(
        "plugin:scraper-gen", PLUGIN_ROOT / "skills" / "web-scrapers", plugin="scraper-gen"
    )
    assert skill is not None
    assert skill.qualified_name == "scraper-gen:web-scrapers"
    desc = skill.description.lower()
    assert any(kw in desc for kw in ("scrap", "extract", "crawl"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_examples_scraper_gen_plugin.py -v`
Expected: FAIL — `assert skill is not None` (directory absent).

- [ ] **Step 3: Create SKILL.md**

Create `examples/scraper-gen/skills/web-scrapers/SKILL.md` with exactly this content:

````markdown
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
````

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_examples_scraper_gen_plugin.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add examples/scraper-gen/skills/web-scrapers/SKILL.md tests/test_examples_scraper_gen_plugin.py
git commit -m "feat(examples): scraper-gen orchestrating skill (web-scrapers)"
```

---

### Task 4: Full verification sweep

**Files:**
- None created; verification only.

**Interfaces:**
- Consumes: everything from Tasks 1–3.

- [ ] **Step 1: CLI validation of the plugin**

Run: `uv run marim plugin validate examples/scraper-gen`
Expected output: `valid: scraper-gen (0.1.0) — 1 skills, 3 agents, 0 hooks, 0 MCP servers`

- [ ] **Step 2: Lint**

Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 3: Type-check**

Run: `uv run pyright`
Expected: `0 errors, 0 warnings` (pyright covers src only; the new test file is still lint-covered).

- [ ] **Step 4: Full test suite**

Run: `uv run pytest`
Expected: all tests pass, including the 3 in `tests/test_examples_scraper_gen_plugin.py`.

- [ ] **Step 5: Nothing to commit — confirm clean tree for these files**

Run: `git status --porcelain examples/scraper-gen tests/test_examples_scraper_gen_plugin.py`
Expected: empty output. (Other sessions may dirty unrelated files; ignore those.)

---

## Out of scope (explicitly)

- No changes to marim source (`src/`) — the plugin uses existing machinery.
- No `mcp.json` in the plugin (spec decision: avoid duplicating a user's own
  browser MCP server; namespacing means plugin servers never dedupe).
- No AGENTS.md (always-on instructions) — the workflow is lazily loaded via
  the skill, keeping zero always-on prompt weight.
- Manual dogfood run against a live site (spec's manual-testing note) is a
  post-merge activity, not a plan task.
