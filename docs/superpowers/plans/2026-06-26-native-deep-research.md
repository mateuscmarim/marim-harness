# Native Deep-Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a built-in `deep-research` skill + `researcher` sub-agent so marim's main agent fans out into parallel workers, adversarially verifies claims, and synthesizes a cited report.

**Architecture:** Add a package-relative `builtin` discovery root to both `skill_roots()` and `agent_roots()` (precedence project > global > builtin > plugins), then drop two markdown assets — `builtin/skills/deep-research/SKILL.md` (orchestration policy run by the main agent) and `builtin/agents/researcher.md` (a read-only web worker) — into that root. No changes to `spawn_agent`, the turn loop, or config flags.

**Tech Stack:** Python ≥3.10, Pydantic AI, hatchling build backend, pytest, ruff, pyright. Discovery reuses the existing `cached_discover` machinery in `workspace/skills.py` and `workspace/agents.py`.

## Global Constraints

- `requires-python = ">=3.10"` — no 3.11+-only syntax.
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`/`pytest`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- CI order (match locally before claiming done): `ruff` → `pyright` → `pytest`.
- Tool-name sets are fixed: `READ_TOOLS = {read_file, glob, tree, grep} | LSP_TOOLS`, `NET_TOOLS = {web_search, fetch_url}`. A read-only worker's reach is `READ_TOOLS | NET_TOOLS`.
- Tool/skill docstrings and skill/agent bodies are model-facing product copy — write them deliberately.
- Discovery dedups first-root-wins; nothing in discovery may raise into a turn (malformed assets are skipped).

---

### Task 1: Built-in discovery root

Adds a package-relative `builtin` root to both `skill_roots()` and `agent_roots()`, after `global`, so bundled assets are discoverable and user/global assets can shadow them. The `builtin/` dirs are created (empty) here; assets land in Tasks 2–3.

**Files:**
- Modify: `src/marim_harness/config/env.py` (add `builtin_root()`)
- Modify: `src/marim_harness/config/__init__.py` (export `builtin_root`)
- Modify: `src/marim_harness/workspace/skills.py:57-63` (`skill_roots`)
- Modify: `src/marim_harness/workspace/agents.py:91-97` (`agent_roots`)
- Create: `src/marim_harness/builtin/skills/.gitkeep`
- Create: `src/marim_harness/builtin/agents/.gitkeep`
- Test: `tests/test_skills.py:59` (update), `tests/test_agents.py:58` (update), plus new cases in each.

**Interfaces:**
- Produces: `marim_harness.config.builtin_root() -> pathlib.Path` returning `<pkg>/builtin` (i.e. `src/marim_harness/builtin`). `skill_roots(ws)` and `agent_roots(ws)` each return a 3-tuple list ending in `("builtin", builtin_root() / "skills"|"agents")`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_skills.py`, update the existing precedence test and add a builtin-root test:

```python
def test_skill_roots_order_and_precedence(tmp_path):
    from marim_harness.config import builtin_root, config_dir

    ws = tmp_path / "ws"
    roots = skill_roots(ws)
    sources = [s for s, _ in roots]
    # Project before global before the bundled built-in root.
    assert sources == ["project", "global", "builtin"]
    assert roots[0][1] == ws / ".marim" / "skills"
    assert roots[1][1] == config_dir() / "skills"
    assert roots[2][1] == builtin_root() / "skills"


def test_builtin_root_is_inside_package():
    from marim_harness.config import builtin_root

    root = builtin_root()
    assert root.name == "builtin"
    assert root.parent.name == "marim_harness"
```

In `tests/test_agents.py`, update its precedence test the same way:

```python
def test_agent_roots_order_and_precedence(tmp_path):
    from marim_harness.config import builtin_root, config_dir

    ws = tmp_path / "ws"
    roots = agent_roots(ws)
    sources = [s for s, _ in roots]
    assert sources == ["project", "global", "builtin"]
    assert roots[2][1] == builtin_root() / "agents"
```

