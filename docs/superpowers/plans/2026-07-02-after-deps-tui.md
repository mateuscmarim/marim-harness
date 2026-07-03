# after= Dependencies in the TUI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `spawn_agent(after=[...])` dependent renders as *waiting* (not running) on its card and on the Ctrl+X sub-agents screen, flips to running when its prerequisites settle, and attributes a prerequisite failure to the culprit job — per the approved spec `docs/superpowers/specs/2026-07-02-after-deps-tui-design.md`.

**Architecture:** Renderer-derived, TUI-only (spec approach A). The card learns its prerequisite ids from the spawn tool args it already receives, its own job id from the detach handoff it already parses, and the waiting→running flip rides the existing jobs-change hook (`fill_finished_detached_cards`). `waiting` is display-only state — `status` stays `"pending"` everywhere.

**Tech Stack:** Python 3.10+, Textual widgets, pytest (anyio) with the repo's existing card/renderer test patterns.

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced).
- All commands via `uv` (`uv run pytest --no-cov …`, `uv run ruff check src tests`, `uv run pyright`). CI order: ruff → pyright → pytest.
- Scope is `src/marim_harness/interfaces/tui/` ONLY. No changes to `tools/`, `jobs.py`, `runtime/`, or headless behavior.
- `waiting` is derived display state: `status` stays `"pending"`; nothing that switches on status may change behavior.
- Docstrings/comments are product surface — copy the given ones verbatim.
- The working tree may carry unrelated user WIP (pyproject.toml, uv.lock, app.py, scrapers/) — never stage or commit it; `git add` only the files each task names.

## File Structure

- `src/marim_harness/interfaces/tui/widgets/subagent_stats.py` — pure presentation: waiting glyph, row cells, summary aggregation (Task 1).
- `src/marim_harness/interfaces/tui/widgets/subagents_view.py` — summary bar text gains the `N waiting` segment (Task 1).
- `src/marim_harness/interfaces/tui/widgets/subagent.py` — card state fields + waiting/blocked rendering (Task 2).
- `src/marim_harness/interfaces/tui/stream_render.py` — pure helpers (`_after_ids`, `_deps_pending`, `blocked_by_id`), fail-prefix additions, and the four wiring points (Task 3).
- Tests: `tests/test_subagent_stats.py`, `tests/test_subagents_screen.py`, `tests/test_subagent_card.py`, `tests/test_app.py`.

---

### Task 1: Pure presentation — waiting glyph, summary split, summary bar

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent_stats.py` (`status_glyph`, `row_cells`, `SummaryStats`, `aggregate`)
- Modify: `src/marim_harness/interfaces/tui/widgets/subagents_view.py` (`SubAgentSummary.refresh_totals`)
- Test: `tests/test_subagent_stats.py`, `tests/test_subagents_screen.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (Tasks 2–3 rely on these):
  - `status_glyph(status: str, waiting: bool = False) -> str` — `"⧗"` for a non-terminal waiting agent.
  - `SummaryStats` gains field `waiting: int` (inserted after `running`; `aggregate` constructs by keyword, so ordering is safe).
  - `aggregate` counts an agent as waiting when non-terminal and `getattr(a, "waiting", False)`.
  - `FakeAgent` (test fixture) gains `waiting: bool = False`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_subagent_stats.py`, add `waiting: bool = False` to the `FakeAgent` dataclass (after `detached: bool = False`), then append:

```python
def test_status_glyph_waiting_variant():
    # A pending agent blocked on after= prerequisites shows ⧗, not the running ▸.
    assert status_glyph("pending", waiting=True) == "⧗"
    assert status_glyph("pending", waiting=False) == "▸"
    # Terminal states win over a stale waiting flag.
    assert status_glyph("done", waiting=True) == "✓"
    assert status_glyph("failed", waiting=True) == "✕"


def test_row_cells_waiting_agent_gets_hourglass():
    a = FakeAgent(status="pending", waiting=True)
    assert row_cells(a)[0] == "⧗"


