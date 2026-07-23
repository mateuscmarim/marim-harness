# Coding Guidelines Compliance Review

**Date:** 2026-03-23  
**Scope:** Full `src/marim_harness/` tree  
**Guidelines:** `coding-guidelines.md` (Object Calisthenics — Pragmatic Rules, 9 rules)  
**Method:** 6 parallel per-subsystem reviews by `claude-general`, each reading its own files.

## Scorecard

| Rule | Description | Verdict |
|------|-------------|---------|
| 1 | Control Complexity | ✅ mostly — `_execute_spawn`, `_run_with_approval`, TUI dispatcher on the heavier side |
| 2 | Prefer Straight-Line Flow | ✅ clean everywhere |
| 3 | Model the Domain | ✅ strong — `JobRegistry`, `TaskList`, `Mode`, `TokenSplit` are good domain types |
| 4 | Encapsulate Collections | ✅ clean |
| 5 | Limit Deep Navigation | ⚠️ `Harness` reaching into `TurnController` internals; `session.chain` nav |
| 6 | Name for Clarity | ✅ strong — handful of low-severity local-naming nits |
| 7 | Optimize for Cohesion | ⚠️ some duplication: atomic_io, errors, agents/skills discovery, TUI replay methods |
| 8 | Treat Large State | ⚠️ `ModelConfig` (19 fields), `Deps` sub-agent callbacks, TUI renderer stream dicts |
| 9 | Encapsulate Behavior | ⚠️ `Harness` sets collaborator fields that should self-manage |

**Totals:** 0 high, 22 medium, 15 low.

---

## High-priority findings (severity = high)

### H1. `subagents.py:387–492` — `_execute_spawn` mixes two lifecycle paths (Guidelines 1, 7)

`_execute_spawn` is ~106 lines with an explicit `background: bool` parameter and three separate `if background:` branches interspersed throughout. Tracing either path requires mentally filtering out the other.

**Fix:** Extract shared setup into a `_prepare_spawn()` helper returning a small struct, then delegate to `_execute_foreground_spawn` / `_execute_background_spawn` for diverging tails. The CLI early-return stays.

### H2. `session_view.py:33,166` — `replay_history` / `replay_messages_into` share 70% identical logic (Guidelines 1, 7)

`replay_history` (132 lines) and `replay_messages_into` (72 lines) have near-copy-pasted `TextPart`, `ThinkingPart`, `ToolCallPart`, `ToolReturnPart` dispatch arms. Any future change to part rendering must be applied in both.

Note: `replay_history` also carries arms the sub-pane path does not (`UserPromptPart`/`SummaryWidget`, `ask_user` standalone mount, `SubAgentDetailHost` pane creation, `model_label` fallback) — the extraction must keep those main-log-only behaviors out of the sub-pane path.

**Fix:** Extract `async def _replay_parts(self, message, container, mount_fn, *, build_pane: bool)` parameterized on mount callable + pane flag. Both existing methods call it. Main-log-only arms stay in `replay_history` above/below the shared call.

---

## Medium-priority findings

### M1. `turn_controller.py:358–502` — `_run_with_approval` complexity (Guideline 1)

144 lines, 5 nesting levels at the deepest, 10 distinct concerns (token accounting, overflow retry, flush, error notes, provider-error dump, approval round, persist/compact/return). Justified by CLAUDE.md as the "heart of the system" but not fully tamed.

**Fix:** Extract `_handle_run_failure` and `_run_approval_round` private methods. Main `while True` shrinks to 3 paths at ≤2 nesting levels.

### M2. `agent.py:447,347` — `Harness` writes private fields of `TurnController` (Guidelines 5, 9)

```python
self.turn_controller._pending_hook_context = ctx
self.turn_controller._pending_jobs_digest = None
```

**Fix:** Add `apply_session_start_context()` and `clear_pending_jobs_digest()` methods on `TurnController`.

### M3. `agent.py:362–365,413–419` — duplicated `self.session.store.model` navigation (Guidelines 5, 9)

