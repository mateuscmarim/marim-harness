# Decouple Skill/Agent Discovery from `~/.claude` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make marim's skill and sub-agent discovery read only marim's own roots (`.marim/` project, `~/.config/marim/` global), dropping the two `~/.claude` interop roots in each subsystem.

**Architecture:** Two leaf root-listing functions (`skill_roots` in `workspace/skills.py`, `agent_roots` in `workspace/agents.py`) each drop from four root tuples to two. Everything downstream already iterates whatever roots these return, so only prose (docstrings, one help string) and tests that assert the old four-root shape need touching. No new behavior, no new error paths.

**Tech Stack:** Python 3.10+, pytest, `uv` for env/deps, ruff, pyright.

## Global Constraints

- Discovery roots after this change, in precedence order, are **exactly**:
  `("project", <ws>/.marim/<kind>)` then `("global", config_dir()/<kind>)` —
  where `<kind>` is `skills` or `agents`. No `.claude` path segment anywhere.
- `config_dir()` is imported from `..config` (already imported in both files).
- Full removal — no opt-out flag, no backward-compat shim.
- Discovery must never raise into a turn (existing `try/except OSError` around
  `iterdir()` is unchanged).
- Gates that must pass before any commit: `uv run ruff check src tests`,
  `uv run pyright src`, `uv run pytest`.
- Commit messages end with the two trailers used in this repo:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x
  ```
- Stage only the named files per task (`git add <paths>`). Never `git add -A`
  (`.marim/` runtime artifacts must stay untracked).

---

### Task 1: Decouple skill discovery from `~/.claude`

**Files:**
- Modify: `src/marim_harness/workspace/skills.py` (module docstring lines 5–6; `Skill.source` docstring line ~37; `skill_roots()` lines 48–57)
- Modify: `src/marim_harness/interfaces/tui/commands.py` (the `/skill` empty-list help string, ~lines 182–185)
- Test: `tests/test_skills.py`

**Interfaces:**
- Consumes: `config_dir()` from `marim_harness.config` (already imported in `skills.py`).
- Produces: `skill_roots(workspace_root) -> list[tuple[str, Path]]` returning exactly `[("project", ws/".marim"/"skills"), ("global", config_dir()/"skills")]`. `discover_skills`, dedup, and `Skill.source` values are unchanged in mechanism — `source` can now only ever be `"project"` or `"global"`.

- [ ] **Step 1: Update the failing tests first**

Open `tests/test_skills.py`. Make these four edits.

(a) Replace the roots-order test (currently lines 52–59):

```python
def test_skill_roots_order_and_precedence(tmp_path):
    from marim_harness.config import config_dir

    ws = tmp_path / "ws"
    roots = skill_roots(ws)
    sources = [s for s, _ in roots]
    # Only marim's own roots: project before global. No .claude interop roots.
    assert sources == ["project", "global"]
    assert roots[0][1] == ws / ".marim" / "skills"
    assert roots[1][1] == config_dir() / "skills"
    assert not any(".claude" in str(p) for _, p in roots)
```

(b) Replace `test_discover_claude_skills` (currently lines 121–126) with a test that the `.claude` dir is now IGNORED:

```python
def test_ignores_claude_skills_dir(isolated_home):
    ws = isolated_home / "ws"
    _make_skill(ws / ".claude" / "skills", "from-claude")
    assert discover_skills(ws) == []
```

(c) Delete `test_precedence_marim_over_claude` (currently lines 140–147) entirely — the `.claude` root no longer exists, so marim-over-claude precedence is not a thing. `test_precedence_project_over_global` (lines 129–137) stays as-is and is the surviving precedence test.

(d) Update the `isolated_home` fixture docstring (lines 44–46): change
`"""Point the global roots (config dir + ~/.claude) at tmp so tests don't see` to
`"""Point the global root (config dir) at tmp so tests don't see`. Leave the
fixture body unchanged (it still sets `HOME`, which the
`test_ignores_claude_skills_dir` test relies on indirectly via the project
`.claude` path, and which keeps the real `~/.claude` out of view).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_skills.py -q`
Expected: FAILs — `test_skill_roots_order_and_precedence` fails on
`sources == ["project", "global"]` (still four roots), and
`test_ignores_claude_skills_dir` fails because the `.claude` skill is still
discovered (returns one skill, not `[]`).

- [ ] **Step 3: Update `skill_roots()` in `skills.py`**

Replace lines 48–57:

```python
def skill_roots(workspace_root) -> list[tuple[str, Path]]:
    """The two discovery roots, highest precedence first: project over global."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "skills"),
        ("global", config_dir() / "skills"),
    ]
```

- [ ] **Step 4: Fix the `skills.py` docstrings**

Module docstring (lines 5–6): replace

```
``references/``, and ``assets/``. marim discovers skills from four roots in
precedence order — project before global, marim before claude within a scope —
```

with

```
``references/``, and ``assets/``. marim discovers skills from two roots in
precedence order — project before global —
```

`Skill.source` docstring (line ~37): replace

```
    discovery root it came from (e.g. ``project`` or ``global/.claude``)."""