def test_row_cells_tolerates_agents_without_waiting_field():
    # FakeNode has no `waiting` attribute — row_cells must not require it.
    n = FakeNode(stream_id="s1")
    assert row_cells(n)[0] == "✓"


def test_aggregate_splits_waiting_out_of_running():
    agents = [
        FakeAgent(status="pending", waiting=True, tokens=10),
        FakeAgent(status="pending", tokens=20),
        FakeAgent(status="done", tokens=30),
    ]
    stats = aggregate(agents, cost_of=lambda a: 0.0)
    assert stats.total == 3
    assert stats.waiting == 1
    assert stats.running == 1  # the genuinely-executing one only
    assert stats.done == 1
    assert stats.tokens == 60
```

In `tests/test_subagents_screen.py`, append (reuses the file's `_ListApp` and `FakeAgent` import pattern from `test_list_rows_and_summary`):

```python
@pytest.mark.anyio
async def test_summary_bar_shows_waiting_segment_only_when_nonzero():
    from tests.test_subagent_stats import FakeAgent

    app = _ListApp()
    async with app.run_test():
        summ = app.query_one(SubAgentSummary)
        summ.refresh_totals(aggregate(
            [FakeAgent(status="pending", waiting=True), FakeAgent(status="done")],
            cost_of=lambda a: 0.0,
        ))
        assert "1 waiting" in str(summ.render())
        summ.refresh_totals(aggregate(
            [FakeAgent(status="done")], cost_of=lambda a: 0.0,
        ))
        assert "waiting" not in str(summ.render())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_stats.py -k "waiting" -v` and `uv run pytest --no-cov tests/test_subagents_screen.py -k waiting_segment -v`
Expected: FAIL — `status_glyph() got an unexpected keyword argument 'waiting'`; `SummaryStats` has no `waiting`; summary text lacks "waiting".

- [ ] **Step 3: Implement**

In `subagent_stats.py`, replace `status_glyph` and update `row_cells`' glyph cell:

```python
def status_glyph(status: str, waiting: bool = False) -> str:
    """The list glyph for a sub-agent status; running agents get a ▸ marker,
    and a non-terminal agent blocked on after= prerequisites gets ⧗ so a
    stalled fan-out doesn't read as busier than it is."""
    if status in STATUS_GLYPH:
        return STATUS_GLYPH[status]
    return "⧗" if waiting else "▸"
```

In `row_cells`, change the first cell to:

```python
        status_glyph(agent.status, getattr(agent, "waiting", False)),
```

In `SummaryStats`, insert `waiting: int` after `running: int`. In `aggregate`, change the docstring's tail sentence "everything not terminal is running" to "everything not terminal is running — split into *waiting* (blocked on after= prerequisites, ``getattr`` so plain stand-ins work) and genuinely running", and rework the counting loop:

```python
    running = waiting = done = failed = tokens = 0
    cost = 0.0
    for a in agents:
        tokens += a.tokens
        cost += cost_of(a)
        if a.status == "done":
            done += 1
        elif a.status in ("failed", "denied"):
            failed += 1
        elif getattr(a, "waiting", False):
            waiting += 1
        else:
            running += 1
    return SummaryStats(
        total=len(agents),
        running=running,
        waiting=waiting,
        done=done,
        failed=failed,
        tokens=tokens,
        cost_text=format_cost(cost) if tokens else "",
    )
```

In `subagents_view.py`, rework `SubAgentSummary.refresh_totals`'s `left` so waiting appears only when non-zero (between total and running):

```python
    def refresh_totals(self, stats: SummaryStats) -> None:
        mid = f"{stats.running} running · {stats.done} done · {stats.failed} failed"
        if stats.waiting:
            mid = f"{stats.waiting} waiting · {mid}"
        left = f"{stats.total} sub-agents · {mid}"
        right = f"{stats.tokens:,} tokens"
        if stats.cost_text:
            right = f"{right} · {stats.cost_text}"
        self.update(Content(f"{left}    {right}"))
