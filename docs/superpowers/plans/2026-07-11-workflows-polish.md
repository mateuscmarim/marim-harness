# Workflows Polish Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the dynamic-workflows feature: hardened model-facing docstring, a first-class workflow-run card in the sub-agents screen (with persisted `log()` lines and children nested under it), and structured output for schema'd `agent()` calls with a prompt-contract fallback for `claude-cli` spawns.

**Architecture:** The engine announces the run's lifecycle through two new optional `UIHooks` callbacks (`on_workflow_start` / `on_workflow_done`), mirroring the existing `on_workflow_spawn` / `on_workflow_spawn_done` pair; the TUI claims an *unmounted* `SubAgentWidget` for the run keyed in a new `workflow_cards` dict (never `tool_widgets` — that key belongs to the run_workflow `ToolCallWidget`). Schema enforcement moves from prompt text to the spawn seam: `SubagentRunner.run(..., output_schema=...)` builds the child with `output_type=StructuredDict(schema)` natively, with the prompt contract as fallback for `claude-cli` backends and non-object schema roots — the decision lives in a new pure helper in core (`subagents/output_schema.py`) because `jsonschema` is `[workflows]`-extra-only and the runner is core.

**Tech Stack:** Python ≥3.10, Pydantic AI 2.8 (`StructuredDict`), Textual, pydantic-monty (workflows extra), pytest + anyio.

**Spec:** `docs/superpowers/specs/2026-07-11-workflows-polish-design.md` (approved 2026-07-11).

## Global Constraints

- Work on branch `feat/workflows-polish` (already exists, HEAD has the spec commit).
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pytest`/`pip`.
- `requires-python >=3.10` — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity cap 10 per function (extract helpers, never `# noqa: C901`).
- CI order locally before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- All new `UIHooks` callbacks are optional and default `None`; headless never sets them and every engine call site guards with `getattr(..., None)` / `is None` — no headless behavior change.
- Never modify `workflows/schema.py`'s jsonschema-dependent helpers' home: `jsonschema` is available only with the `[workflows]` extra; nothing under `subagents/` or `runtime/` may import it (directly or transitively).
- `git add` ONLY the files your task creates/modifies — NEVER `git add -A` or `git add .` (concurrent sessions share this checkout). The untracked file `scratch-canary.md` is not ours: never add, modify, or delete it.
- Every commit message ends with exactly these two lines:
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN
  ```
- Preserve the long "why" comments around resumability, the deps/services cycle, and the Monty cancellation invariant when editing nearby code.
- Tool docstrings are model-facing product copy — write them with that in mind.

---

### Task 1: `run_workflow` docstring hardening

**Files:**
- Modify: `src/marim_harness/tools/workflow_tools.py` (the `run_workflow` docstring)
- Test: `tests/test_workflow_tool.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: nothing other tasks rely on (pure copy change).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_workflow_tool.py` (it already imports `run_workflow` from `marim_harness.tools.workflow_tools`):

```python
def test_docstring_warns_about_common_mistakes():
    """The run_workflow docstring is the model-facing product doc for the
    sandbox dialect; the common-mistakes section was added from failures
    observed in live runs, so a future rewrite must not silently drop it."""
    doc = run_workflow.__doc__ or ""
    assert "Common mistakes" in doc
    assert "print(result)" in doc
    assert "asyncio.run" in doc
    assert "log()" in doc
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest --no-cov tests/test_workflow_tool.py::test_docstring_warns_about_common_mistakes -v`
Expected: FAIL with `AssertionError` (no "Common mistakes" in the docstring).

- [ ] **Step 3: Add the common-mistakes section to the docstring**

In `src/marim_harness/tools/workflow_tools.py`, the docstring currently has this paragraph followed by the example:

```
    The script's LAST EXPRESSION is this tool's result — end with plain data
    (dict/list/str), JSON-serialized for you and spilled to a workspace file
    if very large. Keep intermediate results in variables; return only what
    you need.

    Example — parallel review sweep:
```

Insert a new paragraph between them so it reads:

```
    The script's LAST EXPRESSION is this tool's result — end with plain data
    (dict/list/str), JSON-serialized for you and spilled to a workspace file
    if very large. Keep intermediate results in variables; return only what
    you need.

    Common mistakes (each has burned a real run):
    - Ending with print(result): print returns None, so the tool result is
      None. End with the bare value — `result`, not `print(result)`.
    - Wrapping work in asyncio.run(...): the script body already runs in an
      event loop; `await` directly at top level.
    - Reporting through log(): log() is the progress channel only. The
      result must be the final expression.

    Example — parallel review sweep:
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest --no-cov tests/test_workflow_tool.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src/marim_harness/tools/workflow_tools.py tests/test_workflow_tool.py
uv run pyright
git add src/marim_harness/tools/workflow_tools.py tests/test_workflow_tool.py
git commit -m "docs(workflows): warn about common script mistakes in the run_workflow docstring

The docstring is the model-facing product doc for the sandbox dialect; add
the three failure modes observed in live runs (print(result) -> None result,
asyncio.run in an already-async body, log() used as the result channel), and
a marker test so a rewrite can't silently drop the section.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 2: Engine-announced run lifecycle hooks

**Files:**
- Modify: `src/marim_harness/runtime/deps.py` (UIHooks: new `on_workflow_start`/`on_workflow_done`, re-signed `on_workflow_log`)
- Modify: `src/marim_harness/workflows/engine.py` (`_script_title` helper, `run()` fires the hooks, `_log` carries the id)
- Modify: `src/marim_harness/runtime/harness.py` (`bind_ui` threads the new callbacks)
- Modify: `src/marim_harness/interfaces/tui/app.py` (adapt the existing `on_workflow_log` lambda to 2 args — keeps the app green until Task 3 replaces it)
- Test: `tests/test_workflow_engine.py`, `tests/test_workflow_wiring.py`, `tests/test_app.py` (update 2 existing call sites)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (Task 3 relies on these exact signatures):
  - `UIHooks.on_workflow_start: Callable[[str, str], None] | None` — `(tool_call_id, title)`, sync.
  - `UIHooks.on_workflow_done: Callable[[str, str, bool], None] | None` — `(tool_call_id, outcome, failed)`, sync, fired exactly once on every exit of an announced run.
  - `UIHooks.on_workflow_log` re-signed to `Callable[[str, str], None] | None` — `(tool_call_id, message)`.
  - `Harness.bind_ui(..., on_workflow_start=..., on_workflow_done=...)` keyword params.
  - `marim_harness.workflows.engine._script_title(script: str) -> str` (module-level, pure).

