# Job Poll Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop models from busy-polling background jobs: identical zero-information `jobs`/`job("list")`/`job_output` reads get a deterministic, escalating "stop polling — end your turn" response, plus docstring/listing-text nudges — per the approved spec `docs/superpowers/specs/2026-07-02-job-poll-guard-design.md`.

**Architecture:** A poll ledger on `JobRegistry` (`note_poll(key, snapshot) -> int`, cleared on every state change — register/settle/clear) counts consecutive identical read-only observations. The tools layer applies escalation: warn on the 2nd identical look, replace the body on the 3rd+ (interactive only — headless has no wake loop, so it gets an append-only `wait_for_job` pointer and never loses the data). Snapshot keys are stable projections (the `render_jobs` table has no timestamps; output-polls key on the output text, so a growing buffer is progress and never nagged).

**Tech Stack:** Python 3.10+, asyncio (single-loop registry, no locking), pytest (anyio).

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`.
- All commands via `uv` (`uv run pytest --no-cov …`, `uv run ruff check src tests`, `uv run pyright`). CI order: ruff → pyright → pytest.
- Scope: `src/marim_harness/jobs.py`, `src/marim_harness/tools/provider.py`, `tests/test_jobs.py`, `tests/test_provider.py` only. No TUI changes, no new callbacks.
- The guard fires only while at least one relevant job is `running` — reading settled results is never treated as polling. `wait_for_job` is exempt (bounded blocking, not busy-polling).
- Guard copy is exact (see Task 2 constants) — it is model-facing product surface, as are all docstring additions.
- No turn-boundary plumbing into `JobRegistry`; the ledger resets only on job state changes.
- The working tree may carry unrelated user WIP (pyproject.toml, uv.lock, app.py, scrapers/) — never stage or commit it; `git add` only the files each task names.

## File Structure

- `src/marim_harness/jobs.py` — `JobRegistry` gains the poll ledger: `note_poll`, cleared in `_settle`, `register`, `clear_history` (Task 1).
- `src/marim_harness/tools/provider.py` — guard constants + `_guarded_poll_response` + shared `_jobs_listing`/`_job_output_read` bodies; `jobs`, `job_output`, `job` route through them; docstring additions to `jobs`, `job`, and `spawn_agent`'s `after` paragraph (Task 2).
- Tests: `tests/test_jobs.py` (ledger), `tests/test_provider.py` (tool behavior, next to the existing `_job_ctx` tests).

---

### Task 1: `JobRegistry` poll ledger

**Files:**
- Modify: `src/marim_harness/jobs.py` (`__init__`, `_settle`, `register`, `clear_history`, new `note_poll`)
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (Task 2 relies on this): `JobRegistry.note_poll(key: str, snapshot: str) -> int` — returns the number of consecutive identical observations for `key` (1 = first sight or changed); the whole ledger clears on `register`, any settle (done/failed/cancelled), and `clear_history`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs.py` (the file already imports `asyncio`, `pytest`, and `JobRegistry`; reuse its gated-coroutine style):

```python
@pytest.mark.anyio
async def test_note_poll_counts_identical_and_resets_on_change():
    reg = JobRegistry()
    assert reg.note_poll("list", "A") == 1
    assert reg.note_poll("list", "A") == 2
    assert reg.note_poll("list", "A") == 3
    assert reg.note_poll("list", "B") == 1  # snapshot changed → fresh count
    assert reg.note_poll("output:job-1", "x") == 1  # keys are independent
    assert reg.note_poll("list", "B") == 2  # …and don't disturb each other


@pytest.mark.anyio
async def test_note_poll_ledger_clears_on_state_changes():
    reg = JobRegistry()
    assert reg.note_poll("list", "A") == 1

    gate = asyncio.Event()

    async def _work() -> str:
        await gate.wait()
        return "ok"

    jid = reg.register("agent", "w", _work())  # register clears the ledger
    assert reg.note_poll("list", "A") == 1
    assert reg.note_poll("list", "A") == 2
    gate.set()
    await reg.wait(jid, 5)  # settle clears the ledger
    assert reg.note_poll("list", "A") == 1
    reg.note_poll("list", "A")
    reg.clear_history()  # /clear clears the ledger
    assert reg.note_poll("list", "A") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_jobs.py -k note_poll -v`