```

- [ ] **Step 4: Run tests to verify they pass (plus the files' existing tests)**

Run: `uv run pytest --no-cov tests/test_subagent_stats.py tests/test_subagents_screen.py -v`
Expected: ALL PASS (existing glyph/rows/summary tests stay green — `waiting` defaults keep old constructions valid).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/subagent_stats.py \
        src/marim_harness/interfaces/tui/widgets/subagents_view.py \
        tests/test_subagent_stats.py tests/test_subagents_screen.py
git commit -m "feat(tui): waiting glyph + summary split for after= dependents"
```

---

### Task 2: Card widget — waiting/blocked state and rendering

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/subagent.py` (`__init__` fields, `_glyph`, `_paint_header`, `_paint_activity`, new `set_waiting`)
- Test: `tests/test_subagent_card.py`

**Interfaces:**
- Consumes: nothing from Task 1 (card paints its own lines).
- Produces (Task 3 relies on these):
  - `SubAgentWidget.after_ids: list[str]` (default `[]`), `job_id: str | None` (default None), `waiting: bool` (default False), `blocked_by: str | None` (default None) — all set post-construction by the renderer, like `stream_id`/`parent_id`.
  - `SubAgentWidget.set_waiting(waiting: bool) -> None` — flips the flag and repaints both lines; no-op when unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_card.py`:

```python
def test_waiting_card_shows_hourglass_after_tag_and_waiting_line():
    w = SubAgentWidget("merge", "Combine the reports", "sonnet")
    w.detached = True
    w.after_ids = ["job-3", "job-4"]
    w.set_waiting(True)
    header = str(w._header.render())
    assert "⧗" in header                      # static hourglass, not the spinner
    assert "after job-3, job-4" in header     # dim prerequisite tag
    assert "bg" in header                     # existing marker preserved
    assert "waiting on job-3, job-4" in str(w._activity.render())


def test_set_waiting_flip_restores_running_rendering():
    w = SubAgentWidget("merge", "Combine the reports", "sonnet")
    w.after_ids = ["job-3"]
    w.set_waiting(True)
    w.set_waiting(False)
    header = str(w._header.render())
    assert "⧗" not in header and "after" not in header
    assert "working…" in str(w._activity.render())


def test_blocked_card_names_the_culprit_in_header():
    w = SubAgentWidget("merge", "Combine the reports", "sonnet")
    w.after_ids = ["job-3"]
    w.blocked_by = "job-3"
    w.finish("PrerequisiteFailed: prerequisite job-3 failed — boom", status="failed")
    header = str(w._header.render())
    assert "blocked by job-3" in header
    assert "✕" in header
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_card.py -k "waiting or blocked" -v`
Expected: FAIL with `AttributeError: 'SubAgentWidget' object has no attribute 'set_waiting'` (first test), then attribute errors for the new fields.

- [ ] **Step 3: Implement**

In `SubAgentWidget.__init__`, after the `self.detached = False` block, add:

```python
        # after= dependency display (spec 2026-07-02-after-deps-tui-design).
        # ``after_ids`` are the prerequisite background-job ids from the spawn's
        # tool args; ``job_id`` is this card's own background job (parsed from
        # the detach handoff); ``waiting`` is DERIVED display state — status
        # stays "pending", so nothing that switches on status changes; and
        # ``blocked_by`` names the failed prerequisite once one kills the run.
        # All set post-construction by the renderer, like stream_id/parent_id.
        self.after_ids: list[str] = []
        self.job_id: str | None = None
        self.waiting = False
        self.blocked_by: str | None = None
```

In `_glyph`, add the waiting branch before the spinner:

```python
    def _glyph(self) -> str:
        if self.status == "done":
            return "✓"
        if self.status in ("denied", "failed"):
            return "✕"
        if self.waiting:
            return "⧗"
        return _SPINNER[self._spin]
```

