# CLAUDE.md Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `AGENTS.md` is absent from the workspace root, fall back to reading `CLAUDE.md` — the standard Claude Code instructions file many projects already maintain.

**Architecture:** Modify `load_project_instructions` to iterate a fallback file list (`AGENTS.md`, then `CLAUDE.md`) instead of trying a single hardcoded name. The existing `filename` parameter still works as an override for callers that want a specific file. Tests cover the new fallback paths.

**Tech Stack:** Python, pytest, pydantic-ai (existing test infrastructure with `FunctionModel`)

## Global Constraints

- Zero new dependencies
- `load_project_instructions` signature remains backward-compatible (`filename` defaults to `None` instead of `_PROJECT_INSTRUCTIONS_FILE`, but callers passing no args get the same behavior)
- Global instructions path (`~/.config/marim/AGENTS.md`) is unchanged
- Plugin instructions are unchanged
- Dynamic reload behavior (re-reads every turn) is unchanged

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/marim_harness/instructions.py` | Add fallback tuple, modify `load_project_instructions`, update closure prefix |
| `tests/test_instructions.py` | Unit tests for fallback behavior |
| `tests/test_agent_instructions.py` | Integration test: CLAUDE.md injected into agent prompt |

---

### Task 1: Unit tests for CLAUDE.md fallback

**Files:**
- Modify: `tests/test_instructions.py`

**Interfaces:**
- Consumes: `load_project_instructions(workspace_root)` (existing function, signature unchanged)
- Produces: Four new test functions validating fallback behavior

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_instructions.py` after the existing `test_unreadable_file_returns_none`:

```python
def test_claude_md_fallback(tmp_path: Path):
    """CLAUDE.md is used when AGENTS.md is absent."""
    (tmp_path / "CLAUDE.md").write_text("Claude rules.\n")
    assert load_project_instructions(tmp_path) == "Claude rules."


def test_agents_md_takes_priority_over_claude_md(tmp_path: Path):
    """AGENTS.md wins when both files exist."""
    (tmp_path / "AGENTS.md").write_text("Agents rules.\n")
    (tmp_path / "CLAUDE.md").write_text("Claude rules.\n")
    assert load_project_instructions(tmp_path) == "Agents rules."


def test_explicit_filename_ignores_fallback(tmp_path: Path):
    """Passing filename= bypasses the fallback list entirely."""
    (tmp_path / "AGENTS.md").write_text("ignored\n")
    (tmp_path / "CLAUDE.md").write_text("also ignored\n")
    (tmp_path / ".marim.md").write_text("explicit rules")
    assert load_project_instructions(tmp_path, filename=".marim.md") == "explicit rules"


def test_empty_claude_md_returns_none(tmp_path: Path):
    """An empty CLAUDE.md is treated the same as a missing file."""
    (tmp_path / "CLAUDE.md").write_text("   \n\t\n")
    assert load_project_instructions(tmp_path) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_instructions.py -v`
Expected: The four new tests FAIL (function not yet modified to try CLAUDE.md)

- [ ] **Step 3: Run existing tests to verify they still pass**

Run: `uv run pytest tests/test_instructions.py -v`
Expected: The original four tests still PASS (no behavior change for them yet)

- [ ] **Step 4: Commit**

```bash
git add tests/test_instructions.py
git commit -m "test: add failing tests for CLAUDE.md fallback"
```

---

### Task 2: Implement CLAUDE.md fallback in `load_project_instructions`

**Files:**
- Modify: `src/marim_harness/instructions.py:24-55`

**Interfaces:**
- Consumes: (nothing new)
- Produces: `load_project_instructions(workspace_root, filename=None)` now tries `_PROJECT_FALLBACK_FILES` when `filename` is `None`

- [ ] **Step 1: Add the fallback tuple**

In `src/marim_harness/instructions.py`, add after line 24 (`_PROJECT_INSTRUCTIONS_FILE = "AGENTS.md"`):

```python
_PROJECT_FALLBACK_FILES = ("AGENTS.md", "CLAUDE.md")
```

- [ ] **Step 2: Modify `load_project_instructions`**

Replace the entire function (lines 44-55) with:

```python
def load_project_instructions(
    workspace_root, filename: str | None = None
) -> Optional[str]:
    """Read project-specific agent instructions from the workspace root.

    When *filename* is given, try only that file.  Otherwise iterate the
    fallback list (``AGENTS.md``, ``CLAUDE.md``) and return the first
    non-empty result.  Returns ``None`` if no file is found or all are
    empty/unreadable — a broken file must never break a turn.
    """
    if filename is not None:
        candidates = [filename]
    else:
        candidates = _PROJECT_FALLBACK_FILES

    for name in candidates:
        path = Path(workspace_root) / name
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return text
    return None
```

- [ ] **Step 3: Run all unit tests**

Run: `uv run pytest tests/test_instructions.py -v`
Expected: All 8 tests PASS (4 existing + 4 new)

- [ ] **Step 4: Commit**

```bash
git add src/marim_harness/instructions.py
git commit -m "feat: fall back to CLAUDE.md when AGENTS.md is absent"
```

---

### Task 3: Update `_project_instructions` closure prefix

**Files:**
- Modify: `src/marim_harness/instructions.py:86-91`

**Interfaces:**
- Consumes: `load_project_instructions` (already modified in Task 2)
- Produces: Updated instruction prefix that doesn't assume the source file name

- [ ] **Step 1: Update the closure**

In `src/marim_harness/instructions.py`, change line 91 from:

```python
        return f"Project-specific instructions from AGENTS.md:\n\n{text}"
```

to:

```python
        return f"Project-specific instructions:\n\n{text}"
```

- [ ] **Step 2: Run existing integration tests**

Run: `uv run pytest tests/test_agent_instructions.py -v`
Expected: All existing tests PASS — the prefix change doesn't break assertions (they check for instruction content, not the prefix)

- [ ] **Step 3: Run unit tests**

Run: `uv run pytest tests/test_instructions.py -v`
Expected: All 8 tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/marim_harness/instructions.py
git commit -m "fix: remove filename assumption from project instructions prefix"
```

---

### Task 4: Integration test for CLAUDE.md injection

**Files:**
- Modify: `tests/test_agent_instructions.py`

**Interfaces:**
- Consumes: `Harness`, `Deps`, `FunctionModel`, `BuiltinToolProvider` (existing test infrastructure)
- Produces: One new integration test validating CLAUDE.md flows through to the agent

- [ ] **Step 1: Write the integration test**

Add this test to `tests/test_agent_instructions.py` after `test_project_instructions_injected_and_dynamic`:

```python
@pytest.mark.anyio
async def test_claude_md_fallback_injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLAUDE.md content reaches the agent when AGENTS.md is absent."""
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="BASE PROMPT",
    )

    # No AGENTS.md, no CLAUDE.md -> only base prompt.
    await harness.run_turn("hi")
    assert "BASE PROMPT" in captured["instructions"]
    assert "CLAUDE.md" not in captured["instructions"]

    # CLAUDE.md present, still no AGENTS.md -> CLAUDE.md content appears.
    (tmp_path / "CLAUDE.md").write_text("Always use type hints.")
    await harness.run_turn("hi again")
    assert "Always use type hints." in captured["instructions"]
    assert "BASE PROMPT" in captured["instructions"]
    instr = captured["instructions"]
    assert instr.index("BASE PROMPT") < instr.index("Always use type hints.")

    # Adding AGENTS.md takes priority over CLAUDE.md.
    (tmp_path / "AGENTS.md").write_text("Use tabs, not spaces.")
    await harness.run_turn("third")
    instr = captured["instructions"]
    assert "Use tabs, not spaces." in instr
    assert "Always use type hints." not in instr
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_agent_instructions.py::test_claude_md_fallback_injected -v`
Expected: PASS

- [ ] **Step 3: Run all tests**

Run: `uv run pytest tests/test_agent_instructions.py tests/test_instructions.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_agent_instructions.py
git commit -m "test: add integration test for CLAUDE.md fallback"
```

---

### Task 5: Final verification

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests PASS

- [ ] **Step 2: Check for regressions in related tests**

Run: `uv run pytest tests/test_agent_instructions.py tests/test_plugin_instructions.py tests/test_plugin_discovery.py tests/test_app.py -v`
Expected: All PASS — no regressions

- [ ] **Step 3: Commit (if any fixups needed)**

```bash
git add -A
git commit -m "fix: address review feedback"
```