Expected: FAIL with `AttributeError: 'JobRegistry' object has no attribute 'note_poll'`.

- [ ] **Step 3: Implement**

In `JobRegistry.__init__`, after the `self._wake_consumed` block, add:

```python
        # Poll ledger: consecutive identical read-only observations per surface
        # ("list", "output:<job-id>") since the last state change. Read by the
        # jobs tools (via note_poll) to nudge a model out of busy-polling with
        # an escalating no-change response; any register/settle/clear resets it
        # because the next poll genuinely has something new to see. Deliberately
        # NOT reset at turn boundaries — the ledger keys off job state, not
        # turns (spec 2026-07-02-job-poll-guard-design).
        self._poll_ledger: dict[str, tuple[str, int]] = {}
```

After `_next_id`, add:

```python
    def note_poll(self, key: str, snapshot: str) -> int:
        """Record one read-only poll of ``key`` (a tool surface: ``"list"`` or
        ``"output:<job-id>"``) that observed ``snapshot``, and return how many
        consecutive polls of that key saw this exact snapshot (1 = first sight,
        or changed since last time). Snapshots must be stable projections —
        never include elapsed-time renderings, or the count can never rise."""
        last, count = self._poll_ledger.get(key, ("", 0))
        count = count + 1 if snapshot == last else 1
        self._poll_ledger[key] = (snapshot, count)
        return count
```

Add `self._poll_ledger.clear()` at three state-change points:
- in `_settle`, right after `job.status = status` (inside the not-already-terminal path);
- in `register`, right before `self._notify()`;
- in `clear_history`, next to `self._wake_consumed.clear()`.

- [ ] **Step 4: Run tests to verify they pass (whole file)**

Run: `uv run pytest --no-cov tests/test_jobs.py -v`
Expected: ALL PASS (existing registry tests untouched).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/jobs.py tests/test_jobs.py
git commit -m "feat(jobs): poll ledger — count identical read-only observations"
```

---

### Task 2: Tool-layer guard, wake note, docstrings, CI gate

**Files:**
- Modify: `src/marim_harness/tools/provider.py` (`jobs` at ~892, `job_output` at ~901, `job` at ~925, `spawn_agent`'s `after` docstring paragraph at ~636; new module-level constants + helpers directly above `jobs`)
- Test: `tests/test_provider.py` (next to the existing `_job_ctx` tests, ~line 279)

**Interfaces:**
- Consumes: `JobRegistry.note_poll(key, snapshot) -> int` (Task 1); `ctx.deps.ui.interactive` (existing `UIHooks` flag — False headless, True once the TUI calls `bind_ui`).
- Produces: nothing new for later tasks (this is the last task). `jobs()` and `job("list")` now share `_jobs_listing(ctx)`; `job_output()` and `job("output")` share `_job_output_read(ctx, id)`.

- [ ] **Step 1: Write the failing tool tests**

Append to `tests/test_provider.py` (reuses `_make_deps`, `SimpleNamespace`, `pytest` already imported there):

```python
async def _poll_ctx(tmp_path):
    """A ctx over a REAL JobRegistry holding one gated running agent job."""
    import asyncio

    from marim_harness.jobs import JobRegistry

    deps = _make_deps(tmp_path)
    deps.jobs = JobRegistry()
    gate = asyncio.Event()

    async def _work() -> str:
        await gate.wait()
        return "done!"

    jid = deps.jobs.register("agent", "explore: map it", _work())
    return SimpleNamespace(deps=deps), gate, jid


