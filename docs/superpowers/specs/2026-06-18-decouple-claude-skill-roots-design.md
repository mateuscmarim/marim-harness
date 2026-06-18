# Decouple marim Skill/Agent Discovery from `~/.claude` — Design

**Date:** 2026-06-18
**Status:** Approved (pending spec review)

## Goal

Make marim's skill and sub-agent discovery self-contained: read only marim's
own roots — `.marim/` (project) and `~/.config/marim/` (global) — and stop
reading the two `~/.claude` interop roots in each subsystem. Full removal; no
opt-out flag, no backward-compat shim.

## Background

marim discovers both skills and sub-agents from four precedence-ordered roots,
defined identically in two places:

- `workspace/skills.py` → `skill_roots()`:
  `project` (`<ws>/.marim/skills`), `project/.claude` (`<ws>/.claude/skills`),
  `global` (`config_dir()/skills`), `global/.claude` (`~/.claude/skills`).
- `workspace/agents.py` → `agent_roots()`: the same four, under `agents/`.

The two `.claude` roots exist for interop — so a Claude Code user's hand-placed
`~/.claude/skills` / `~/.claude/agents` "just work" in marim without migration.
This couples marim to another tool's config directory. As marim becomes a
standalone product, discovery should depend only on marim's own directories.

Removing the `global/.claude` skills root drops the four skills currently coming
from `~/.claude/skills` (`deploy-nasa`, `prd`, `ralph`, `toast-zeroing-trend`)
from marim's view. **Decision: leave them in place** — they remain available to
Claude Code; the user re-adds any wanted ones under `~/.config/marim/skills`.
`~/.claude/agents` is empty, so nothing is orphaned there.

## Architecture

Two leaf functions change shape; everything downstream (`discover_skills`,
`discover_agents`, dedup, parsing, the `/skill` command, the prompt index)
already iterates whatever roots the function returns, so no caller logic
changes. The only ripples are prose (docstrings, one help string) and tests
that assert the old four-root shape.

## Changes

### 1. `workspace/skills.py`

`skill_roots()` returns exactly two roots:

```python
def skill_roots(workspace_root) -> list[tuple[str, Path]]:
    """The two discovery roots, highest precedence first: project over global."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "skills"),
        ("global", config_dir() / "skills"),
    ]
```

Docstring fixes in the same file:
- Module docstring (line ~5–6): "marim discovers skills from four roots in
  precedence order — project before global, marim before claude within a
  scope —" → "from two roots in precedence order — project before global —".
- `Skill.source` docstring (line ~37): the example `` `global/.claude` `` →
  `` `global` `` (keep `` `project` `` as the other example).

### 2. `workspace/agents.py`

`agent_roots()` returns exactly two roots (same pattern, under `agents/`):

```python
def agent_roots(workspace_root) -> list[tuple[str, Path]]:
    """The two discovery roots, highest precedence first: project over global."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "agents"),
        ("global", config_dir() / "agents"),
    ]
```

Docstring fixes:
- Module docstring (line ~9–10): "Custom agents live in
  `.marim/agents/<name>.md` (and the parallel claude/global roots);" →
  "(and the parallel global root);".
- `agent_roots` docstring: drop "marim over claude within each scope".

### 3. `interfaces/tui/commands.py`

The `/skill` empty-list help string (line ~183–184) currently reads:

```
"No skills found. Drop a skill directory under `.marim/skills/` "
"(or `.claude/skills/`) with a `SKILL.md` inside."
```

Remove the `.claude` parenthetical:

```
"No skills found. Drop a skill directory under `.marim/skills/` "
"or `~/.config/marim/skills/` with a `SKILL.md` inside."
```

### 4. `tests/test_skills.py`

- `test_skill_roots_order_and_precedence`: assert
  `sources == ["project", "global"]`, `roots[0][1] == ws/".marim"/"skills"`,
  `roots[1][1] == config_dir()/"skills"` (use the same `config_dir` import the
  module uses, resolved under the test's `XDG_CONFIG_HOME`), and assert no root
  path contains a `.claude` segment.
- `test_discover_claude_skills` (lines 121–126): **delete** — the `.claude`
  root no longer exists, so a skill under `<ws>/.claude/skills` must NOT be
  discovered. Replace with `test_ignores_claude_skills_dir`: place a skill
  under `ws/".claude"/"skills"` and assert `discover_skills(ws) == []`.
- `test_precedence_marim_over_claude` (lines 140–147): **delete/replace** — no
  longer meaningful. The surviving precedence test is
  `test_precedence_project_over_global` (unchanged; uses `.marim` + global).
- `isolated_home` fixture docstring mentions "(config dir + ~/.claude)"; trim
  to "(config dir)". The fixture still sets `HOME` (harmless) — keep it so the
  `ignores_claude_skills_dir` test can prove `~/.claude` is not read even when
  present.

### 5. `tests/test_agents.py`

- `test_agent_roots_order_and_precedence` (lines 51–57): assert
  `sources == ["project", "global"]` and `roots[1][1] == config_dir()/"agents"`.
- No `.claude` agent discovery/precedence test exists to remove. Optionally add
  `test_ignores_claude_agents_dir` mirroring the skills one for symmetry.

## Data Flow

```
discover_skills(ws) / discover_agents(ws)
   └─ iterate skill_roots(ws) / agent_roots(ws)   ← now 2 roots, not 4
        ├─ ("project", <ws>/.marim/...)
        └─ ("global", ~/.config/marim/...)
   dedup by bare name, first root wins (project over global)
```

`<ws>/.claude/*` and `~/.claude/*` are never read.

## Error Handling

No new paths. `discover_*` already wraps `root.iterdir()` in `try/except OSError`
and skips missing roots, so dropping roots cannot raise. Malformed-skill
skipping is unchanged.

## Testing

- Unit: `skill_roots()` and `agent_roots()` each return exactly the two-root
  list, in order, with no `.claude` path segment.
- Behavior: a skill/agent placed under `<ws>/.claude/...` is NOT discovered.
- Regression: existing `.marim`/global discovery, dedup, and project-over-global
  precedence tests still pass (after removing the `.claude`-specific cases).
- Gates: `uv run ruff check src tests`, `uv run pyright src`, `uv run pytest`
  all green.

## Out of Scope

- The opt-out flag approach (`MARIM_CLAUDE_SKILLS=0`) — explicitly rejected in
  favor of full removal.
- Migrating the four `~/.claude/skills` skills into marim — user chose to leave
  them behind.
- Any change to hooks/MCP config roots (those read `~/.config/marim` and
  `.marim` already; they do not read `~/.claude`).
- The `agents.py` reuse of "skills discovery machinery" is via shared *patterns*,
  not a shared `skill_roots()` call — each file owns its own roots function, so
  both must be edited. No refactor to unify them (YAGNI).