The same 3-level chain appears twice in nearly identical guards.

**Fix:** Add `SessionController.saved_model_id` property.

### M4. `agent.py:388–391` — `Harness` rebuilds aux agents that `SessionController` owns (Guidelines 5, 9)

```python
if self.session.summarizer is not None:
    self.session.summarizer = make_summarizer(model)
```

**Fix:** Move into `SessionController.update_model(model)`.

### M5. `checkpoints.py:99` — duck-typed `session` collaborator forces `getattr` chains (Guideline 5)

`CheckpointManager` doesn't annotate `session`'s type, forcing defensive `getattr` in two methods.

**Fix:** Add `session: "SessionController"` annotation; extract `_store()` helper.

### M6. `store.py:251–262` — `SessionInfo` missing `model` causes double disk read (Guidelines 7, 8)

`latest_model()` calls `latest()` (which reads all files) then reads the latest file again for `model`. The field belongs in `SessionInfo`.

**Fix:** Add `model: str | None = None` to `SessionInfo`; populate in `list()`.

### M7. `config/model.py:23–69` — `ModelConfig` has 19 fields across 7 groups (Guideline 8)

**Fix:** Extract nested value objects (`SubagentConfig`, `NotificationConfig`).

### M8. `deps.py:84–120` — four sub-agent callbacks flat on `Deps` (Guideline 8)

**Fix:** Group into a `SubAgentCallbacks` dataclass.

### M9. `tools/fs.py:452–530` — `grep` output-collection logic scattered across 4 nonlocal vars (Guidelines 8, 9)

**Fix:** Extract `_OutputCollector` class.

### M10. `atomic_io.py:93–143` — `atomic_write_text` / `atomic_write_bytes` share 35 lines (Guideline 7)

**Fix:** Extract `_atomic_write_core(path, open_kwargs, write_fn)`.

### M11. `errors.py:21–36,128–143` — duplicate exception-chain walkers (Guideline 7)

**Fix:** Single `_find_in_chain(exc, exc_class)` helper.

### M12. `workspace/agents.py` / `workspace/skills.py` — near-identical discovery + caching (Guideline 7)

**Fix:** Extract generic `_cached_discover(roots, sig_fn, collect_fn)` helper.

### M13. `stream_render.py:293` — 4 parallel `dict[str, X]` keyed by `stream_id` (Guidelines 8, 4)

Plus never-pruned — stale entries accumulate for session lifetime.

**Fix:** `@dataclass class _SubStreamState`; replace 4 dicts with `dict[str, _SubStreamState]`; prune on sub-agent finish.

### M14. `stream_render.py:674` — 106-line `dispatch_stream_event` (Guideline 1)

**Fix:** Extract `_on_text_start`, `_on_text_delta`, `_on_thinking_start`, etc.

### M15. `mcp/config.py:316` — class defined inside `with` inside function (Guideline 1)

**Fix:** Move `_QuietStdioServer` to module scope; requires also hoisting the `MCPServerStdio` import and re-checking where the `DeprecationWarning` needs suppressing.

**Severity: low** (downgraded from medium — deliberate, contained localization pattern).

### M16. `mcp/manager.py:82,127` — raw `mcp_status` dict manually bookkept (Guidelines 4, 9)

**Fix:** `@dataclass class McpStatus` with `add_connected`, `add_failed`, `remove`, `to_dict()`.

### M17. `interfaces/tui/app.py:291,328,347` — `self.harness.deps.jobs` nav repeated 6× (Guideline 5)

**Fix:** Add `HarnessApp.jobs` property.

---

## Low-priority findings (abbreviated)

