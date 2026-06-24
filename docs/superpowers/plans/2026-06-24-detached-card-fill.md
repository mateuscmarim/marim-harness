# Fill-on-Finish Detached Sub-Agent Cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a detached sub-agent's background job finishes, fill its existing live `SubAgentWidget` card with the real report and final status, instead of leaving it showing the handoff note.

**Architecture:** An auto-detached spawn already builds a card in `intercept_tool`; today the result handler finishes it with the handoff note. Instead, detect the handoff, keep the card pending, map `job_id → card`, and fill it when the job settles — driven by the existing `JobRegistry.on_change → app._on_jobs_changed` hook. No server-side changes, no streaming, no resume.

**Tech Stack:** Python 3.10+, Textual, pytest. Spec: `docs/superpowers/specs/2026-06-24-detached-card-fill-design.md`.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright src`. Never bare `python`/`pip`/`pytest`.
- Ruff line length 100; lint set `E,F,I` (import sorting enforced).
- `requires-python >=3.10` — no 3.11+-only syntax.
- CI order (match before claiming done): ruff → pyright → pytest. Coverage on by default; `--no-cov` only for fast single-test runs.
- pyright runs on `src` only; test-file type looseness is not gated by CI.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File Structure

- `src/marim_harness/interfaces/tui/stream_render.py` — `_detached_job_id` parser, the `_detached_cards` map, `note_detached_spawn`, `_fill_detached_card`, `fill_finished_detached_cards`, and the result-handler integration (Tasks 1, 2).
- `src/marim_harness/interfaces/tui/app.py` — call the fill from `_on_jobs_changed` (Task 3).
- Tests: `tests/test_app.py` (extends the existing `on_events` + `JobRegistry` pattern).

---

### Task 1: Handoff parser + the job_id→card map

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (add `_detached_job_id`; add `self._detached_cards` to `StreamRenderer.__init__` ~line 276; clear it in `StreamRenderer.reset` ~line 300)
- Test: `tests/test_app.py`

**Interfaces:**
- Produces: `_detached_job_id(content: str) -> str | None`; `StreamRenderer._detached_cards: dict[str, SubAgentWidget]`.

- [ ] **Step 1: Write the failing test**

In `tests/test_app.py`, near `test_subagent_failed_detects_runner_error_text`:

```python
def test_detached_job_id_round_trips_with_the_handoff():
    from marim_harness.interfaces.tui.stream_render import _detached_job_id
    from marim_harness.tools.provider import _detach_handoff

    assert _detached_job_id(_detach_handoff("job-7")) == "job-7"
    # A normal report is not a handoff.
    assert _detached_job_id("Here is my report on the parser.") is None
    assert _detached_job_id("") is None
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_app.py::test_detached_job_id_round_trips_with_the_handoff -q --no-cov`
Expected: FAIL — `ImportError: cannot import name '_detached_job_id'`.

- [ ] **Step 3: Add the parser**

In `stream_render.py`, immediately after the `subagent_failed` function:

```python
_DETACH_PREFIX = "Started detached sub-agent "


def _detached_job_id(content: str) -> str | None:
    """The job id from an auto-detached spawn's handoff note (the text
    ``_detach_handoff`` returns), or None for any other tool return. Paired with
    that producer — a round-trip test pins the two formats together."""
    text = content.lstrip()
    if not text.startswith(_DETACH_PREFIX):
        return None
    job_id, sep, _ = text[len(_DETACH_PREFIX):].partition(",")
    return job_id.strip() if sep and job_id.strip() else None
```

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_app.py::test_detached_job_id_round_trips_with_the_handoff -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Add the map field + clear it on reset**

In `StreamRenderer.__init__`, right after `self.subagents: list[SubAgentWidget] = []` (~line 276):

```python
        # job_id → a pending detached-spawn card, awaiting its background job's
        # report. Filled on job settle (fill_finished_detached_cards); cleared on
        # session reset. Not pruned per-turn: the job finishes after the turn ends.
        self._detached_cards: dict[str, SubAgentWidget] = {}
