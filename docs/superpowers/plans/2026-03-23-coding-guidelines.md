# Plan: Coding-Guidelines Compliance Fixes

**Created:** 2026-03-23  
**Source:** `docs/review-coding-guidelines-2026-03-23.md` (verified)  
**Branch:** `dev`  
**Base:** `main`

## Global Conventions

- Follow `coding-guidelines.md` (Object Calisthenics, 9 rules).
- Match existing code style (ruff line-length 100, isort, early-return bias).
- Every change needs a focused test proving it; run the full suite once before committing.
- Commit per finding or per logical group — no bundled unrelated changes.
- Do NOT restructure code outside the scope of a finding.
- Ruff lint + pyright type-check + `uv run pytest --no-cov` must pass at every commit.

## Task List

### Task 1 — Extract `_replay_parts` in session_view.py (H2)

**Findings:** H2 — `replay_history` (132 lines) and `replay_messages_into` (72 lines) share ~70% identical `TextPart`/`ThinkingPart`/`ToolCallPart`/`ToolReturnPart` dispatch arms.

**What to do:**
1. Read `src/marim_harness/interfaces/tui/session_view.py` — understand both methods fully.
2. Extract shared dispatch into `async def _replay_parts(self, message, container, mount_fn, *, build_pane: bool)`.
3. `replay_history` keeps its `UserPromptPart`/`SummaryWidget`, `ask_user` standalone-mount, `SubAgentDetailHost` pane creation, and `model_label` fallback arms — these stay in the main-log path, not the shared helper.
4. Both public methods call `_replay_parts` for the shared part dispatch.
5. Add a test asserting both methods render identical part types identically (snapshot or output comparison).

**Acceptance:** Both methods produce the same output for the shared part types; no behavioral change; tests pass.

---

### Task 2 — Split `_execute_spawn` in subagents.py (H1)

**Findings:** H1 — `_execute_spawn` (~106 lines) mixes foreground + background lifecycle via `if background:` branches.

**What to do:**
1. Read `src/marim_harness/subagents.py:387–492`.
2. Extract shared setup (worktree open, agent build, MCP grant, hooks, timing probe) into `_prepare_spawn()` returning a small struct.
3. Delegate diverging tails to `_execute_foreground_spawn` and `_execute_background_spawn`.
4. The CLI early-return (`defn.backend == "claude-cli"`) stays as-is.
5. Existing tests must pass unchanged (behavior-preserving refactor).

**Acceptance:** No test changes needed; behavior identical; full suite green.

---

### Task 3 — Harness ↔ TurnController encapsulation (M2, M3, M4)

**Findings:**
- M2: `Harness` writes `turn_controller._pending_hook_context` and `_pending_jobs_digest` directly.
- M3: Duplicated `self.session.store.model` navigation chain in `agent.py`.
- M4: `Harness` rebuilds `session.summarizer` / `session.titler` directly.

**What to do:**
1. Add `TurnController.apply_session_start_context(ctx)` and `clear_pending_jobs_digest()` methods; call from `Harness`.
2. Add `SessionController.saved_model_id` property; replace both navigation sites.
3. Add `SessionController.update_model(model)` that rebuilds aux agents; call from `Harness.set_model`.
4. Add tests for each new method/property.

**Acceptance:** No more `Harness` → private-field writes; no more `session.store.model` navigation from outside `SessionController`.

---

### Task 4 — Fix CheckpointManager duck-typing and double read (M5, M6)

**Findings:**
- M5: `CheckpointManager.session` is untyped → defensive `getattr` chains.
- M6: `SessionInfo` missing `model` field → `latest_model()` double-reads disk.

**What to do:**
1. Add `session: "SessionController"` annotation to `CheckpointManager.__init__`.
2. Extract `_store()` helper; replace `getattr` chains in `_sidecar_path()` and `_session_id()`.
3. Add `model: str | None = None` to `SessionInfo`; populate in `SessionStore.list()`.
4. Simplify `latest_model()` to `self.latest().model`.
5. Add a test for `latest_model()` correctness.

**Acceptance:** Single disk read for model lookup; direct attribute access in `CheckpointManager`.

---

### Task 5 — Small mechanical fixes (M9, M10, M11, L1, L2, L9, L10)

**Findings:** Batch of low-risk, single-file fixes.

**What to do:**
1. **M9 (fs.py:452):** Extract `_OutputCollector` class for grep output budget tracking.
2. **M10 (atomic_io.py:93):** Extract `_atomic_write_core(path, open_kwargs, write_fn)`; both public functions become 2-line wrappers.
3. **M11 (errors.py:21,128):** Extract `_find_in_chain(exc, exc_class)`; both walkers become wrappers.
4. **L1 (store.py:207):** Hoist `saved or {}` to `meta =` once.
5. **L2 (checkpoints.py:331,338):** Extract `_delete_all_refs()` helper.
6. **L9 (provider.py:512):** Flatten nested ternary to `if/else`.
7. **L10 (fetch.py:299):** Remove `.encode()` round-trip on str comparisons.