| # | File | Issue | Fix |
|---|------|-------|-----|
| L1 | `store.py:207` | `(saved or {})` evaluated 3× | Hoist to `meta =` |
| L2 | `checkpoints.py:331,338` | Duplicate ref-deletion loop | Extract `_delete_all_refs()` |
| L3 | `checkpoints.py:243` | `pre` too abbreviated | `pre_restore_commit` |
| L4 | `ctrl.py:278` | `start_cb` / `indicator_shown` indirection | Inline |
| L5 | `ctrl.py:283` | `did` ambiguous fragment | `compacted` |
| L6 | `instructions.py:113` | `g` / `p` single-letter names | `global_index` / `project_index` |
| L7 | `turn_controller.py:202` | `_consumed_this_turn` positional tuple | `@dataclass _ConsumedContext` |
| L8 | `tools/fs.py:235` | `list[int]` as mutable counter box | Named class or `nonlocal` |
| L9 | `tools/provider.py:512` | Nested ternary reads backwards | Flat `if/else` |
| L10 | `tools/fetch.py:299` | `.encode()` round-trip on str | Compare str to str |
| L11 | `notifications.py:127` | Comment describes wrong branch | Move to `except` block |
| L12 | `notifications.py:215` | `chr(39)` obscures intent | `"'"` literal |
| L13 | `workspace/agents.py:141` | `.strip()` evaluated twice | `_opt_str` helper |
| L14 | `workspace/snapshot.py:116` | `restore()` complexity ≈ 6 | Extract `_remove_extra_files()` |
| L15 | `workspace/worktree.py:109` | `flush()` closure with `nonlocal` | Explicit loop accumulator |
| L16 | `plugins/discovery.py:156` | Same resolve logic ×4, 2 idioms | `_resolve_hooks_entries()` |
| L17 | `plugins/install.py:176` | `setattr` on known attrs | Direct assignment or methods |
| L18 | `lsp/manager.py:202` | 4-line guard repeated 5× | `_require_server()` helper |
| L19 | `stream_render.py:128` | Abstract base not using `abc` | `abc.ABC` + `@abstractmethod` |

---

## What's done well

Across the entire codebase, the following patterns are worth preserving:

- **Domain modeling.** `TaskList`, `JobRegistry`, `Mode`, `TokenSplit`, `CommandPolicy`, `CachedImage`, `NotificationConfig` all carry real behavior — no empty data bags.
- **Straight-line flow.** Early returns and early continues are the default. `if/else` is used when it reads better. No unnecessary `else` after `return`.
- **"Why" comments.** `atomic_io.py` explains the old deterministic-name race; `compaction.py` explains the force= extension; `command_policy.py` explains fail-closed regex. The codebase is generous with *why*, terse with *what* — exactly the right ratio.
- **Pure helpers separated from I/O.** `turn_context.py`, `command_policy.py`, `lsp/registry.py`, `lsp/checks.py` are side-effect-free and easy to test.
- **Factory-function wiring.** `build_collaborators` / `build_harness` keep construction out of `__init__`; the `Collaborators` frozen dataclass makes the wiring output explicit.
- **Defensive defaults.** `CommandPolicy._MATCH_ALL` / `_MATCH_NONE` fail closed on bad regex. `Snapshotter` Protocol + `NullSnapshotter` keeps git out of the checkpoints module. `atomic_io.file_lock` degrades to no-op on non-POSIX.
- **`_VersionedHistory`** (`ctrl.py`) is a textbook encapsulating-collections-with-behavior example — all in-place mutators overridden, version-bump invariant documented.
- **Naming.** Consistently explicit. `_coerce_timeout`, `_linked_elevation_revokes_trust`, `subagent_failed`, `spill_ref` — no cryptic abbreviations in any reviewed file.

---

## Recommended order of attack

1. **H1 + H2** — highest severity, localized, no risk.
2. **M2, M3, M4** — all in `agent.py` / `turn_controller.py`; one PR cleans up the Harness/TurnController boundary.
3. **M6, M10, M11** — small, mechanical, low-risk.
4. **M9, M13** — single-file extractions.
5. **M1, M5, M7, M8, M12, M14, M15, M16** — larger structural changes; each is independent so they can be parallelized.
6. **L1–L19** — batch as a "hygiene" PR.