```

with

```
    discovery root it came from (e.g. ``project`` or ``global``)."""
```

- [ ] **Step 5: Fix the `/skill` help string in `commands.py`**

In `src/marim_harness/interfaces/tui/commands.py`, the empty-skills message reads:

```python
                "No skills found. Drop a skill directory under `.marim/skills/` "
                "(or `.claude/skills/`) with a `SKILL.md` inside."
```

Replace with:

```python
                "No skills found. Drop a skill directory under `.marim/skills/` "
                "or `~/.config/marim/skills/` with a `SKILL.md` inside."
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_skills.py -q`
Expected: PASS (all skills tests green).

- [ ] **Step 7: Run the gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: ruff clean, pyright clean, full suite passes.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/workspace/skills.py \
        src/marim_harness/interfaces/tui/commands.py \
        tests/test_skills.py
git commit -m "$(cat <<'EOF'
refactor: drop ~/.claude skill discovery roots

skill_roots() now returns only marim's own roots (project .marim/skills,
global ~/.config/marim/skills). Skills under .claude/skills are no longer
discovered. Updates docstrings and the /skill help text accordingly.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x
EOF
)"
```

---

### Task 2: Decouple sub-agent discovery from `~/.claude`

**Files:**
- Modify: `src/marim_harness/workspace/agents.py` (module docstring lines 9–10; `agent_roots()` lines 79–88 — the function at the `marim over claude` docstring)
- Test: `tests/test_agents.py`

**Interfaces:**
- Consumes: `config_dir()` from `marim_harness.config` (already imported in `agents.py`).
- Produces: `agent_roots(workspace_root) -> list[tuple[str, Path]]` returning exactly `[("project", ws/".marim"/"agents"), ("global", config_dir()/"agents")]`. `discover_agents`, dedup, and `AgentDef.source` are unchanged in mechanism — `source` can now only be `"built-in"`, `"project"`, or `"global"`.

- [ ] **Step 1: Update the failing test first**

In `tests/test_agents.py`, replace `test_agent_roots_order_and_precedence` (currently lines 51–57):

```python
def test_agent_roots_order_and_precedence(tmp_path):
    from marim_harness.config import config_dir

    ws = tmp_path / "ws"
    roots = agent_roots(ws)
    sources = [s for s, _ in roots]
    # Only marim's own roots: project before global. No .claude interop roots.
    assert sources == ["project", "global"]
    assert roots[0][1] == ws / ".marim" / "agents"
    assert roots[1][1] == config_dir() / "agents"
    assert not any(".claude" in str(p) for _, p in roots)
```

Then add, immediately after that test, a behavior test that the `.claude`
agents dir is ignored (mirrors the skills one; uses the same `isolated_home`
fixture and `_make_agent` helper the file already defines):

```python
def test_ignores_claude_agents_dir(isolated_home):
    ws = isolated_home / "ws"
    _make_agent(ws / ".claude" / "agents", "from-claude")
    names = {a.name for a in discover_agents(ws)}
    assert "from-claude" not in names
```

Before writing Step 1, confirm the helper name and signature: open
`tests/test_agents.py` and check how custom agents are created in the existing
tests (e.g. `test_precedence_project_over_global`). If the helper is not named
`_make_agent` or takes different arguments, use whatever that file already uses
to drop an agent `.md` into a directory, keeping the same call shape. Do not
invent a new helper.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agents.py -q`
Expected: FAILs — `test_agent_roots_order_and_precedence` fails on
`sources == ["project", "global"]` (still four roots); the new
`test_ignores_claude_agents_dir` may pass already (because `~/.claude/agents`
is unrelated to a project `.claude/agents` — verify) but the roots test
failing is sufficient to confirm red.

- [ ] **Step 3: Update `agent_roots()` in `agents.py`**

Replace the function body (lines ~79–88):

```python
def agent_roots(workspace_root) -> list[tuple[str, Path]]:
    """The two discovery roots, highest precedence first: project over global."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "agents"),
        ("global", config_dir() / "agents"),
    ]
```

- [ ] **Step 4: Fix the `agents.py` module docstring**

Lines 9–10 read:

```
agents live in ``.marim/agents/<name>.md`` (and the parallel claude/global
roots); their file body is the role's system prompt and an optional ``tools:``
```

Replace with:

```
agents live in ``.marim/agents/<name>.md`` (and the parallel global
root); their file body is the role's system prompt and an optional ``tools:``
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agents.py -q`
Expected: PASS (all agents tests green).

- [ ] **Step 6: Run the gates**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: ruff clean, pyright clean, full suite passes.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/workspace/agents.py tests/test_agents.py
git commit -m "$(cat <<'EOF'
refactor: drop ~/.claude sub-agent discovery roots

agent_roots() now returns only marim's own roots (project .marim/agents,
global ~/.config/marim/agents). Agents under .claude/agents are no longer
discovered. Updates the module docstring accordingly.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x
EOF
)"
```

---

## Self-Review

**1. Spec coverage:**
- skills.py `skill_roots()` two-root form → Task 1 Step 3. ✅
- skills.py module + `Skill.source` docstrings → Task 1 Step 4. ✅
- commands.py `/skill` help string → Task 1 Step 5. ✅
- agents.py `agent_roots()` two-root form → Task 2 Step 3. ✅
- agents.py module docstring → Task 2 Step 4. ✅
- test_skills.py: roots assertion, ignore-claude test, drop marim-over-claude, fixture docstring → Task 1 Step 1. ✅
- test_agents.py: roots assertion, optional ignore-claude test → Task 2 Step 1. ✅
- Spec "Out of scope" (no opt-out flag, no migration, no hooks/MCP changes, no unifying refactor) → reflected in Global Constraints + nothing in either task touches those. ✅

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to" — every code step has the literal before/after text. The only conditional is Task 2 Step 1's helper-name verification, which gives an explicit rule ("use whatever the file already uses; do not invent a helper"), not a placeholder.

**3. Type consistency:** `skill_roots`/`agent_roots` both return `list[tuple[str, Path]]` with sources `["project", "global"]`, consistent across tasks and tests. `config_dir()` import path (`marim_harness.config`) is consistent. `discover_skills`/`discover_agents` names match the existing module APIs.
