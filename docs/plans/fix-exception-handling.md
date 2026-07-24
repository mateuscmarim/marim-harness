# Plan: Fix Broad Exception Handling

**Status:** Ready for execution
**Created:** 2026-07-24
**Source:** Codebase review — weakness #3

## Problem

87 broad `except Exception` sites across 40 files (34 unbound, 53 bound). Zero
`logger.exception()` calls. ~35 sites log without traceback, ~20 are completely
silent. Bugs get swallowed invisibly.

## Proposed Standard

Every `except Exception` must include traceback information:

```python
# Defensive, no recovery needed (expected degradation)
except Exception:
    logger.debug("context", exc_info=True)

# Recovery uses the exception (unexpected but recoverable)
except Exception as exc:
    logger.warning("context: %s", exc, exc_info=True)
```

**Log level guidance:**
- `logger.debug` — expected degradation (catalog failures, config parse errors,
  notification failures, tool coercion failures). Normal operation, not worth
  alerting on.
- `logger.warning` — unexpected but recoverable (transcript corruption, session
  titler failure, hook crashes, LSP server start failure). Worth noticing but
  not crashing.
- **No change** — TUI-surfaced errors (user already sees them) and intentionally
  silent sites (UI rendering fallbacks, shutdown cleanup).

## Categories

| Category | Count | Action | Log Level |
|---|---|---|---|
| Already has `exc_info=True` | 15 | Add `as exc` if unbound | — |
| Log without traceback | 35 | Add `exc_info=True` | debug or warning |
| Completely silent | 20 | ~10 should log, ~10 stay silent | debug |
| TUI-surfaced errors | 7 | Leave alone — user sees them | — |
| Import guards / type narrowing | 3-5 | Narrow to specific exception type | — |
| `except BaseException` (correct) | 5 | Leave alone | — |

## PR Structure

### PR 1: Add traceback logging (zero regression risk)

30 sites across 25 files. Purely additive — never changes control flow.

Changes:
- Add `as exc` to all unbound `except Exception:` catches
- Add `exc_info=True` to existing log calls that lack traceback
- Add `logger.debug(..., exc_info=True)` to silent catches that should log
- Explicitly exclude 7 TUI-surfaced and 10 intentionally-silent sites

Estimated: 5-7 hrs

### PR 2: Narrow exception types (needs review)

3-5 sites where the called API's exceptions are known:

- `runtime/errors.py` ×2 → `except ImportError` (import guards)
- `interfaces/tui/app.py:351` → `except OSError` (driver write)
- `compaction.py:413` — investigate if serialization-specific narrowing is safe

Add tests verifying the new boundaries.

Estimated: 2-3 hrs

### PR 3: Linter rule (optional)

Add flake8-bugbear BLE001 rule with an approved-uses list to prevent future
unbound `except Exception:` regressions.

## Sites deliberately left unchanged

- `run_driver.py:169` — analyzes exception programmatically for overflow
  classification, not for logging
- `compaction.py:413` — exotic content serialization, deliberately silent
- 5 `except BaseException` sites — correctly broad for cancellation handling
- 7 TUI-surfaced errors (settings.py ×6, providers.py ×1) — user already sees them
- ~10 intentionally-silent sites (highlight.py ×2, tools.py ×2, prompt.py,
  stream_render.py, status.py, app.py:351 shutdown, jobs.py:340 race guard)

## Worst Offenders (PR 1 priority order)

1. `interfaces/tui/app.py` — 6 catches, 2 completely silent on critical turn/spawn paths
2. `compaction.py` — 3 catches, 2 silent on masking/persist paths
3. `runtime/controller.py` — 5 catches, 3 unbound, already well-logged
4. `interfaces/tui/widgets/` — 5 silent catches across 3 files (render fallbacks)
5. `lsp/manager.py` — 4 catches, all logged without traceback
6. `mcp/manager.py` — catches without traceback
7. `subagents/runner.py` — multiple catches across 1252-line file