In `_paint_header`, after the `bg` tag append and before the type/title part:

```python
        if self.waiting and self.after_ids:
            parts.append((f"after {', '.join(self.after_ids)} ", "dim"))
        elif self.blocked_by:
            parts.append((f"blocked by {self.blocked_by} ", "dim red"))
```

In `_paint_activity`, replace the pending branch's update with:

```python
        if self.status == "pending":
            if self.waiting and self.after_ids:
                # Blocked on prerequisites: say so instead of "working…", so a
                # stalled dependent is tellable from a busy one at a glance.
                self._activity.update(
                    Content(f"↳ waiting on {', '.join(self.after_ids)}")
                )
            else:
                # Show the current tool while running; "working…" before the
                # first call.
                self._activity.update(Content(f"↳ {self.activity or 'working…'}"))
```

After `set_model`, add:

```python
    def set_waiting(self, waiting: bool) -> None:
        """Flip the derived waiting display state (an after= spawn blocked on
        prerequisites) and repaint both card lines. Display-only: ``status``
        stays "pending". No-op when unchanged, so jobs-change sweeps can call
        it unconditionally."""
        if self.waiting == waiting:
            return
        self.waiting = waiting
        self._paint_header()
        self._paint_activity()
```

- [ ] **Step 4: Run tests to verify they pass (whole file)**

Run: `uv run pytest --no-cov tests/test_subagent_card.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/subagent.py tests/test_subagent_card.py
git commit -m "feat(tui): sub-agent card renders waiting/blocked after= state"
```

---

### Task 3: Renderer wiring, pure helpers, fail-prefix fix, CI gate

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (`_SUBAGENT_FAIL_PREFIXES`, new `_after_ids`/`_deps_pending`/`blocked_by_id`, `mount_spawn_widget`, `note_detached_spawn`, `_fill_detached_card`, `fill_finished_detached_cards`)
- Test: `tests/test_app.py` (pure helpers, next to `test_subagent_failed_detects_runner_error_text` at ~line 1139), `tests/test_subagents_screen.py` (app-level round trip)

**Interfaces:**
- Consumes: `SubAgentWidget.after_ids/job_id/waiting/blocked_by/set_waiting` (Task 2).
- Produces (module-level in `stream_render.py`):
  - `_after_ids(args: dict) -> list[str]`
  - `_deps_pending(after_ids: list[str], jobs) -> bool`
  - `blocked_by_id(content: str) -> str | None`

- [ ] **Step 1: Write the failing pure-helper tests**

Append to `tests/test_app.py`, directly after `test_subagent_failed_detects_runner_error_text`:

```python
def test_subagent_failed_detects_after_rejections():
    from marim_harness.interfaces.tui.stream_render import subagent_failed

    assert subagent_failed("Cannot spawn with after=['job-9']: no such job(s).") is True
    assert subagent_failed("after= requires a detached spawn. Pass background=True…") is True


def test_after_ids_normalizes_str_and_list():
    from marim_harness.interfaces.tui.stream_render import _after_ids

    assert _after_ids({"after": "job-1"}) == ["job-1"]
    assert _after_ids({"after": ["job-1", " job-2 ", ""]}) == ["job-1", "job-2"]
    assert _after_ids({"after": None}) == []
    assert _after_ids({}) == []


def test_deps_pending_only_while_a_prerequisite_runs():
    from types import SimpleNamespace

    from marim_harness.interfaces.tui.stream_render import _deps_pending

    jobs = SimpleNamespace(get=lambda jid: {
        "job-1": SimpleNamespace(status="running"),
        "job-2": SimpleNamespace(status="done"),
    }.get(jid))
    assert _deps_pending(["job-1", "job-2"], jobs) is True
    assert _deps_pending(["job-2"], jobs) is False
    # A pruned/unknown id counts as settled — never blocks a card forever.
    assert _deps_pending(["job-gone"], jobs) is False


def test_blocked_by_id_parses_prerequisite_failures():
    from marim_harness.interfaces.tui.stream_render import blocked_by_id

    assert blocked_by_id("prerequisite job-3 failed — boom") == "job-3"
    assert blocked_by_id("PrerequisiteFailed: prerequisite job-7 cancelled") == "job-7"
    assert blocked_by_id("Sub-agent 'merge' failed: ValueError: boom") is None
```