- [ ] **Step 1: Write the failing engine tests**

Append to `tests/test_workflow_engine.py`. The file already imports `asyncio` and `pytest`, has the `_engine(tmp_path, spawn, **kw)` fixture returning `(engine, deps)`, and the `_echo_spawn` fake. Add `_script_title` to the existing engine import line (`from marim_harness.workflows.engine import WorkflowEngine, ...` — extend it, don't duplicate the import):

```python
def test_script_title_prefers_the_leading_comment():
    assert _script_title("# review sweep\nx = 1") == "review sweep"


def test_script_title_falls_back_to_a_line_count():
    assert _script_title("x = 1\n# late comment") == "workflow script (2 lines)"
    assert _script_title("\n\n#\nx = 1") == "workflow script (4 lines)"


@pytest.mark.anyio
async def test_workflow_start_and_done_hooks_fire_on_success(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    events = []
    deps.ui.on_workflow_start = lambda tcid, title: events.append(("start", tcid, title))
    deps.ui.on_workflow_done = lambda tcid, outcome, failed: events.append(("done", tcid, failed))
    out = await eng.run('# review sweep\n"ok"', None, "tcS")
    assert events == [("start", "tcS", "review sweep"), ("done", "tcS", False)]
    assert out == '"ok"'


@pytest.mark.anyio
async def test_on_workflow_done_fires_failed_on_script_raise(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    events = []
    deps.ui.on_workflow_done = lambda tcid, outcome, failed: events.append((tcid, failed))
    out = await eng.run("boom_undefined_name", None, "tcE")
    assert "raised" in out
    assert events == [("tcE", True)]


@pytest.mark.anyio
async def test_on_workflow_done_fires_failed_on_timeout(tmp_path):
    async def slow_spawn(*a, **kw):
        await asyncio.sleep(5)
        return "never"

    eng, deps = _engine(tmp_path, slow_spawn, timeout_secs=0.1)
    events = []
    deps.ui.on_workflow_done = lambda tcid, outcome, failed: events.append((tcid, failed, outcome))
    out = await eng.run('await agent("x")\n"done"', None, "tcT")
    assert "timed out" in out
    assert events == [("tcT", True, out)]


@pytest.mark.anyio
async def test_on_workflow_done_fires_on_cancellation(tmp_path):
    started = asyncio.Event()

    async def slow_spawn(*a, **kw):
        started.set()
        await asyncio.sleep(30)
        return "never"

    eng, deps = _engine(tmp_path, slow_spawn)
    events = []
    deps.ui.on_workflow_done = lambda tcid, outcome, failed: events.append((tcid, outcome, failed))
    run = asyncio.ensure_future(eng.run('await agent("x")\n"done"', None, "tcC"))
    await started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert events == [("tcC", "workflow aborted", True)]


@pytest.mark.anyio
async def test_parse_failure_fires_no_lifecycle_hooks(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    events = []
    deps.ui.on_workflow_start = lambda *a: events.append(a)
    deps.ui.on_workflow_done = lambda *a: events.append(a)
    out = await eng.run("def broken(:\n    pass", None, "tcP")
    assert "failed to parse" in out
    assert events == []


@pytest.mark.anyio
async def test_log_lines_carry_the_tool_call_id(tmp_path):
    eng, deps = _engine(tmp_path, _echo_spawn)
    lines = []
    deps.ui.on_workflow_log = lambda tcid, msg: lines.append((tcid, msg))
    await eng.run('log("step 1")\n"ok"', None, "tcL")
    assert lines == [("tcL", "step 1")]
```

Also extend `tests/test_workflow_wiring.py::test_ui_hooks_default_workflow_callbacks_none`:

```python
def test_ui_hooks_default_workflow_callbacks_none():
    ui = UIHooks()
    assert ui.on_workflow_spawn is None
    assert ui.on_workflow_log is None
    assert ui.on_workflow_spawn_done is None
    assert ui.on_workflow_start is None
    assert ui.on_workflow_done is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py tests/test_workflow_wiring.py -v`
Expected: the new tests FAIL (`ImportError` on `_script_title`, `AttributeError`/`TypeError` on the new UIHooks fields — dataclass rejects unknown attribute assignment only at type-check time, so the hook tests fail because the engine never calls them).

- [ ] **Step 3: Add the UIHooks fields in `runtime/deps.py`**

In the `UIHooks` dataclass, replace the `on_workflow_log` field-and-comment:

```python
    # (message) -> None. A workflow script's log() line. None when headless
    # (the engine falls back to DEBUG logging).
    on_workflow_log: "Callable[[str], None] | None" = None
```

with:

```python
    # (tool_call_id, message) -> None. A workflow script's log() line, keyed
    # by the run's tool_call_id so the TUI can route it to the run's card.
    # None when headless (the engine falls back to DEBUG logging).
    on_workflow_log: "Callable[[str, str], None] | None" = None
```

and directly after the existing `on_workflow_spawn_done` field, add:

```python
    # (tool_call_id, title) -> None. Fired by the workflow engine once the
    # script has PARSED (a parse failure creates no run worth tracking), so
    # the TUI can claim a first-class card for the run itself in the
    # sub-agents screen — children then nest under it and log() lines have a
    # pane to land in.
    on_workflow_start: "Callable[[str, str], None] | None" = None
    # (tool_call_id, outcome, failed) -> None. Fired exactly once at EVERY
    # exit of a run announced by on_workflow_start — success, script raise,
    # timeout, and cancellation — so the claimed card always settles. The
    # failed flag is explicit because the engine knows which exit it took;
    # the UI never re-sniffs result text.
    on_workflow_done: "Callable[[str, str, bool], None] | None" = None
```

- [ ] **Step 4: Fire the hooks from `workflows/engine.py`**

Add the pure title helper at module level, directly after the `_PrintTail` class:

```python
def _script_title(script: str) -> str:
    """A short human label for the run's card: the script's first comment
    line when it opens with one (models usually title their scripts), else a
    line count. Pure; unit-tested directly."""
    for line in script.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            text = s.lstrip("#").strip()
            if text:
                return text
        break
    return f"workflow script ({len(script.splitlines())} lines)"
```

Replace the body of `run()` from the line `state = _RunState(tool_call_id=tool_call_id)` onward (keep the parse `try/except` above it unchanged) so the full method reads:

```python
    async def run(self, script: str, args: object, tool_call_id: str) -> str:
        try:
            monty = Monty(script, inputs=["args"], script_name="workflow.py")
        except MontySyntaxError as exc:
            return f"Workflow script failed to parse: {exc}"
        # Announce the run only after a successful parse: a parse failure is
        # returned above with no run to track, so no card is ever claimed for
        # it and _announce_done below fires exactly once per announced run.
        self._announce_start(tool_call_id, _script_title(script))
        state = _RunState(tool_call_id=tool_call_id)
        vm_limits: ResourceLimits = {
            # Never let the VM's own duration cap exceed the caller's
            # configured timeout_secs — otherwise a non-yielding compute
            # loop could outlast the SLA a short custom timeout implies
            # (the outer asyncio.wait_for below can't preempt one; see the
            # module docstring).
            "max_duration_secs": min(self._timeout, _MAX_VM_DURATION_SECS),
            "max_memory": _VM_MEMORY_LIMIT_BYTES,
        }
        prints = _PrintTail()
        vm = asyncio.ensure_future(
            monty.run_async(
                inputs={"args": args},
                limits=vm_limits,
                external_functions=self._host_table(state),
                print_callback=prints.append,
            )
        )
        # asyncio.wait (not wait_for + shield): both leave the VM task
        # uncancelled on timeout/cancel, but on Python 3.14 an abandoned
        # shield attaches a callback that reports the task's eventual
        # exception to the loop's exception handler even after it has been
        # retrieved -- and the deliberate wind-down below ENDS with the VM
        # raising (WorkflowCancelled surfaces as MontyRuntimeError), so every
        # abort would log a spurious "exception in shielded future".
        try:
            done, _ = await asyncio.wait({vm}, timeout=self._timeout)
        except asyncio.CancelledError:
            # The turn was aborted. Wind the VM down through its host
            # functions (never a direct cancel — see module docstring), then
            # let the cancellation propagate so the turn's resumability
            # invariants hold.
            await self._abort_and_drain(state, vm)
            self._announce_done(tool_call_id, "workflow aborted", failed=True)
            raise
        if not done:
            await self._abort_and_drain(state, vm)
            outcome = (f"Workflow timed out after {self._timeout:.0f}s; "
                       "in-flight sub-agents were cancelled.")
            self._announce_done(tool_call_id, outcome, failed=True)
            return outcome
        try:
            value = vm.result()
        except MontyRuntimeError as exc:
            outcome = f"Workflow script raised: {exc}"
            self._announce_done(tool_call_id, outcome, failed=True)
            return outcome
        shaped = self._shape(value, tool_call_id, prints.text())
        self._announce_done(tool_call_id, shaped, failed=False)
        return shaped
```

Add the two announce helpers directly after `_log` (same guarded-getattr posture as every workflow hook):

```python
    def _announce_start(self, tool_call_id: str, title: str) -> None:
        cb = getattr(self.deps.ui, "on_workflow_start", None)
        if cb is not None:
            cb(tool_call_id, title)

    def _announce_done(self, tool_call_id: str, outcome: str, *, failed: bool) -> None:
        cb = getattr(self.deps.ui, "on_workflow_done", None)
        if cb is not None:
            cb(tool_call_id, outcome, failed)
```

Re-sign `_log` to carry the id, and update its one call site in `_host_table`:

```python
    def _log(self, tool_call_id: str, message: str) -> None:
        logger.debug("workflow log: %s", message)
        cb = getattr(self.deps.ui, "on_workflow_log", None)
        if cb is not None:
            cb(tool_call_id, message)
```

In `_host_table`, replace:

```python
        def log(message):
            self._log(str(message))
```

with:

```python
        def log(message):
            self._log(state.tool_call_id, str(message))
```

- [ ] **Step 5: Thread the callbacks in `Harness.bind_ui` (`runtime/harness.py`)**

In the `bind_ui` signature, replace:

```python
        on_workflow_log: Callable[[str], None] | None = None,
```

with:

```python
        on_workflow_log: Callable[[str, str], None] | None = None,
```

and after the `on_workflow_spawn_done` parameter line, add:

```python
        on_workflow_start: Callable[[str, str], None] | None = None,
        on_workflow_done: Callable[[str, str, bool], None] | None = None,
```

In the body, after `self.deps.ui.on_workflow_spawn_done = on_workflow_spawn_done`, add:

```python
        self.deps.ui.on_workflow_start = on_workflow_start
        self.deps.ui.on_workflow_done = on_workflow_done
```

- [ ] **Step 6: Adapt the TUI's log lambda and its tests (keeps the suite green; Task 3 replaces this wiring)**

In `src/marim_harness/interfaces/tui/app.py`, replace:

```python
            on_workflow_log=lambda msg: self.notify(
                rich.markup.escape(msg), title="workflow", timeout=4
            ),
```

with:

```python
            on_workflow_log=lambda _tcid, msg: self.notify(
                rich.markup.escape(msg), title="workflow", timeout=4
            ),
```

In `tests/test_app.py::test_bind_ui_wires_workflow_spawn_and_log_callbacks`, update the two single-arg calls:

- `ui.on_workflow_log("step 1 done")` → `ui.on_workflow_log("tc1", "step 1 done")`
- `ui.on_workflow_log("processing [bold red]injected[/bold red] file")` → `ui.on_workflow_log("tc1", "processing [bold red]injected[/bold red] file")`

(The assertions on the notified text stay unchanged.)

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py tests/test_workflow_wiring.py tests/test_app.py tests/test_workflow_acceptance.py tests/test_workflow_tool.py -v`
Expected: all PASS.

- [ ] **Step 8: Lint, type-check, commit**

```bash
uv run ruff check src tests
uv run pyright
git add src/marim_harness/runtime/deps.py src/marim_harness/workflows/engine.py \
        src/marim_harness/runtime/harness.py src/marim_harness/interfaces/tui/app.py \
        tests/test_workflow_engine.py tests/test_workflow_wiring.py tests/test_app.py
git commit -m "feat(workflows): engine announces the run lifecycle (on_workflow_start/done, keyed log)

Two new optional UIHooks mirroring the spawn pair: on_workflow_start fires
after a successful parse with a derived title (first comment line, else a
line count); on_workflow_done fires exactly once on every exit — success,
script raise, timeout, and the cancellation path before re-raising — with an
explicit failed flag so the UI never sniffs result text. on_workflow_log now
carries the run's tool_call_id so the TUI can route lines to the run's card.
Headless is unchanged: all three stay None and every call site guards.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 3: Workflow card in the sub-agents screen + tree grouping + persisted logs

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (`workflow_cards` dict; `claim_workflow_card` / `append_workflow_log` / `finish_workflow_card`; `parent_id` flip in `claim_workflow_spawn`)
- Modify: `src/marim_harness/interfaces/tui/subagents/pane.py` (`SubAgentPane.append_log`)
- Modify: `src/marim_harness/interfaces/tui/app.py` (wire the new hooks; `_on_workflow_log` method)
- Modify: `src/marim_harness/interfaces/tui/styles.tcss` (`.workflow-log` rule)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes (from Task 2, exact signatures): `UIHooks.on_workflow_start(tool_call_id: str, title: str)`, `UIHooks.on_workflow_done(tool_call_id: str, outcome: str, failed: bool)`, `UIHooks.on_workflow_log(tool_call_id: str, message: str)`, `Harness.bind_ui(..., on_workflow_start=..., on_workflow_done=...)`.
- Produces: `StreamRenderer.workflow_cards: dict[str, SubAgentWidget]`, `StreamRenderer.claim_workflow_card(tool_call_id, title)`, `StreamRenderer.append_workflow_log(tool_call_id, message)`, `StreamRenderer.finish_workflow_card(tool_call_id, outcome, failed)`, `SubAgentPane.append_log(message)` — all sync. No later task consumes these.

- [ ] **Step 1: Write the failing TUI test**

Append to `tests/test_app.py` (uses the module's existing `_app(tmp_path)` helper and imports):

```python
@pytest.mark.anyio
async def test_workflow_card_lifecycle_and_child_nesting(tmp_path: Path):
    """on_workflow_start claims a first-class card for the run in the
    sub-agents list — unmounted, because the run_workflow ToolCallWidget owns
    the transcript slot and the tool_widgets[tool_call_id] key. Children nest
    under it via parent_id, log() lines persist into its pane, and
    on_workflow_done settles it with the engine's explicit failed flag."""
    from marim_harness.interfaces.tui.subagents.stats import tree_order

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        ui = app.harness.deps.ui
        assert ui.on_workflow_start is not None
        assert ui.on_workflow_done is not None

        ui.on_workflow_start("tc1", "review sweep")
        card = app.stream.workflow_cards["tc1"]
        assert card in app.stream.subagents
        assert card.stream_id == "tc1" and card.status == "pending"
        # The run_workflow tool call owns the tool_widgets slot; the card
        # must not collide with it (and is never mounted in the transcript).
        assert app.stream.tool_widgets.get("tc1") is not card
        assert card.parent is None

        await ui.on_workflow_spawn("tc1::wf1", "explore", "review bugs", "tc1")
        await pilot.pause()
        child = app.stream.tool_widgets["tc1::wf1"]
        assert child.parent_id == "tc1"
        assert [row.agent for row in tree_order(app.stream.subagents)] == [card, child]

        ui.on_workflow_log("tc1", "step 1 done")
        await pilot.pause()
        assert card.pane is not None
        assert len(card.pane.query(".workflow-log")) == 1

        ui.on_workflow_done("tc1", '{"findings": []}', False)
        assert card.status == "done"
        assert card.report == '{"findings": []}'
```

If `tests/test_app.py` does not already import `Path` from `pathlib` at the top, it does (existing tests use `tmp_path: Path`) — do not add a duplicate.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest --no-cov tests/test_app.py::test_workflow_card_lifecycle_and_child_nesting -v`
Expected: FAIL — `assert ui.on_workflow_start is not None` (bind_ui not wired by the app yet).

- [ ] **Step 3: Add `SubAgentPane.append_log`**

In `src/marim_harness/interfaces/tui/subagents/pane.py`, directly after `append_error`:

```python
    def append_log(self, message: str) -> None:
        """A workflow script's log() progress line — kept in the run's
        transcript so it outlives the transient toast the app also raises."""
        self.mount(Static(Content(message), classes="workflow-log"))
```

(`Static` and `Content` are already imported at the top of pane.py.)

- [ ] **Step 4: Add the renderer state and methods in `stream_render.py`**

In `StreamRenderer.__init__`, directly after the line `self.tool_widgets: dict[str, ToolCallWidget | SubAgentWidget] = {}` (line ~418), add:

```python
        # Workflow RUN cards, keyed by the run_workflow tool_call_id. A
        # separate map from tool_widgets: that key is already taken by the
        # run_workflow ToolCallWidget itself, which _on_tool_result must keep
        # settling normally — registering the card there would clobber it.
        self.workflow_cards: dict[str, SubAgentWidget] = {}
```

Directly before `claim_workflow_spawn`, add the three methods:

```python
    def claim_workflow_card(self, tool_call_id: str, title: str) -> None:
        """A first-class card for the workflow RUN itself, claimed when the
        engine announces a parsed script (on_workflow_start). It registers in
        the ordered ``subagents`` list — so the sub-agents screen shows the
        run and ``tree_order`` nests its children under it (their parent_id
        is this tool_call_id) — and gets a pane so log() lines have a
        transcript to land in. It is NOT mounted into the main transcript:
        the run_workflow tool widget already represents the run there. An
        unmounted card is safe — its header/activity Statics exist from
        __init__ and updating an unmounted Static just stores content."""
        widget = SubAgentWidget("workflow", title, str(self.app.harness.model_label or ""))
        widget.stream_id = tool_call_id
        self.subagents.append(widget)
        self.workflow_cards[tool_call_id] = widget
        self.ensure_pane(widget)
        self.app.subagents.refresh()

    def append_workflow_log(self, tool_call_id: str, message: str) -> None:
        """Persist a script's log() line into the run card's pane (the toast
        the app also raises is transient). Unknown ids are dropped — the same
        tolerance every optional UI callback has."""
        widget = self.workflow_cards.get(tool_call_id)
        if widget is None:
            return
        pane = self.ensure_pane(widget)
        if pane is not None:
            pane.append_log(message)

    def finish_workflow_card(self, tool_call_id: str, outcome: str, failed: bool) -> None:
        """Settle the run's card. The engine fires on_workflow_done on EVERY
        exit path (success, raise, timeout, cancel) with an explicit failed
        flag, so — unlike finish_workflow_child — there is no report-text
        sniffing here: the engine knows which exit it took."""
        widget = self.workflow_cards.get(tool_call_id)
        if widget is None:
            return
        widget.finish(outcome, status="failed" if failed else "done")
        self.app.subagents.refresh()
```

- [ ] **Step 5: Flip `parent_id` in `claim_workflow_spawn`**

Still in `stream_render.py`, in `claim_workflow_spawn`, replace:

```python
        widget.stream_id = stream_id
        widget.parent_id = None
```

with:

```python
        widget.stream_id = stream_id
        # Nest under the workflow run's card in the sub-agents screen:
        # claim_workflow_card registered that card with stream_id ==
        # parent_id (the run_workflow tool_call_id), so tree_order picks the
        # pair up. If the card is absent (headless replay edge), tree_order's
        # unknown-parent fallback renders the child at root — never lost.
        widget.parent_id = parent_id
```

and in the method's docstring, replace the final sentence

```
``parent_id`` (the run_workflow tool_call_id) is accepted for future tree
grouping but not yet used for nesting.
```

with:

```
``parent_id`` (the run_workflow tool_call_id) nests the card under the
workflow run's own card in the sub-agents screen (see claim_workflow_card).
```

- [ ] **Step 6: Wire the app (`interfaces/tui/app.py`)**

In the `bind_ui(...)` call, replace:

```python
            on_workflow_spawn=self._on_workflow_spawn,
            on_workflow_log=lambda _tcid, msg: self.notify(
                rich.markup.escape(msg), title="workflow", timeout=4
            ),
            on_workflow_spawn_done=self.stream.finish_workflow_child,
```

with:

```python
            on_workflow_spawn=self._on_workflow_spawn,
            on_workflow_start=self.stream.claim_workflow_card,
            on_workflow_log=self._on_workflow_log,
            on_workflow_done=self.stream.finish_workflow_card,
            on_workflow_spawn_done=self.stream.finish_workflow_child,
```

Directly after the `_on_workflow_spawn` method, add:

```python
    def _on_workflow_log(self, tool_call_id: str, message: str) -> None:
        """Route a workflow script's log() line: persist it into the run
        card's pane (so it survives past the toast) and raise the transient
        toast. Fired on the app's event loop by the engine, so direct
        renderer mutation is safe — same as _on_workflow_spawn."""
        self.stream.append_workflow_log(tool_call_id, message)
        self.notify(rich.markup.escape(message), title="workflow", timeout=4)
```

- [ ] **Step 7: Style the log line**

In `src/marim_harness/interfaces/tui/styles.tcss`, next to the existing `.subagent-error` rule (search for it; if absent, next to `.notice-msg`), add:

```tcss
.workflow-log { color: $text-muted; text-style: italic; margin: 0 0 0 2; }
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_app.py tests/test_subagents_screen.py -v`
Expected: all PASS (including the two existing workflow wiring tests updated in Task 2 — `_on_workflow_log` with an unclaimed "tc1" drops the pane write and still toasts, so their notify assertions hold).

- [ ] **Step 9: Lint, type-check, commit**

```bash
uv run ruff check src tests
uv run pyright
git add src/marim_harness/interfaces/tui/stream_render.py \
        src/marim_harness/interfaces/tui/subagents/pane.py \
        src/marim_harness/interfaces/tui/app.py \
        src/marim_harness/interfaces/tui/styles.tcss \
        tests/test_app.py
git commit -m "feat(tui): first-class workflow card — tree grouping and persisted log() lines

on_workflow_start claims an unmounted SubAgentWidget for the run, keyed in a
new workflow_cards map (tool_widgets[tool_call_id] belongs to the
run_workflow ToolCallWidget and must keep settling). Children now set
parent_id so tree_order nests them under the run in the sub-agents screen;
log() lines land in the card's pane (durable) plus the existing toast; and
on_workflow_done settles the card via the engine's explicit failed flag.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 4: Core output-schema module + runner StructuredDict support

**Files:**
- Create: `src/marim_harness/subagents/output_schema.py`
- Modify: `src/marim_harness/subagents/runner.py` (`run`/`_execute_spawn`/`_prepare_spawn`/`build` thread `output_schema`; dict serialization in `_execute_native_spawn`)
- Modify: `src/marim_harness/workflows/schema.py` (drop `output_contract` — it moves to core)
- Modify: `src/marim_harness/workflows/engine.py` (import `output_contract` from its new home — usage unchanged until Task 5)
- Modify: `src/marim_harness/runtime/deps.py` (widen the `SubAgent` alias)
- Modify: `src/marim_harness/subagents/run_driver.py` (widen `run_to_completion`'s return annotation)
- Test: Create `tests/test_subagent_output_schema.py`; modify `tests/test_subagent_cli_spawn.py`, `tests/test_workflow_schema.py`

**Interfaces:**
- Consumes: nothing from other tasks (independent of Tasks 2–3).
- Produces (Task 5 relies on these exact signatures):
  - `SubagentRunner.run(self, type, task, stream_id, mcp_names=None, max_output_chars=None, model=None, isolation=None, caller_depth=0, output_schema: dict | None = None) -> str` — `output_schema` is keyword-position 9, pass it BY KEYWORD.
  - `marim_harness.subagents.output_schema.output_contract(schema: dict) -> str` — same text the workflows module produced (starts `"\n\nOutput contract: respond with ONLY a JSON object..."`).
  - `marim_harness.subagents.output_schema.resolve_output_schema(schema: dict | None, backend: str | None) -> tuple[dict | None, str]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_subagent_output_schema.py`:

```python
"""Schema'd spawn output: the native StructuredDict path vs the
prompt-contract fallback (claude-cli backends, non-object schema roots)."""

import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.subagents.output_schema import output_contract, resolve_output_schema
from tests.conftest import _make_deps, _make_harness

FINDINGS = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"],
}


def test_output_contract_embeds_the_schema_and_demands_bare_json():
    text = output_contract(FINDINGS)
    assert "ONLY a JSON object" in text
    assert '"findings"' in text


def test_resolve_passes_object_schemas_to_structured_output():
    assert resolve_output_schema(FINDINGS, None) == (FINDINGS, "")


def test_resolve_falls_back_for_the_cli_backend():
    schema, contract = resolve_output_schema(FINDINGS, "claude-cli")
    assert schema is None
    assert "Output contract" in contract and '"findings"' in contract


def test_resolve_falls_back_for_non_object_roots():
    array_root = {"type": "array", "items": {"type": "string"}}
    schema, contract = resolve_output_schema(array_root, None)
    assert schema is None
    assert "Output contract" in contract


def test_resolve_no_schema_is_a_no_op():
    assert resolve_output_schema(None, "claude-cli") == (None, "")
    assert resolve_output_schema(None, None) == (None, "")


@pytest.mark.anyio
async def test_object_schema_rides_structured_output(tmp_path: Path):
    h = _make_harness(
        TestModel(call_tools=[], custom_output_args={"findings": ["bug in x"]}),
        _make_deps(tmp_path),
    )
    out = await h.subagents.run("explore", "review", "s1", output_schema=FINDINGS)
    assert json.loads(out) == {"findings": ["bug in x"]}


@pytest.mark.anyio
async def test_non_object_schema_falls_back_to_prompt_contract(tmp_path: Path):
    seen = {}

    def fn(messages, info):
        seen["prompt"] = messages[0].parts[-1].content
        return ModelResponse(parts=[TextPart(content="plain text")])

    h = _make_harness(FunctionModel(fn), _make_deps(tmp_path))
    out = await h.subagents.run(
        "explore", "list things", "s1",
        output_schema={"type": "array", "items": {"type": "string"}},
    )
    assert "Output contract" in seen["prompt"]
    assert "plain text" in out


@pytest.mark.anyio
async def test_no_schema_leaves_the_spawn_unchanged(tmp_path: Path):
    seen = {}

    def fn(messages, info):
        seen["prompt"] = messages[0].parts[-1].content
        return ModelResponse(parts=[TextPart(content="ok")])

    h = _make_harness(FunctionModel(fn), _make_deps(tmp_path))
    out = await h.subagents.run("explore", "just look", "s1")
    assert "Output contract" not in seen["prompt"]
    assert "ok" in out
```

Append to `tests/test_subagent_cli_spawn.py` (reuses its `_fake_cli`, `_write_cli_agent`, `_dummy_model` helpers):

```python
@pytest.mark.anyio
async def test_cli_backend_schema_appends_prompt_contract(tmp_path: Path, monkeypatch):
    """A claude-cli spawn is an external process marim only launches — it
    can't take a pydantic-ai output type, so the runner (which knows the
    backend) appends the prompt contract to the task instead."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _write_cli_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    seen = {}

    async def fake_execute(defn, task, *args, **kwargs):
        seen["task"] = task
        return "ok"

    monkeypatch.setattr(runner._cli, "execute", fake_execute)
    out = await runner.run(
        "cli-worker", "do the thing", "s1",
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )
    assert out == "ok"
    assert seen["task"].startswith("do the thing")
    assert "Output contract" in seen["task"]
```

In `tests/test_workflow_schema.py`: delete `test_output_contract_embeds_the_schema_and_demands_bare_json` (it moved to the new file) and remove `output_contract` from the `from marim_harness.workflows.schema import (...)` list.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_output_schema.py tests/test_subagent_cli_spawn.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.subagents.output_schema'`.

- [ ] **Step 3: Create `src/marim_harness/subagents/output_schema.py`**

```python
"""How a schema'd spawn enforces its output schema.

The native path uses pydantic-ai structured output: SubagentRunner.build
gives the child ``output_type=StructuredDict(schema)``, so a mismatch is
retried IN-RUN (the model re-emits just the final output) instead of costing
the caller a full re-spawn. Two spawn shapes can't take an output type and
fall back to a contract paragraph appended to the task text: the claude-cli
backend (an external process marim only launches) and schemas without an
object root (StructuredDict requires one). ``resolve_output_schema`` is that
decision, made once by the runner — the component that knows the backend.

Lives under ``subagents/`` rather than ``workflows/schema.py`` because the
runner is core and this module must not pull in jsonschema (a [workflows]
extra); report *validation* stays in the workflows package."""

from __future__ import annotations

import json


def output_contract(schema: dict) -> str:
    """The output-contract paragraph appended to a schema'd task: the
    sub-agent must respond with ONLY a JSON object matching the schema."""
    return (
        "\n\nOutput contract: respond with ONLY a JSON object matching this "
        "JSON Schema — no prose before or after it:\n"
        + json.dumps(schema, indent=2)
    )


def resolve_output_schema(
    schema: dict | None, backend: str | None
) -> tuple[dict | None, str]:
    """Decide the enforcement path for a spawn's output schema. Returns
    ``(schema, "")`` when the spawn can ride structured output (native
    backend, object-rooted schema), or ``(None, contract)`` for the prompt
    fallback. Pure; unit-tested directly."""
    if schema is None:
        return None, ""
    if backend == "claude-cli" or schema.get("type") != "object":
        return None, output_contract(schema)
    return schema, ""
```

- [ ] **Step 4: Move `output_contract` out of the workflows package**

In `src/marim_harness/workflows/schema.py`: delete the `output_contract` function (the module keeps `extract_json`, `check_valid_schema`, `validate_report`, `shape_result`). Update the module docstring's first line from "schema output contracts, report validation, and result shaping" to "report validation and result shaping" (the contract now lives in `subagents/output_schema.py`).

In `src/marim_harness/workflows/engine.py`, replace the import line:

```python
from .schema import check_valid_schema, output_contract, shape_result, validate_report
```

with:

```python
from ..subagents.output_schema import output_contract
from .schema import check_valid_schema, shape_result, validate_report
```

(Engine behavior is unchanged in this task; Task 5 removes the `output_contract` usage entirely.)

- [ ] **Step 5: Thread `output_schema` through the runner (`subagents/runner.py`)**

Imports: add `import json` to the stdlib block (after `import contextlib`); change `from pydantic_ai import Agent` to `from pydantic_ai import Agent, StructuredDict`; add `from .output_schema import resolve_output_schema` next to the other `.`-relative imports (e.g. after `from .isolation import SpawnWorktree`, keeping ruff's import sorting happy).

**`run`** — add the parameter and pass it down. New signature:

```python
    async def run(
        self, type: str, task: str, stream_id: str,
        mcp_names: list[str] | None = None, max_output_chars: int | None = None,
        model: str | None = None, isolation: str | None = None,
        caller_depth: int = 0, output_schema: dict | None = None,
    ) -> str:
```

Append to its docstring (before the closing `"""`):

```
        ``output_schema`` is an optional JSON Schema the spawn's final report
        must satisfy. Object-rooted schemas on native spawns are enforced with
        pydantic-ai structured output (mismatches retry in-run); claude-cli
        spawns and non-object roots fall back to a contract paragraph appended
        to the task. The report is always returned as str — a structured
        result is JSON-serialized.
```

And in its body, pass the keyword through:

```python
        return await self._execute_spawn(
            type, task, mcp_names, max_output_chars, model, isolation,
            background=False, stream_id=stream_id, caller_depth=caller_depth,
            output_schema=output_schema,
        )
```

(`run_background` is deliberately unchanged — nothing schemas a background spawn today; YAGNI.)

**`_execute_spawn`** — add `output_schema: dict | None = None,` after `caller_depth: int = 0,` in the keyword-only section of the signature. Then, replace:

```python
        defn = self._resolve_agent(type)
        depth = caller_depth + 1
```

with:

```python
        defn = self._resolve_agent(type)
        depth = caller_depth + 1
        # Decide the schema enforcement path ONCE, where the backend is
        # known: object-rooted schemas on native spawns ride structured
        # output (build() below sets output_type); the claude-cli backend
        # and non-object roots get the prompt contract appended to the task
        # instead — see subagents/output_schema.py.
        output_schema, contract = resolve_output_schema(
            output_schema, defn.backend if defn is not None else None
        )
        task = task + contract
```

and pass the schema into the prepare call:

```python
        prep = await self._prepare_spawn(
            type, task, mcp_names, max_output_chars, model,
            iso, work_root, stream_id, debug=debug, t0=t0, defn=defn, depth=depth,
            output_schema=output_schema,
        )
```

(The cli early-return between those two edits is untouched — its `task` already carries the appended contract, and `output_schema` is `None` on that path.)

**`_prepare_spawn`** — add `output_schema: dict | None = None,` to the keyword-only section of its signature (after `resumed: bool = False,`), and thread it into the build call:

```python
        sub, err = self.build(type, max_output_chars, model, work_root, defn=defn,
                              depth=depth, mask_trigger=mask_trigger,
                              checkpoint=checkpoint, output_schema=output_schema)
```

**`build`** — add `output_schema: dict | None = None,` to the keyword-only section of its signature (after `checkpoint: Callable[[list], None] | None = None,`). Append to its docstring, before the "Returns" sentence: ``` ``output_schema``, when set (already resolved by the caller to an object-rooted schema), makes the sub-agent's output structured: its ``output_type`` becomes ``StructuredDict(output_schema)``. ``` Then change the Agent construction from:

```python
        sub = Agent(
            model_obj,
            deps_type=Deps,
            instructions=subagent_instructions(
```

to:

```python
        sub = Agent(
            model_obj,
            deps_type=Deps,
            # Schema'd spawns enforce their output natively: StructuredDict
            # validates the final output against the schema and retries
            # IN-RUN on mismatch, instead of the caller re-running the whole
            # spawn. Everything downstream still sees str —
            # _execute_native_spawn serializes a dict result.
            output_type=StructuredDict(output_schema) if output_schema else str,
            instructions=subagent_instructions(
```

**`_execute_native_spawn`** — in the inner `_run`, change:

```python
            return SpawnRun(
                output=result.output,
```

to:

```python
            out = result.output
            return SpawnRun(
                # A schema'd spawn (output_type=StructuredDict) finishes with
                # a dict; every seam above returns str, so serialize HERE —
                # the caller gets the same JSON text a prompt-contracted
                # spawn would produce, minus the extraction guesswork.
                output=out if isinstance(out, str) else json.dumps(out),
```

- [ ] **Step 6: Widen the output types for pyright**

In `src/marim_harness/runtime/deps.py`, replace line 259:

```python
SubAgent = Agent[Deps, str]
```

with:

```python
# str for ordinary spawns; a schema'd spawn (output_type=StructuredDict)
# finishes with a dict, which the runner serializes back to str before it
# crosses any seam (SpawnRun.output stays textual).
SubAgent = Agent[Deps, str | dict[str, Any]]
```

Ensure `Any` is in deps.py's `typing` import (add it to the existing `from typing import ...` line if absent).

In `src/marim_harness/subagents/run_driver.py`, change `run_to_completion`'s return annotation from `AgentRunResult[str]` to `AgentRunResult[str | dict[str, Any]]` (`Any` is already imported there — it types the `granted: list[Any]` parameter).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagent_output_schema.py tests/test_subagent_cli_spawn.py tests/test_workflow_schema.py tests/test_workflow_engine.py tests/test_agent_subagents.py -v`
Expected: all PASS (workflow engine tests still pass — the engine still appends `output_contract` itself until Task 5, and the double-append cannot happen because the engine never passes `output_schema`).

- [ ] **Step 8: Lint, type-check, commit**

```bash
uv run ruff check src tests
uv run pyright
git add src/marim_harness/subagents/output_schema.py src/marim_harness/subagents/runner.py \
        src/marim_harness/subagents/run_driver.py src/marim_harness/workflows/schema.py \
        src/marim_harness/workflows/engine.py src/marim_harness/runtime/deps.py \
        tests/test_subagent_output_schema.py tests/test_subagent_cli_spawn.py \
        tests/test_workflow_schema.py
git commit -m "feat(subagents): native structured output for schema'd spawns (output_schema seam)

SubagentRunner.run gains output_schema: object-rooted schemas make the child
agent's output_type StructuredDict(schema), so a validation mismatch retries
in-run instead of costing a full re-spawn; the dict result is JSON-serialized
in _execute_native_spawn so every seam above stays str. claude-cli backends
(external process, no output type) and non-object roots fall back to the
prompt contract, decided once in _execute_spawn where the backend is known.
output_contract moves to core subagents/output_schema.py — the runner must
not import jsonschema (a [workflows] extra); validation stays in workflows/.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 5: Engine rides the `output_schema` seam

**Files:**
- Modify: `src/marim_harness/workflows/engine.py` (`_agent_call` passes the schema through `_spawn_child`; contract appends removed)
- Test: `tests/test_workflow_engine.py`, `tests/test_workflow_acceptance.py` (fake spawns grow the keyword; contract assertions flip)

**Interfaces:**
- Consumes (from Task 4): `self._spawn(...)` is `SubagentRunner.run` — accepts `output_schema: dict | None = None` as a keyword after 8 positionals `(type, task, stream_id, mcp_names, max_output_chars, model, isolation, caller_depth)`.
- Produces: nothing later tasks rely on. The workflow-script surface (`agent(task, schema=...)` returning validated data) is unchanged.

- [ ] **Step 1: Update the fakes and write the failing tests**

In `tests/test_workflow_engine.py`, every fake spawn must tolerate the new keyword. Add `**kw` to each `async def spawn(...)` / `_echo_spawn` signature in the file — e.g. `async def spawn(type, task, *rest):` becomes `async def spawn(type, task, *rest, **kw):`, and `_echo_spawn(type, task, stream_id, mcp_names, max_output_chars, model, isolation, caller_depth)` becomes `_echo_spawn(type, task, stream_id, mcp_names, max_output_chars, model, isolation, caller_depth, **kw)`. (Grep the file for `async def spawn` — there are ~9 — plus `_echo_spawn`; slow/plain fakes already written as `async def slow_spawn(*a, **kw)` in Task 2 need nothing.)

In `tests/test_workflow_acceptance.py`, the end-to-end fake changes from asserting the contract in the prompt to asserting the schema rides the seam. Replace:

```python
    async def spawn(type, task, stream_id, mcp, cap, model, iso, depth):
        assert type == "explore" and "Output contract" in task
```

with:

```python
    async def spawn(type, task, stream_id, mcp, cap, model, iso, depth, *, output_schema=None):
        assert type == "explore" and output_schema is not None
        assert "Output contract" not in task
```

In `tests/test_workflow_engine.py::test_schema_valid_report_returns_a_dict_into_the_script`, replace the fake's contract assertion the same way:

```python
    async def spawn(type, task, *rest, output_schema=None):
        assert output_schema is not None
        assert "Output contract" not in task
        return '{"findings": ["bug in x"]}'
```

Then append the new engine tests to `tests/test_workflow_engine.py`:

```python
@pytest.mark.anyio
async def test_schema_rides_the_spawn_seam_not_the_prompt(tmp_path):
    seen = {}

    async def spawn(type, task, stream_id, *rest, output_schema=None):
        seen["task"], seen["output_schema"] = task, output_schema
        return '{"findings": []}'

    eng, _ = _engine(tmp_path, spawn)
    out = await eng.run(SCHEMA_SCRIPT, None, "tc1")
    assert out == "[]"
    assert seen["output_schema"] == FINDINGS
    assert "Output contract" not in seen["task"]


@pytest.mark.anyio
async def test_schema_retry_keeps_the_schema_on_the_seam(tmp_path):
    calls = []

    async def spawn(type, task, *rest, output_schema=None):
        calls.append((task, output_schema))
        if len(calls) == 1:
            return "not json at all"
        return '{"findings": []}'

    eng, _ = _engine(tmp_path, spawn)
    out = await eng.run(SCHEMA_SCRIPT, None, "tc1")
    assert out == "[]"
    assert len(calls) == 2
    retry_task, retry_schema = calls[1]
    assert "failed validation" in retry_task
    assert "Output contract" not in retry_task
    assert retry_schema == FINDINGS


@pytest.mark.anyio
async def test_unschemad_agent_calls_pass_no_schema(tmp_path):
    seen = {}

    async def spawn(type, task, *rest, output_schema=None):
        seen["output_schema"] = output_schema
        return "report"

    eng, _ = _engine(tmp_path, spawn)
    await eng.run('await agent("look around")', None, "tc1")
    assert seen["output_schema"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py tests/test_workflow_acceptance.py -v`
Expected: the new/updated schema tests FAIL (the engine still appends the contract to the task and passes no `output_schema` keyword — the fakes see `output_schema=None` and `"Output contract" in task`).

- [ ] **Step 3: Rewire `_agent_call` / `_spawn_child` in `workflows/engine.py`**

Remove the import added in Task 4 — delete the line:

```python
from ..subagents.output_schema import output_contract
```

Replace `_agent_call` with:

```python
    async def _agent_call(self, state: _RunState, task: str, *, type: str,
                          model, schema, max_output_chars, isolation):
        if schema is not None:
            check_valid_schema(schema)
        # Enforcement rides the spawn seam (runner-side structured output,
        # with the prompt contract as the runner's own claude-cli/non-object
        # fallback); validate_report below stays as defense in depth — on the
        # native path the JSON round-trips trivially, on the fallback path it
        # does exactly the work it did when the engine owned the contract.
        report = await self._spawn_child(
            state, type, task, max_output_chars, model, isolation, schema,
        )
        if schema is None:
            return report
        data, err = validate_report(report, schema)
        for _ in range(_SCHEMA_RETRIES):
            if err is None:
                return data
            retry_task = (
                task
                + f"\n\nA previous attempt failed validation: {err}. "
                  "Respond again with ONLY the corrected JSON."
            )
            report = await self._spawn_child(
                state, type, retry_task, max_output_chars, model, isolation, schema,
            )
            data, err = validate_report(report, schema)
        if err is None:
            return data
        raise WorkflowResultError(
            f"agent() output failed schema validation after a retry: {err}"
        )
```

In `_spawn_child`, add the parameter and forward it. New signature:

```python
    async def _spawn_child(self, state: _RunState, type: str, task: str,
                           max_output_chars, model, isolation,
                           output_schema: dict | None = None) -> str:
```

and change the spawn call to pass it by keyword (the 8 positionals are unchanged):

```python
        child = asyncio.ensure_future(self._spawn(
            type, task, stream_id, None, max_output_chars, model, isolation,
            self.deps.subagent_depth, output_schema=output_schema,
        ))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py tests/test_workflow_acceptance.py tests/test_workflow_tool.py tests/test_workflow_schema.py tests/test_subagent_output_schema.py -v`
Expected: all PASS.

- [ ] **Step 5: Full CI gate and commit**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
git add src/marim_harness/workflows/engine.py tests/test_workflow_engine.py \
        tests/test_workflow_acceptance.py
git commit -m "feat(workflows): schema'd agent() calls ride the output_schema spawn seam

The engine stops baking output_contract into the task text and passes
output_schema through _spawn_child to SubagentRunner.run, which enforces it
natively (StructuredDict) or falls back to the prompt contract for
claude-cli/non-object cases — chosen by the component that knows the
backend. validate_report plus the single re-spawn retry stay as defense in
depth; the retry prompt carries the validation error but no contract (the
seam re-applies enforcement on the retry spawn too).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

## Verification (after all tasks)

Run the full CI order on Python 3.10 (default venv) — ruff, pyright, pytest — as each task already does. If you want the 3.14 leg locally (the recent asyncio.shield regression makes it worth a spot check for engine changes):

```bash
UV_PROJECT_ENVIRONMENT=/tmp/claude-1000/-home-mateuscmarim-Projects-marim-dev-marim-harness/7590b271-7047-48d5-a645-0cba58e3d312/scratchpad/venv314 \
  uv run --python 3.14 pytest --no-cov tests/test_workflow_engine.py tests/test_workflow_acceptance.py
```

Do NOT run any paid model. Live/e2e verification is out of scope for this plan; unit tests use TestModel/FunctionModel only.