@pytest.mark.anyio
async def test_jobs_listing_appends_wake_note_when_interactive(tmp_path):
    from marim_harness.tools.provider import jobs as jobs_tool

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    out = jobs_tool(ctx)
    assert jid in out
    assert "wake you on completion" in out
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_poll_guard_escalates_warn_then_replace(tmp_path):
    from marim_harness.tools.provider import jobs as jobs_tool

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    first = jobs_tool(ctx)
    assert "No change since your last check" not in first
    second = jobs_tool(ctx)
    assert jid in second  # table still present on the first repeat…
    assert "end your turn" in second  # …plus the warning
    third = jobs_tool(ctx)
    assert "(poll 3)" in third and "Stop polling" in third
    assert jid not in third  # the table is withheld, not decorated
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_poll_guard_headless_appends_and_never_replaces(tmp_path):
    from marim_harness.tools.provider import jobs as jobs_tool

    ctx, gate, jid = await _poll_ctx(tmp_path)
    assert ctx.deps.ui.interactive is False  # headless default
    jobs_tool(ctx)
    second = jobs_tool(ctx)
    third = jobs_tool(ctx)
    for out in (second, third):
        assert jid in out  # headless never loses the data
        assert "wait_for_job" in out  # …and is pointed at blocking instead
        assert "end your turn" not in out  # no wake loop headless
    assert "wake you on completion" not in third  # standing note is TUI-only
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_settled_listing_is_a_result_read_not_a_poll(tmp_path):
    from marim_harness.tools.provider import jobs as jobs_tool

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)
    out = ""
    for _ in range(3):
        out = jobs_tool(ctx)
        assert "No change since your last check" not in out
        assert jid in out
    assert "wake you on completion" not in out  # nothing running


@pytest.mark.anyio
async def test_static_output_marker_triggers_guard(tmp_path):
    from marim_harness.tools.provider import job_output

    ctx, gate, jid = await _poll_ctx(tmp_path)
    ctx.deps.ui.interactive = True
    first = job_output(ctx, jid)  # "(still running)" — an agent job has no output_fn
    assert "No change since your last check" not in first
    assert "end your turn" in job_output(ctx, jid)  # identical marker → warn
    gate.set()
    await ctx.deps.jobs.wait(jid, 5)


@pytest.mark.anyio
async def test_growing_output_is_progress_not_polling(tmp_path):
    import asyncio

    from marim_harness.jobs import JobRegistry
    from marim_harness.tools.provider import job_output

    deps = _make_deps(tmp_path)
    deps.jobs = JobRegistry()
    deps.ui.interactive = True
    ctx = SimpleNamespace(deps=deps)
    gate = asyncio.Event()
    buf = ["a"]

    async def _work() -> str:
        await gate.wait()
        return "ok"

    jid = deps.jobs.register("bash", "tail -f", _work(), output_fn=lambda: "".join(buf))
    assert "No change since your last check" not in job_output(ctx, jid)
    buf.append("b")  # the buffer grew — that's progress
    assert "No change since your last check" not in job_output(ctx, jid)
    gate.set()
    await deps.jobs.wait(jid, 5)
```

Also update `_job_ctx`'s `FakeJobs` (~test_provider.py:255-270): add a `get` stub so the new shared read path doesn't crash on the fake —

```python
        def get(self, id):
            return None  # no running job → the poll guard stays out of the way
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_provider.py -k "poll or wake_note or settled_listing or static_output or growing_output" -v`
Expected: FAIL — no wake note, no guard text, `jobs()` returns the bare table every time.

- [ ] **Step 3: Implement the guard + shared bodies**

In `provider.py`, directly above the `jobs` tool function, add:

```python
_POLL_WAKE_NOTE = "(running jobs wake you on completion — no need to check again)"
_POLL_WARN = (
    "⚠ No change since your last check. If you have no other work, end your "
    "turn — finished jobs wake you and deliver their reports automatically."
)
_POLL_WARN_HEADLESS = (
    "⚠ No change since your last check. Use wait_for_job(id) to block until a "
    "job finishes instead of polling."
)


def _guarded_poll_response(
    ctx: RunContext[Deps], key: str, body: str, *, any_running: bool
) -> str:
    """Apply the poll guard (spec 2026-07-02-job-poll-guard-design) to one
    read-only jobs response. Counts only while something still runs — reading
    settled results is never polling. Interactive sessions escalate: the 2nd
    identical look appends a warning, the 3rd+ replaces the body entirely (a
    wake loop exists, so ending the turn is always safe, and a fresh-looking
    table makes the warning read as boilerplate). Headless has no wake loop and
    may still need the data: append-only, pointing at wait_for_job."""
    if not any_running:
        return body
    count = ctx.deps.jobs.note_poll(key, body)
    if count < 2:
        return body
    if not ctx.deps.ui.interactive:
        return f"{body}\n\n{_POLL_WARN_HEADLESS}"
    if count == 2:
        return f"{body}\n\n{_POLL_WARN}"
    return (
        f"No change since your last check (poll {count}). Stop polling: end "
        "your turn now — finished jobs wake you and deliver their reports "
        "automatically. Use wait_for_job(id) only if you must block on a "
        "result inside this turn."
    )