- [ ] **Step 2: Run pure tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_app.py -k "after_rejections or after_ids or deps_pending or blocked_by" -v`
Expected: FAIL — `subagent_failed` returns False for the new texts; `ImportError` for `_after_ids`/`_deps_pending`/`blocked_by_id`.

- [ ] **Step 3: Implement helpers + prefix fix**

In `stream_render.py`, add `import re` to the imports. Extend `_SUBAGENT_FAIL_PREFIXES` with two entries (and extend its comment's last line with "Tool-level after= rejections are included so a refused dependent renders ✕, not a green ✓."):

```python
    "Cannot spawn with after=",
    "after= requires a detached spawn",
```

Below `_wait_subagent_label`, add the three helpers:

```python
def _after_ids(args: dict) -> list[str]:
    """The spawn's after= prerequisite job ids from its tool args, normalized
    (str → 1-list; entries stripped, empties dropped). Local on purpose — the
    tool layer has its own normalizer, but the TUI shouldn't import tools."""
    raw = args.get("after")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [s for s in (str(x).strip() for x in raw) if s]


def _deps_pending(after_ids: list[str], jobs) -> bool:
    """True while any prerequisite job is still running. A missing/pruned id
    counts as settled so a card can never block forever on a forgotten job —
    mirrors JobRegistry.await_settled's semantics for display purposes."""
    return any(
        (job := jobs.get(jid)) is not None and job.status == "running"
        for jid in after_ids
    )


_PREREQ_RE = re.compile(r"prerequisite (\S+) (?:failed|cancelled)")


def blocked_by_id(content: str) -> str | None:
    """The culprit job id from a PrerequisiteFailed report, or None. Matches the
    message _run_after raises ("prerequisite job-3 failed — …"), which may reach
    the card prefixed by the exception class name; only the head is scanned so a
    report that merely quotes the phrase deep in its body doesn't match."""
    m = _PREREQ_RE.search(content[:300])
    return m.group(1) if m else None
```

- [ ] **Step 4: Run pure tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_app.py -k "after_rejections or after_ids or deps_pending or blocked_by" -v`
Expected: PASS.

- [ ] **Step 5: Write the failing app-level round-trip test**

Append to `tests/test_subagents_screen.py` (mirrors `test_detached_card_streams_live` above it; `row_cells` import pattern already used there):