(If `tests/test_agents.py:58` lives in a differently-named test, edit that assertion in place to `["project", "global", "builtin"]` rather than adding a duplicate.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_skills.py::test_skill_roots_order_and_precedence tests/test_skills.py::test_builtin_root_is_inside_package -v`
Expected: FAIL — `ImportError: cannot import name 'builtin_root'` (and the sources assertion mismatch).

- [ ] **Step 3: Add the `builtin_root` helper**

In `src/marim_harness/config/env.py`, after `config_dir()` (it already imports `from pathlib import Path`):

```python
def builtin_root() -> Path:
    """The package's bundled skills/agents directory
    (``src/marim_harness/builtin``), shipped inside the wheel. Skills and agents
    discovered here are marim's own defaults; project/global roots shadow them."""
    return Path(__file__).resolve().parent.parent / "builtin"
```

In `src/marim_harness/config/__init__.py`, add `builtin_root` to the `from .env import ...` line and to `__all__`:

```python
from .env import builtin_root, config_dir, global_config_path, load_environment
```
```python
    "builtin_root",
    "config_dir",
```

- [ ] **Step 4: Wire the root into discovery**

In `src/marim_harness/workspace/skills.py`, update the import and `skill_roots()`:

```python
from ..config import builtin_root, config_dir
```
```python
def skill_roots(workspace_root) -> list[tuple[str, Path]]:
    """The discovery roots, highest precedence first: project, then global, then
    marim's bundled built-in skills."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "skills"),
        ("global", config_dir() / "skills"),
        ("builtin", builtin_root() / "skills"),
    ]
```

In `src/marim_harness/workspace/agents.py`, update the import and `agent_roots()`:

```python
from ..config import builtin_root, config_dir
```
```python
def agent_roots(workspace_root) -> list[tuple[str, Path]]:
    """The discovery roots, highest precedence first: project, then global, then
    marim's bundled built-in agents."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "agents"),
        ("global", config_dir() / "agents"),
        ("builtin", builtin_root() / "agents"),
    ]
```

Create the (currently empty) bundled dirs so the root exists on disk:

```bash
mkdir -p src/marim_harness/builtin/skills src/marim_harness/builtin/agents
touch src/marim_harness/builtin/skills/.gitkeep src/marim_harness/builtin/agents/.gitkeep
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_skills.py tests/test_agents.py -v`
Expected: PASS (including the updated precedence assertions).

- [ ] **Step 6: Lint, type-check, full suite**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all PASS. (`builtin_root()` introduces no new typing or lint issues.)

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/config/env.py src/marim_harness/config/__init__.py \
        src/marim_harness/workspace/skills.py src/marim_harness/workspace/agents.py \
        src/marim_harness/builtin tests/test_skills.py tests/test_agents.py
git commit -m "feat: add bundled built-in discovery root for skills and agents"
```

---

### Task 2: `researcher` sub-agent

Ships the read-only web worker each fan-out spawn uses. It mirrors `explore`'s reach (`READ_TOOLS | NET_TOOLS`), so it can't mutate the workspace or recurse.

**Files:**
- Create: `src/marim_harness/builtin/agents/researcher.md`
- Test: `tests/test_agents.py` (new cases)

**Interfaces:**
- Consumes: `builtin` root from Task 1; `discover_agents`, `find_agent`, `effective_tools`, `AgentDef`, `READ_TOOLS`, `NET_TOOLS` (already exported from `marim_harness.workspace` / `marim_harness.tools.provider`).
- Produces: an agent named `researcher`, `source == "builtin"`, `backend == "native"`, tools `== READ_TOOLS | NET_TOOLS`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agents.py` (uses the existing `isolated_home` fixture so project/global roots are empty tmp dirs):

```python
def test_researcher_is_builtin(isolated_home):
    ws = isolated_home / "ws"
    agent = find_agent(ws, "researcher")
    assert agent is not None
    assert agent.source == "builtin"
    assert agent.backend == "native"
    # Read-only + network only: no gated/mutating tools, cannot recurse.
    assert agent.tools == (READ_TOOLS | NET_TOOLS)
    assert "spawn_agent" not in agent.tools
    assert GATED_TOOLS.isdisjoint(agent.tools)