def _jobs_listing(ctx: RunContext[Deps]) -> str:
    """The shared body of jobs() and job("list"): the rendered table, with the
    standing wake note while anything runs (interactive only — headless has no
    wake loop), passed through the poll guard. render_jobs output is a stable
    projection (no elapsed times), so it doubles as the poll snapshot."""
    listed = ctx.deps.jobs.list()
    rows = render_jobs(listed)
    if not rows:
        return "No background jobs."
    any_running = any(j.status == "running" for j in listed)
    if any_running and ctx.deps.ui.interactive:
        rows = f"{rows}\n{_POLL_WAKE_NOTE}"
    return _guarded_poll_response(ctx, "list", rows, any_running=any_running)


def _job_output_read(ctx: RunContext[Deps], id: str) -> str:
    """The shared body of job_output() and job("output"): the read, passed
    through the poll guard keyed per job while that job still runs. A growing
    bash buffer changes the snapshot every call, so real progress is never
    nagged — only zero-information repeats are."""
    target = ctx.deps.jobs.get(id)
    body = ctx.deps.jobs.output(id, mark_seen=True)
    running = target is not None and target.status == "running"
    return _guarded_poll_response(ctx, f"output:{id}", body, any_running=running)
```

Rewire the three tools:

```python
def jobs(ctx: RunContext[Deps]) -> str:
    """List the background jobs you've launched this session, with their id, kind
    (bash/agent), label, and status (running/done/failed/cancelled). Use this to
    see what's still in flight before pulling results with job_output or
    wait_for_job. Never call this in a loop to wait — if you have no other work,
    end your turn; the harness wakes you when a job finishes and delivers its
    report."""
    return _jobs_listing(ctx)
```

```python
def job_output(ctx: RunContext[Deps], id: str) -> str:
    """Read a background job's output by id without blocking: the final result if
    it's finished, the live output so far for a running bash job, or a running
    marker otherwise. To block until a job finishes, use wait_for_job instead."""
    return _job_output_read(ctx, id)
```

In `job` (the consolidated tool): replace the `"list"` branch body with `return _jobs_listing(ctx)`, the `"output"` branch body with `return _job_output_read(ctx, id)`, and append this sentence to the end of its docstring: `Never call "list" or "output" in a loop to wait — if you have no other work, end your turn; the harness wakes you when a job finishes and delivers its report.`

In `spawn_agent`'s docstring, append to the end of the `after` paragraph ("…the failure surfaces in the jobs digest."): `Prerequisite ids come from the spawn handoffs ("Started job-N …"); issue a dependent spawn in a later response, after those return — ids cannot be guessed.`

- [ ] **Step 4: Run tests to verify they pass (both files, including existing job tests)**

Run: `uv run pytest --no-cov tests/test_provider.py tests/test_jobs.py tests/test_jobs_tools.py -v`
Expected: ALL PASS — including the pre-existing `test_job_dispatches_each_action` (the FakeJobs `get` stub keeps the fake path guard-free).

- [ ] **Step 5: Full CI gate**

Run, in CI's order: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all clean (coverage threshold 90% holds). Fix only what this change introduced.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tools/provider.py tests/test_provider.py
git commit -m "feat(tools): escalating poll guard on job reads; wake-note + anti-poll docs"
```

---

## Known, accepted non-goals (from the spec — do not "fix")

- `wait_for_job` stays unguarded (bounded blocking is legitimate).
- No turn-boundary reset of the ledger; a fresh turn's first identical re-list gets the gentle count-2 warning with the table.
- The TUI jobs panel reads `jobs.list()`/`render_jobs` directly and never calls `note_poll` — panel repaints are not model polls.
- No hard turn termination — the harness nudges deterministically but never force-ends a turn.