```

In `StreamRenderer.reset`, after `self.subagents.clear()` (~line 307):

```python
        self._detached_cards.clear()
```

- [ ] **Step 6: Run ruff + pyright + the test**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest tests/test_app.py::test_detached_job_id_round_trips_with_the_handoff -q --no-cov`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/tui/stream_render.py tests/test_app.py
git commit -m "feat(tui): handoff job-id parser + detached-card map

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Keep the card pending, fill it on settle

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (add `note_detached_spawn`, `_fill_detached_card`, `fill_finished_detached_cards`; integrate into the `FunctionToolResultEvent` handler ~lines 547-561)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `_detached_job_id`, `_detached_cards` (Task 1); `subagent_failed`; `self.app.harness.deps.jobs` (a `JobRegistry` whose `get(id)` returns a `Job | None` with `.status`, `.result`, `.kind`).
- Produces: `StreamRenderer.note_detached_spawn(content: str, widget: SubAgentWidget, jobs) -> bool`; `StreamRenderer.fill_finished_detached_cards(jobs) -> None`.

- [ ] **Step 1: Write the failing integration test**

In `tests/test_app.py`:

```python
@pytest.mark.anyio
async def test_detached_card_stays_pending_then_fills_on_settle(tmp_path: Path):
    """An auto-detached spawn's card holds at pending on the handoff note, then
    fills with the real report when the background job finishes."""
    import asyncio

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.widgets import SubAgentWidget
    from marim_harness.tools.provider import _detach_handoff

    app = _app(tmp_path)
    reg = app.harness.deps.jobs
    gate = asyncio.Event()

    async def _work():
        await gate.wait()
        return "THE REAL REPORT"

    jid = reg.register("agent", "explore: map the core loop", _work())  # running

    call = FunctionToolCallEvent(part=ToolCallPart(
        tool_name="spawn_agent",
        args={"type": "explore", "task": "map the core loop"},
        tool_call_id="s1"))
    result = FunctionToolResultEvent(part=ToolReturnPart(
        tool_name="spawn_agent", content=_detach_handoff(jid), tool_call_id="s1"))

    async def gen():
        yield call
        yield result

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        card = app.stream.tool_widgets.get("s1")
        assert isinstance(card, SubAgentWidget)
        assert card.status == "pending"          # not finished on the handoff
        assert card.report != _detach_handoff(jid)

        gate.set()                               # let the job finish
        for _ in range(400):
            if reg.get(jid).status != "running":
                break
            await asyncio.sleep(0)
        app.stream.fill_finished_detached_cards(reg)
        await pilot.pause()
        assert card.status == "done"
        assert card.report == "THE REAL REPORT"
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_app.py::test_detached_card_stays_pending_then_fills_on_settle -q --no-cov`
Expected: FAIL — today the result handler finishes the card on the handoff, so `card.status` is `"done"` with the handoff as report (the first `pending` assertion fails).

- [ ] **Step 3: Add the renderer methods**

In `stream_render.py`, add these methods to `StreamRenderer` (e.g. after `prune_completed`):

```python
    def note_detached_spawn(self, content: str, widget: "SubAgentWidget", jobs) -> bool:
        """If ``content`` is a detached-spawn handoff, keep ``widget`` pending and
        map its job_id → card so it fills when the job settles; return True so the
        caller does NOT finish the card. Fills at once if the job already settled
        (a fast job can finish before its handoff renders). Returns False for a
        normal report, so foreground spawns and wait_for_job cards finish as usual."""
        job_id = _detached_job_id(content)
        if job_id is None:
            return False
        widget.activity = "running in background…"
        self._detached_cards[job_id] = widget
        self._fill_detached_card(job_id, jobs)
        return True

    def _fill_detached_card(self, job_id: str, jobs) -> None:
        """Finish the mapped card for ``job_id`` if its job is terminal, then drop
        it from the map. A no-op while the job still runs."""
        widget = self._detached_cards.get(job_id)
        if widget is None:
            return
        job = jobs.get(job_id)
        if job is None or job.status == "running":
            return
        report = job.result or ""
        if job.status in ("failed", "cancelled") or subagent_failed(report):
            status = "failed"
        else:
            status = "done"
        widget.finish(report, status=status)
        del self._detached_cards[job_id]

    def fill_finished_detached_cards(self, jobs) -> None:
        """Fill every mapped detached card whose job has settled. Called from the
        job-registry change hook so cards update live as background jobs complete."""
        for job_id in list(self._detached_cards):
            self._fill_detached_card(job_id, jobs)
```

