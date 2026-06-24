# Harness `bind_ui()` — UI Callback Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cluster of eight direct attribute pokes the TUI app makes into harness internals (`harness.deps.*`, `harness.deps.tasks/jobs.on_change`, `harness.session.on_*`) with a single `Harness.bind_ui(...)` method, so the interactive UI wires its callbacks through one named, testable entry point instead of reaching two-to-three levels into the harness's objects.

**Architecture:** Today `HarnessApp.__init__` (`interfaces/tui/app.py`) sets eight callbacks by directly assigning to `self.harness.deps.request_approval`, `…ask_user`, `…on_subagent_event`, `self.harness.deps.tasks.on_change`, `self.harness.deps.jobs.on_change`, `self.harness.session.on_compact`, `…on_compact_start`, and `…on_rename`. That breaks encapsulation: the App knows the internal layout of `deps`, `deps.tasks`, `deps.jobs`, and `session`. This plan adds one keyword-only `bind_ui()` method on `Harness` that performs those eight assignments internally; the App calls it once. Headless never calls it — the callbacks stay `None` and every reader already guards.

**Tech Stack:** Python 3, dataclasses, pydantic-ai, Textual (TUI), pytest, `uv`.

## Global Constraints

- Run tests with `uv run pytest`.
- **No behavioral change.** `bind_ui` must assign the same eight callbacks to the same eight targets that `app.py` assigns today — same callables, same destinations. Existing `test_app.py` tests invoke `app.harness.session.on_compact(...)`, `…on_compact_start()`, and `app.stream.on_subagent_event(...)`; those targets must still be wired after construction.
- `bind_ui` is keyword-only, all parameters default to `None`. Headless does not call it.
- The eight targets and their exact attributes (all confirmed to exist):
  - `self.deps.request_approval` (`Optional[ApprovalFn]`)
  - `self.deps.ask_user` (`Optional[AskUserFn]`)
  - `self.deps.on_subagent_event` (`Optional[SubAgentEventCb]`)
  - `self.deps.tasks.on_change` (`TaskList.on_change`)
  - `self.deps.jobs.on_change` (`JobRegistry.on_change`)
  - `self.session.on_compact` (`SessionController.on_compact`)
  - `self.session.on_compact_start` (`SessionController.on_compact_start`)
  - `self.session.on_rename` (`SessionController.on_rename`)
- `Callable` and `Any` are already imported in `agent.py` (added by the prior `build_collaborators` task) — reuse them; do not add new typing imports.
- Full suite is currently 1231 passed, 1 skipped; after the one new test expect 1232 passed, 1 skipped.

---

### Task 1: Add `Harness.bind_ui()` and route the app through it

**Files:**
- Modify: `src/marim_harness/agent.py` — add a `bind_ui` method on `Harness` (place it right after `__init__`, before `resume`, around line 230 in the post-`build_collaborators` file).
- Modify: `src/marim_harness/interfaces/tui/app.py` — replace the eight assignment lines (currently lines 86-93) with one `bind_ui(...)` call.
- Test: `tests/test_agent.py` — add a unit test using the existing `_minimal_harness` helper.