**Acceptance:** Each fix is behavior-preserving; lint + tests pass.

---

### Task 6 — Cohesion fixes in config and workspace (M7, M8, M12, L3, L13)

**Findings:**
- M7: `ModelConfig` has 19 fields — extract value objects.
- M8: Four sub-agent callbacks flat on `Deps`.
- M12: `agents.py` / `skills.py` near-identical discovery + caching.
- L3: `agents.py:141` double `.strip()` evaluation.
- L13: `snapshot.py:116` `restore()` complexity.

**What to do:**
1. **M7:** Extract `SubagentConfig` and `NotificationConfig` from `ModelConfig`; update `load_config`.
2. **M8:** Add `SubAgentCallbacks` dataclass to `deps.py`; reduce `Deps` fields.
3. **M12:** Extract generic `_cached_discover(roots, sig_fn, collect_fn)` helper; both modules call it.
4. **L3:** Add `_opt_str(raw, default)` helper; eliminate double `.strip()`.
5. **L13:** Extract `_remove_extra_files()` from `GitSnapshotter.restore()`.
6. Add tests for config loading and discovery caching.

**Acceptance:** `ModelConfig` ≤ 12 fields; `Deps` ≤ 14 fields; shared discovery helper tested.

---

### Task 7 — TUI renderer stream state (M13, M14)

**Findings:**
- M13: Four parallel `dict[str, X]` keyed by `stream_id` in `StreamRenderer` — never pruned.
- M14: 106-line `dispatch_stream_event`.

**What to do:**
1. **M13:** Introduce `@dataclass class _SubStreamState`; replace four dicts with `dict[str, _SubStreamState]`; prune on sub-agent finish.
2. **M14:** Extract `_on_text_start`, `_on_text_delta`, `_on_thinking_start`, `_on_thinking_delta`, `_on_tool_call`, `_on_tool_result` private methods; dispatcher delegates.
3. Add a test asserting stream state is pruned after sub-agent completion.

**Acceptance:** No stale entries in stream dicts after sub-agent finish; dispatcher is six short delegations.

---

### Task 8 — MCP / plugin / LSP cleanups (M15, M16, M17, L14, L15, L16, L17, L18, L19)

**Findings:** Batch of medium/low fixes across MCP, plugins, LSP, and remaining items.

**What to do:**
1. **M15 (mcp/config.py:316):** Move `_QuietStdioServer` to module scope; hoist import; re-check warning suppression.
2. **M16 (mcp/manager.py:82):** Add `@dataclass class McpStatus` with `add_connected`, `add_failed`, `remove`, `to_dict()`.
3. **M17 (app.py:291):** Add `HarnessApp.jobs` property; replace `self.harness.deps.jobs` nav.
4. **L14 (notifications.py:127):** Move rollback comment to `except` block.
5. **L15 (notifications.py:215):** Replace `chr(39)` with `"'"`.
6. **L16 (plugins/discovery.py:156):** Extract `_resolve_hooks_entries()` / `_resolve_mcp_servers()` helpers.
7. **L17 (plugins/install.py:176):** Replace `setattr` on known attrs with direct assignment or methods.
8. **L18 (lsp/manager.py:202):** Extract `_require_server()` helper; five call sites use it.
9. **L19 (stream_render.py:128):** Make `_StreamSink` an `abc.ABC` with `@abstractmethod`.

**Acceptance:** Each fix behavior-preserving; lint + tests pass.

---

### Task 9 — Remaining low-priority hygiene (L4, L5, L6, L7, L8, L11, L12)

**Findings:** Final batch of low-severity nits.

**What to do:**
1. **L4 (store.py:207):** Already covered in Task 5 (L1). Skip if done.
2. **L5 (ctrl.py:278):** Inline `start_cb` / `indicator_shown` indirection.
3. **L6 (ctrl.py:283):** Rename `did` → `compacted`.
4. **L7 (turn_controller.py:202):** Replace `_consumed_this_turn` tuple with `@dataclass _ConsumedContext`.
5. **L8 (fs.py:235):** Replace `list[int]` counter box with a named class or `nonlocal`.
6. **L11 (checkpoints.py:243):** Rename `pre` → `pre_restore_commit`.
7. **L12 (instructions.py:113):** Rename `g`/`p` → `global_index`/`project_index`.

**Acceptance:** All single-line or single-name changes; tests pass.

---

## Verification

After all tasks:
1. `uv run ruff check src tests` — clean.
2. `uv run pyright` — clean.
3. `uv run pytest --no-cov` — all green.
4. `git diff main..HEAD` reviewed by final code-reviewer agent.