- [ ] **Step 4: Integrate into the result handler**

In `stream_render.py`, the `FunctionToolResultEvent` branch (~lines 547-561). Replace the body inside `if widget is not None:` so a detached handoff short-circuits the finish:

```python
            widget = self.tool_widgets.get(event.tool_call_id)
            if widget is not None:
                content = str(getattr(event.part, "content", ""))
                if isinstance(widget, SubAgentWidget) and self.note_detached_spawn(
                    content, widget, self.app.harness.deps.jobs
                ):
                    pass  # detached: card stays pending, fills when its job settles
                else:
                    status = status_from_part(event.part)
                    # A spawn that failed returns its error as a normal (successful)
                    # tool result, so detect the runner's failure text and mark the
                    # card failed rather than letting it render a misleading ✓.
                    if (
                        isinstance(widget, SubAgentWidget)
                        and status == "done"
                        and subagent_failed(content)
                    ):
                        status = "failed"
                    widget.finish(content, status=status)
                    if isinstance(widget, ToolCallWidget):
                        group = self._group_of(widget)
                        if group is not None:
                            # Read widget.status *after* finish() so a bash non-zero
                            # exit (self-flipped inside finish) is detected.
                            group.note_child_finished(failed=widget.status == "failed")
            sink.on_result(event)
```

- [ ] **Step 5: Run, verify it passes + regressions**

Run: `uv run pytest tests/test_app.py tests/test_widgets.py -q --no-cov`
Expected: PASS (the new test, and the existing foreground-spawn / wait_for_job / failed-spawn card tests still pass — note_detached_spawn returns False for their non-handoff returns).

- [ ] **Step 6: Add a failed-job variant test**

In `tests/test_app.py`:

```python
@pytest.mark.anyio
async def test_detached_card_fills_failed_when_job_fails(tmp_path: Path):
    import asyncio

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.widgets import SubAgentWidget
    from marim_harness.tools.provider import _detach_handoff

    app = _app(tmp_path)
    reg = app.harness.deps.jobs

    async def _boom():
        raise ValueError("upstream 500")

    jid = reg.register("agent", "explore: x", _boom())
    for _ in range(400):  # let it settle to failed
        if reg.get(jid).status != "running":
            break
        await asyncio.sleep(0)

    call = FunctionToolCallEvent(part=ToolCallPart(
        tool_name="spawn_agent", args={"type": "explore", "task": "x"},
        tool_call_id="s1"))
    result = FunctionToolResultEvent(part=ToolReturnPart(
        tool_name="spawn_agent", content=_detach_handoff(jid), tool_call_id="s1"))

    async def gen():
        yield call
        yield result

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())   # job already terminal → immediate fill
        await pilot.pause()
        card = app.stream.tool_widgets.get("s1")
        assert isinstance(card, SubAgentWidget)
        assert card.status == "failed"
```

- [ ] **Step 7: Run the new test, then the gate**

Run: `uv run pytest tests/test_app.py -k detached_card -q --no-cov && uv run ruff check src tests && uv run pyright src`
Expected: tests pass; ruff clean; pyright 0 errors.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/tui/stream_render.py tests/test_app.py
git commit -m "feat(tui): hold detached-spawn card pending, fill its report on settle

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Fire the fill live from the job-registry hook

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (`_on_jobs_changed`, ~line 285)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `StreamRenderer.fill_finished_detached_cards(jobs)` (Task 2); `self.harness.deps.jobs`.

- [ ] **Step 1: Write the failing test**

In `tests/test_app.py` — same setup as Task 2's pending-then-fills test, but DO NOT call `fill_finished_detached_cards` manually; rely on the registry's `on_change` (wired to `_on_jobs_changed`) firing when the job settles:

```python
@pytest.mark.anyio
async def test_detached_card_fills_automatically_when_job_settles(tmp_path: Path):
    """Settling the job fires on_jobs_changed, which fills the card with no manual
    call — the live end-to-end path."""
    import asyncio

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.interfaces.tui.widgets import SubAgentWidget
    from marim_harness.tools.provider import _detach_handoff

    app = _app(tmp_path)
    reg = app.harness.deps.jobs
    gate = asyncio.Event()

    async def _work():
        await gate.wait()
        return "AUTO REPORT"

    jid = reg.register("agent", "explore: x", _work())

    call = FunctionToolCallEvent(part=ToolCallPart(
        tool_name="spawn_agent", args={"type": "explore", "task": "x"},
        tool_call_id="s1"))
    result = FunctionToolResultEvent(part=ToolReturnPart(
        tool_name="spawn_agent", content=_detach_handoff(jid), tool_call_id="s1"))

    async def gen():
        yield call
        yield result

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.stream.on_events(None, gen())
        await pilot.pause()
        card = app.stream.tool_widgets.get("s1")
        assert card.status == "pending"

        gate.set()                          # job finishes → on_change → fill (no manual call)
        for _ in range(400):
            if reg.get(jid).status != "running":
                break
            await asyncio.sleep(0)
        await pilot.pause()
        assert card.status == "done"
        assert card.report == "AUTO REPORT"
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_app.py::test_detached_card_fills_automatically_when_job_settles -q --no-cov`
Expected: FAIL — `_on_jobs_changed` doesn't fill yet, so the card stays `pending` after the job settles.

- [ ] **Step 3: Wire the fill into `_on_jobs_changed`**

In `app.py`, `_on_jobs_changed` (~line 285), add the fill alongside the existing calls:

```python
    def _on_jobs_changed(self) -> None:
        """Live callback from the job registry — repaint as jobs launch and
        finish. Each job runs as a task on the app's event loop, so the callback
        fires there and direct widget mutation is safe."""
        self.stream.fill_finished_detached_cards(self.harness.deps.jobs)
        self._render_jobs()
        self._notify_finished_jobs()
        self._maybe_wake()
```

- [ ] **Step 4: Run, verify it passes**

Run: `uv run pytest tests/test_app.py::test_detached_card_fills_automatically_when_job_settles -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Full gate**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: ruff clean, pyright 0 errors, all tests pass, coverage ≥90%.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py tests/test_app.py
git commit -m "feat(tui): fill detached sub-agent cards live on job completion

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- `_detached_job_id` parser pinned to `_detach_handoff` → Task 1.
- `_detached_cards` map + reset clearing → Task 1.
- `note_detached_spawn` (record, "running in background…" activity, immediate fill on already-terminal) → Task 2.
- `fill_finished_detached_cards` + the status rule (failed/cancelled → failed; subagent_failed → failed; else done) → Task 2.
- Result-handler integration (handoff short-circuits finish; foreground/wait paths unaffected) → Task 2.
- `_on_jobs_changed` wiring → Task 3.
- Race (fast job) → Task 2 `note_detached_spawn` immediate fill, covered by `test_detached_card_fills_failed_when_job_fails` (job terminal before the handoff event).
- Failed/cancelled → Task 2 failed-variant test.
- Session-switch clearing → Task 1 (`reset` clears the map).
- Out of scope (explicit `background=True`, streaming, resume) → no tasks, by design.

**Placeholder scan:** No TBD/TODO; every code step shows complete code.

**Type consistency:** `_detached_job_id(content) -> str | None`; `_detached_cards: dict[str, SubAgentWidget]`; `note_detached_spawn(content, widget, jobs) -> bool`; `_fill_detached_card(job_id, jobs)`; `fill_finished_detached_cards(jobs)`; `card.finish(report, status=status)`. Consistent across tasks and with the result-handler call site.

**Note on a shifting tree:** a parallel session is committing to this checkout. Each task commits only its named files; if `stream_render.py`/`app.py`/`test_app.py` show unexpected changes at a task start, confirm they aren't another session's before staging.
