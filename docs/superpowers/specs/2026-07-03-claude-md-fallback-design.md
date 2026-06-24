# CLAUDE.md Fallback for Project Instructions

**Date:** 2026-07-03  
**Status:** Approved  
**Scope:** Project-level instructions only

## Problem

Many projects maintain a `CLAUDE.md` (the standard Claude Code instructions file) but don't have an `AGENTS.md`. When marim runs in such a project, it silently picks up zero project-specific instructions — the user's carefully written guidance is ignored.

## Solution

When `AGENTS.md` is absent from the workspace root, `load_project_instructions` falls back to trying `CLAUDE.md`. The first non-empty file wins. `AGENTS.md` takes priority when both exist.

## Design

### Fallback list

```python
_PROJECT_FALLBACK_FILES = ("AGENTS.md", "CLAUDE.md")
```

Ordered by priority. `AGENTS.md` is first because it's marim's native format and should always win.

### `load_project_instructions` changes

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

Key behaviors:
- **Explicit `filename`:** tries only that file (backward compatible for tests and plugins)
- **No `filename`:** iterates the fallback list, returns first non-empty result
- **Both files missing:** returns `None` (same as today)
- **AGENTS.md exists:** returns it, never looks at CLAUDE.md
- **AGENTS.md missing, CLAUDE.md exists:** returns CLAUDE.md content

### `_project_instructions` closure update

The instruction prefix changes from:
```
Project-specific instructions from AGENTS.md:
```
to:
```
Project-specific instructions:
```

This avoids misleading the agent when the source is actually `CLAUDE.md`.

### `global_instructions_path` — no change

The global path (`~/.config/marim/AGENTS.md`) is unchanged. This fallback is project-level only.

### Plugin instructions — no change

Each plugin's `AGENTS.md` resolution is independent and unaffected.

## Files to modify

| File | Change |
|------|--------|
| `src/marim_harness/instructions.py` | Add `_PROJECT_FALLBACK_FILES`, modify `load_project_instructions`, update `_project_instructions` prefix |
| `tests/test_instructions.py` | Add fallback tests (CLAUDE.md works, AGENTS.md priority, explicit filename still works) |
| `tests/test_agent_instructions.py` | Add integration test for CLAUDE.md injection |

## Tests

### Unit tests (`test_instructions.py`)

1. **CLAUDE.md fallback works** — only `CLAUDE.md` present → returns its content
2. **AGENTS.md takes priority** — both files present → returns AGENTS.md content
3. **Explicit filename overrides fallback** — `filename=".marim.md"` ignores both AGENTS.md and CLAUDE.md
4. **Both missing → None** — no files present → returns None (existing test, still passes)

### Integration test (`test_agent_instructions.py`)

5. **CLAUDE.md injected into agent prompt** — no AGENTS.md, CLAUDE.md present → instructions contain its content, with correct ordering (base prompt < CLAUDE.md content)