def test_project_agent_shadows_builtin_researcher(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(ws / ".marim" / "agents", "researcher", description="Custom override.")
    agent = find_agent(ws, "researcher")
    assert agent.source == "project"
    assert agent.description == "Custom override."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agents.py::test_researcher_is_builtin tests/test_agents.py::test_project_agent_shadows_builtin_researcher -v`
Expected: FAIL — `find_agent` returns `None` (no `researcher.md` yet).

- [ ] **Step 3: Create the agent file**

`src/marim_harness/builtin/agents/researcher.md`:

```markdown
---
description: Web research worker — investigates one sub-question and returns sourced findings. Read-only.
tools: web_search, fetch_url, read_file, glob, grep, tree
---
You are a research sub-agent. You are given ONE focused sub-question. Investigate it
using web_search and fetch_url (and local files when relevant), then report sourced
findings as your final message. You cannot modify anything and cannot spawn other
agents.

Source discipline:
- Prefer primary, high-quality sources: systematic reviews and meta-analyses >
  randomized controlled trials > observational studies > everything else.
- Down-weight and explicitly flag marketing pages, vendor sites, press releases, and
  SEO content. If a claim traces only to those, say so.
- Prefer recent work, but keep landmark older sources that still anchor the field.
- Open the actual source before citing it — never cite from a search snippet alone.

Report format — a list of findings, each as:
- CLAIM: one sentence.
  - source: <URL>
  - type: meta-analysis | RCT | observational | other
  - quality: high | medium | low

Lead with the 2–3 most important findings. End with: open questions, contradictions
you found between sources, and anything you could not verify.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agents.py::test_researcher_is_builtin tests/test_agents.py::test_project_agent_shadows_builtin_researcher -v`
Expected: PASS. (`tools:` lists only names in `SUBAGENT_TOOLS`, so `_parse_tools` keeps them all; `web_search`/`fetch_url` ∈ `NET_TOOLS`, `read_file`/`glob`/`grep`/`tree` ∈ `READ_TOOLS`, giving exactly `READ_TOOLS | NET_TOOLS`.)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/builtin/agents/researcher.md tests/test_agents.py
git commit -m "feat: add built-in researcher sub-agent for deep research"
```

---

### Task 3: `deep-research` skill

Ships the orchestration policy the main agent invokes: plan → fan out to `researcher` workers → adversarial verify with `explore` skeptics → synthesize a cited report.

**Files:**
- Create: `src/marim_harness/builtin/skills/deep-research/SKILL.md`
- Test: `tests/test_skills.py` (new cases)

**Interfaces:**
- Consumes: `builtin` root from Task 1; `researcher` agent from Task 2; `discover_skills`, `find_skill`, `read_skill_body`, `skills_index_text`, `skill_roots`.
- Produces: a skill named `deep-research`, `source == "builtin"`, present in `skills_index_text`, body invokable via `read_skill_body`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_skills.py` (uses the existing `isolated_home` fixture):

```python
def test_deep_research_is_builtin(isolated_home):
    ws = isolated_home / "ws"
    skill = find_skill(ws, "deep-research")
    assert skill is not None
    assert skill.source == "builtin"
    # Appears in the injected index so the model can invoke it.
    index = skills_index_text(discover_skills(ws))
    assert "deep-research" in index
    # Body names the worker type so the main agent fans out, not researches inline.
    body = read_skill_body(skill)
    assert "researcher" in body
    assert "spawn_agent" in body


def test_project_skill_shadows_builtin_deep_research(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".marim" / "skills", "deep-research", description="Custom override.")
    skill = find_skill(ws, "deep-research")
    assert skill.source == "project"
    assert skill.description == "Custom override."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_skills.py::test_deep_research_is_builtin tests/test_skills.py::test_project_skill_shadows_builtin_deep_research -v`
Expected: FAIL — `find_skill` returns `None` (no skill dir yet).

- [ ] **Step 3: Create the skill file**

`src/marim_harness/builtin/skills/deep-research/SKILL.md`:

```markdown
---
name: deep-research
description: Produce a multi-source, fact-checked, cited research report. Use when the user wants deep research on a topic — fans out parallel researchers, adversarially verifies claims, then synthesizes.
---
# Deep research

Produce a thorough, cited research report by DELEGATING — do NOT do the research
yourself in this turn. Your job is to orchestrate sub-agents and synthesize their
reports.

## 1. Plan
Restate the question, then decompose it into 3–6 INDEPENDENT sub-questions that can be
researched in parallel. If the question is too vague to research well (missing scope,
constraints, region, or timeframe), ask the user 1–3 clarifying questions FIRST, then
continue.

## 2. Fan out (parallel)
In a SINGLE turn, call `spawn_agent` once per sub-question:
- `type`: `researcher`
- `task`: the sub-question, stated precisely
- `context`: the overall research question and why this sub-question matters
- `returns`: "A list of findings; each = CLAIM + source URL + type
  (meta-analysis/RCT/observational/other) + quality (high/medium/low)."

Spawn them together so they run concurrently. Do NOT research inline.

## 3. Verify (adversarial)
Collect the workers' findings. For each load-bearing claim — the ones your conclusion
depends on — call `spawn_agent` `type=explore` with a task to REFUTE it: find
counter-evidence and confirm the cited source actually supports the claim. Drop or
downgrade any claim that does not survive.

## 4. Synthesize
Write ONE report:
- Every nontrivial claim keeps its citation.
- Where good sources genuinely DISAGREE, say so and explain why (effect size, trial
  quality, population) — do not flatten into a single verdict.
- End with: (a) 5 bullets "established vs. hyped", and (b) a per-sub-question
  confidence rating (high/medium/low) with the main limiting factor.

## Example
Topic: "Evidence on creatine for cognition (not muscle)." Sub-questions → researchers:
healthy adults; special populations (sleep-deprived, vegetarians, aging, mood);
dosing/kinetics for a brain effect; safety & study quality. Then refute the
load-bearing claims, then synthesize.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_skills.py::test_deep_research_is_builtin tests/test_skills.py::test_project_skill_shadows_builtin_deep_research -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/builtin/skills/deep-research/SKILL.md tests/test_skills.py
git commit -m "feat: add built-in deep-research skill"
```

---

### Task 4: Package the built-in assets into the wheel

Confirms the `.md` assets ship inside the built wheel (they're non-`.py` files under the package). If hatchling omits them, add an explicit force-include.

**Files:**
- Possibly modify: `pyproject.toml` (`[tool.hatch.build.targets.wheel]`)

**Interfaces:**
- Consumes: assets created in Tasks 2–3.
- Produces: a wheel containing `marim_harness/builtin/skills/deep-research/SKILL.md` and `marim_harness/builtin/agents/researcher.md`.

- [ ] **Step 1: Build the wheel**

Run: `uv build --wheel`
Expected: a wheel under `dist/`.

- [ ] **Step 2: Inspect the wheel for the assets**

Run: `python -c "import zipfile,glob; w=sorted(glob.glob('dist/*.whl'))[-1]; print('\n'.join(n for n in zipfile.ZipFile(w).namelist() if 'builtin' in n))"`
Expected: lists both `marim_harness/builtin/skills/deep-research/SKILL.md` and `marim_harness/builtin/agents/researcher.md`.

- [ ] **Step 3: If (and only if) the assets are missing, add a force-include**

Append to `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/marim_harness/builtin" = "marim_harness/builtin"
```

Then rebuild and re-inspect (Steps 1–2) until both files appear. If they were already present in Step 2, make NO change to `pyproject.toml`.

- [ ] **Step 4: Clean up build artifacts**

Run: `rm -rf dist build` (don't commit the wheel).

- [ ] **Step 5: Final full CI pass**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all PASS.

- [ ] **Step 6: Commit (only if pyproject changed)**

```bash
git add pyproject.toml
git commit -m "build: ship built-in deep-research assets in the wheel"
```

---

## Manual verification (post-merge, not a code task)

Live smoke test with a real model (costs tokens + network), to confirm the skill actually triggers fan-out:

```bash
MARIM_MODEL=<a capable model> uv run marim -p \
  "Deep research: current evidence on creatine for cognition (not muscle)."
```

Expect: the agent invokes the `deep-research` skill, spawns multiple `researcher`
sub-agents (visible in the run), runs a verify pass, and returns a cited report.

## Notes for the implementer

- The `builtin` root pointing at a not-yet-populated dir is safe at every step:
  discovery catches `OSError` on `iterdir` and skips. Tasks are independently testable.
- `_parse_tools` silently drops unknown tool names and falls back to `READ_TOOLS` if
  none survive — so keep the `researcher` `tools:` line to names in `SUBAGENT_TOOLS`
  (`read_file, glob, tree, grep, web_search, fetch_url` and the gated/LSP names).
- Sub-agents are never granted `spawn_agent`, so `researcher` and `explore` cannot
  recurse — the fan-out lives only in the main-agent skill body. Don't try to make a
  sub-agent orchestrate.
- Don't add config flags or touch the turn loop; the feature is two assets + one root.
```