**Interfaces:**
- Produces: `Harness.bind_ui(self, *, request_approval=None, ask_user=None, on_subagent_event=None, on_tasks_changed=None, on_jobs_changed=None, on_compact=None, on_compact_start=None, on_rename=None) -> None` — assigns each callback to its target as listed in Global Constraints.
- Consumes: the existing `_minimal_harness(tmp_path)` helper in `tests/test_agent.py` (builds a `Harness` with `TestModel()`, `BuiltinToolProvider()`, `Deps(workspace_root=tmp_path)`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent.py` (the `_minimal_harness` helper is defined at line 164; add this test below the existing LSP-lifecycle tests so the helper is in scope):

```python
def test_bind_ui_wires_all_callbacks(tmp_path):
    h = _minimal_harness(tmp_path)

    def request_approval(_): ...
    def ask_user(_): ...
    async def on_subagent_event(sid, event, usage=None): ...
    def on_tasks_changed(): ...
    def on_jobs_changed(): ...
    def on_compact(before, after): ...
    def on_compact_start(): ...
    def on_rename(old, new): ...

    h.bind_ui(
        request_approval=request_approval,
        ask_user=ask_user,
        on_subagent_event=on_subagent_event,
        on_tasks_changed=on_tasks_changed,
        on_jobs_changed=on_jobs_changed,
        on_compact=on_compact,
        on_compact_start=on_compact_start,
        on_rename=on_rename,
    )

    assert h.deps.request_approval is request_approval
    assert h.deps.ask_user is ask_user
    assert h.deps.on_subagent_event is on_subagent_event
    assert h.deps.tasks.on_change is on_tasks_changed
    assert h.deps.jobs.on_change is on_jobs_changed
    assert h.session.on_compact is on_compact
    assert h.session.on_compact_start is on_compact_start
    assert h.session.on_rename is on_rename
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_agent.py::test_bind_ui_wires_all_callbacks -v`
Expected: FAIL — `AttributeError: 'Harness' object has no attribute 'bind_ui'`.

- [ ] **Step 3: Implement `bind_ui` on `Harness`**

In `src/marim_harness/agent.py`, add this method immediately after `Harness.__init__` ends (after the `self.subagents = collab.subagents` line) and before the `# --- session lifecycle ...` comment / `def resume`:

```python
    def bind_ui(
        self,
        *,
        request_approval: Optional[Callable[..., Any]] = None,
        ask_user: Optional[Callable[..., Any]] = None,
        on_subagent_event: Optional[Callable[..., Any]] = None,
        on_tasks_changed: Optional[Callable[..., Any]] = None,
        on_jobs_changed: Optional[Callable[..., Any]] = None,
        on_compact: Optional[Callable[..., Any]] = None,
        on_compact_start: Optional[Callable[..., Any]] = None,
        on_rename: Optional[Callable[..., Any]] = None,
    ) -> None:
        """Wire the interactive UI's callbacks into the harness in one place.

        The TUI app calls this once at construction instead of reaching into
        ``harness.deps`` / ``harness.deps.tasks`` / ``harness.deps.jobs`` /
        ``harness.session`` field by field. Headless never calls it: the
        callbacks stay ``None`` and every reader guards with an ``is None``
        check.
        """
        self.deps.request_approval = request_approval
        self.deps.ask_user = ask_user
        self.deps.on_subagent_event = on_subagent_event
        self.deps.tasks.on_change = on_tasks_changed
        self.deps.jobs.on_change = on_jobs_changed
        self.session.on_compact = on_compact
        self.session.on_compact_start = on_compact_start
        self.session.on_rename = on_rename
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_agent.py::test_bind_ui_wires_all_callbacks -v`
Expected: PASS.

- [ ] **Step 5: Route the app through `bind_ui`**

In `src/marim_harness/interfaces/tui/app.py`, replace these eight consecutive lines (currently lines 86-93):

```python
        self.harness.deps.request_approval = self._request_approval
        self.harness.deps.ask_user = self._ask_user
        self.harness.deps.tasks.on_change = self._on_tasks_changed
        self.harness.deps.jobs.on_change = self._on_jobs_changed
        self.harness.deps.on_subagent_event = self.stream.on_subagent_event
        self.harness.session.on_compact = self._on_compact
        self.harness.session.on_compact_start = self._on_compact_start
        self.harness.session.on_rename = self.session.on_rename
```

with a single call:

```python
        self.harness.bind_ui(
            request_approval=self._request_approval,
            ask_user=self._ask_user,
            on_subagent_event=self.stream.on_subagent_event,
            on_tasks_changed=self._on_tasks_changed,
            on_jobs_changed=self._on_jobs_changed,
            on_compact=self._on_compact,
            on_compact_start=self._on_compact_start,
            on_rename=self.session.on_rename,
        )
```

Note: `self.stream` and `self.session` (the App's `SessionView`) are assigned earlier in `__init__` (lines ~79-80), and the `self._...` handlers are methods on the App class, so all eight references are valid at this point. `on_rename=self.session.on_rename` passes the App's `SessionView.on_rename`; `bind_ui` assigns it to the harness's `SessionController.on_rename` — same wiring as before.

- [ ] **Step 6: Run the full suite to confirm no behavioral change**

Run: `uv run pytest --no-header -q -o addopts=""`
Expected: PASS — `1232 passed, 1 skipped`. Pay attention to `test_app.py` (the compaction tests at ~818/835/840 call `app.harness.session.on_compact`/`on_compact_start`, which must still be wired) and any subagent-event app tests — a failure there means a callback or target name drifted in the `bind_ui` call.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/agent.py src/marim_harness/interfaces/tui/app.py tests/test_agent.py
git commit -m "refactor(tui): wire UI callbacks through Harness.bind_ui instead of poking internals"
```

---

## Self-Review

**Spec coverage:**
- "One `bind_ui` method replacing the eight pokes" → Task 1 adds the method (Step 3) and routes the app through it (Step 5). ✔
- "Same callbacks, same targets, no behavioral change" → Step 3 assigns exactly the eight documented targets; Step 5 passes exactly the eight callables the app set before; Step 6 runs the full suite, calling out the `test_app.py` tests that depend on the wiring. ✔
- "keyword-only, default None, headless doesn't call it" → signature in Step 3; docstring states the headless contract. ✔
- "Reuse existing `Callable`/`Any` imports" → Global Constraints; Step 3 uses `Optional[Callable[..., Any]]`. ✔

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to" — the method body, the test, and the exact old/new app.py blocks are all shown in full.

**Type consistency:** The eight parameter names (`request_approval`, `ask_user`, `on_subagent_event`, `on_tasks_changed`, `on_jobs_changed`, `on_compact`, `on_compact_start`, `on_rename`) match across the `bind_ui` signature (Step 3), the app.py call (Step 5), and the test (Step 1). The eight assignment targets in Step 3 match the targets the app.py block (Step 5 "before") currently writes.