```python
@pytest.mark.anyio
async def test_after_dependent_card_waits_then_flips(tmp_path):
    """A dependent spawn renders as waiting (⧗, 'after' tag, waiting line, list
    glyph) while its prerequisite runs, and flips to running rendering when the
    jobs-change sweep sees the prerequisite settle. status stays 'pending'
    throughout — waiting is display-only."""
    from types import SimpleNamespace

    from marim_harness.interfaces.tui.widgets.subagent_stats import row_cells

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({
            "type": "merge", "description": "combine reports",
            "after": ["job-1"],
        })
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        await pilot.pause()
        assert w.after_ids == ["job-1"]

        prereq = {"status": "running"}
        jobs = SimpleNamespace(get=lambda jid: SimpleNamespace(**prereq)
                               if jid == "job-1" else None)
        kept = r.note_detached_spawn(
            "Started detached sub-agent job-2, running in the background.", w, jobs
        )
        await pilot.pause()
        assert kept is True
        assert w.job_id == "job-2"
        assert w.waiting is True and w.status == "pending"
        assert "⧗" in str(w._header.render())
        assert row_cells(w)[0] == "⧗"

        prereq["status"] = "done"
        r.fill_finished_detached_cards(jobs)
        await pilot.pause()
        assert w.waiting is False and w.status == "pending"
        assert "⧗" not in str(w._header.render())
        assert row_cells(w)[0] == "▸"


@pytest.mark.anyio
async def test_failed_prerequisite_attributes_blocker(tmp_path):
    """When the dependent's own job dies with PrerequisiteFailed, the filled card
    is failed and names the culprit in its header tag."""
    from types import SimpleNamespace

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({
            "type": "merge", "description": "combine reports", "after": ["job-1"],
        })
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        await pilot.pause()

        report = "PrerequisiteFailed: prerequisite job-1 failed — boom"
        job = SimpleNamespace(status="failed", result=report)
        jobs = SimpleNamespace(get=lambda jid: job if jid == "job-2" else None)
        r.note_detached_spawn("Started detached sub-agent job-2, …", w, jobs)
        await pilot.pause()
        assert w.status == "failed"
        assert w.blocked_by == "job-1"
        assert "blocked by job-1" in str(w._header.render())
```

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -k "waits_then_flips or attributes_blocker" -v`
Expected: FAIL — `after_ids` stays `[]` (mount_spawn_widget ignores `after`), `job_id` is None, `waiting` never set, `blocked_by` never set.

- [ ] **Step 6: Implement the wiring**

In `stream_render.py`:

1. `mount_spawn_widget` — after `self.subagents.append(widget)` insert (before `return widget`):

```python
        widget.after_ids = _after_ids(args)
```

2. `note_detached_spawn` — after `widget.detached = True` insert, and extend the docstring with one sentence: "An after= dependent also records its own job id and enters the derived *waiting* display state while any prerequisite still runs (spec 2026-07-02-after-deps-tui-design)."

```python
        widget.job_id = job_id
        if widget.after_ids and _deps_pending(widget.after_ids, jobs):
            widget.set_waiting(True)
```

3. `_fill_detached_card` — the failed branch gains attribution. Replace the status-selection block with:

```python
        report = job.result or ""
        if job.status in ("failed", "cancelled") or subagent_failed(report):
            status = "failed"
            # A PrerequisiteFailed report names the job that killed this
            # dependent; surface it on the header tag (the red ↳ line already
            # carries the full message). Clear waiting so the tag branch flips.
            culprit = blocked_by_id(report)
            if culprit:
                widget.blocked_by = culprit
            widget.waiting = False
        else:
            status = "done"
```

4. `fill_finished_detached_cards` — before the existing `for job_id in list(self._detached_cards):` loop, insert:

```python
        # Waiting→running sweep: a settle may have unblocked an after=
        # dependent whose own job keeps running. set_waiting no-ops when
        # unchanged, so sweeping every card is cheap.
        for widget in self.subagents:
            if widget.waiting and not _deps_pending(widget.after_ids, jobs):
                widget.set_waiting(False)
```

- [ ] **Step 7: Run the app-level tests to verify they pass (whole files)**

Run: `uv run pytest --no-cov tests/test_subagents_screen.py tests/test_app.py tests/test_subagent_card.py tests/test_subagent_stats.py -v`
Expected: ALL PASS.

- [ ] **Step 8: Full CI gate**

Run, in CI's order: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all clean (coverage threshold 90% holds). Fix only what this change introduced.

- [ ] **Step 9: Commit**

```bash
git add src/marim_harness/interfaces/tui/stream_render.py \
        tests/test_app.py tests/test_subagents_screen.py
git commit -m "feat(tui): wire after= waiting state + blocked-by attribution; fail-prefix fix"
```

---

## Known, accepted limitations (from the spec — do not "fix")

- No jump-to-blocker navigation and no DAG rendering (deferred follow-ups).
- Resumed sessions never show waiting (jobs are process-scoped).
- The jobs panel keeps its existing `(waiting on …)` output_fn text — no change there.
