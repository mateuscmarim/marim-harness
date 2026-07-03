# spawn_agent `after=` Dependencies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harness-enforced ordering between detached sub-agent spawns — `spawn_agent(after=[...])` waits for prerequisite jobs, fails fast if one failed, and injects their reports into the dependent's prompt — then update the scraper-gen plugin to use it.

**Architecture:** A wrapper coroutine at the spawn-tool layer (spec Approach A): `JobRegistry` gains `await_settled(ids)` (no-timeout multi-wait that marks wake-consumed) and a `PrerequisiteFailed` exception; `spawn_agent` validates `after` ids at call time, refuses non-detached spawns, and registers a wrapper that waits → checks → appends "Results of prerequisite jobs" → lazily creates and awaits the real `run_background_agent` coroutine. No TUI/session/runner changes; waiting visibility comes from the job's `output_fn`.

**Tech Stack:** Python (asyncio), pytest + anyio. Prompt-markdown edits for the plugin task.

**Spec:** `docs/superpowers/specs/2026-07-02-spawn-after-dependencies-design.md`

## Global Constraints

- Use `uv` for everything: `uv run pytest ...`, `uv run ruff check src tests`, `uv run pyright`. Never bare python/pip.
- `requires-python >=3.10`: no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (imports sorted).
- CI order before claiming done: ruff → pyright → pytest.
- Injection never truncates: prerequisite report size is controlled by `max_output_chars` on the *dependency* spawns. Do not add any cap at injection time.
- `after` is background-only and validated before anything registers; unknown ids register nothing.
- The codebase favors long why-comments around invariants (resumability, cancellation) — write them where the plan's code blocks include them; don't strip them.
- This repo has concurrent agent sessions: `git add` only the files each task names, never `git add -A`.
- Every commit message ends with the two trailer lines (Co-Authored-By / Claude-Session) used by earlier commits in this feature.

---

### Task 1: `JobRegistry.await_settled` + `PrerequisiteFailed`

**Files:**
- Modify: `src/marim_harness/jobs.py` (add exception near top after `Status`; add method to `JobRegistry` after `wait`)
- Test: `tests/test_jobs.py` (append)

**Interfaces:**
- Produces: `class PrerequisiteFailed(RuntimeError)` at module level of `marim_harness.jobs`; `async def await_settled(self, ids: list[str]) -> list[Job]` on `JobRegistry` — blocks with **no timeout** until every id is terminal, returns `Job` objects in the order the ids were given, marks each id wake-consumed (digest preserved), raises `PrerequisiteFailed` if an id no longer exists, and re-raises `CancelledError` only when the *waiter* was cancelled (a cancelled dependency is returned as a settled Job, not raised). Task 2 consumes both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs.py` (match the file's existing async test idiom — it already uses anyio-style async tests; if it defines `pytestmark = pytest.mark.anyio`, these slot in as-is):

```python
async def _ev_job(ev: asyncio.Event, result: str = "ok") -> str:
    await ev.wait()
    return result


async def test_await_settled_orders_results_and_consumes_wake():
    reg = JobRegistry()
    e1, e2 = asyncio.Event(), asyncio.Event()
    j1 = reg.register("agent", "a", _ev_job(e1, "one"))
    j2 = reg.register("agent", "b", _ev_job(e2, "two"))
    waiter = asyncio.ensure_future(reg.await_settled([j2, j1]))
    await asyncio.sleep(0)
    assert not waiter.done()
    e1.set()
    e2.set()
    settled = await asyncio.wait_for(waiter, 5)
    # Order of the ids given, not completion/registration order.
    assert [j.id for j in settled] == [j2, j1]
    assert [j.result for j in settled] == ["two", "one"]
    assert all(j.status == "done" for j in settled)
    # Wake-consumed (no redundant autonomous turn) but digest preserved.
    assert reg.has_finished_pending() is False
    assert j1 in reg.take_finished_digest() and "done" in reg.output(j2)


async def test_await_settled_immediate_for_terminal_jobs():
    reg = JobRegistry()
    ev = asyncio.Event()
    jid = reg.register("agent", "a", _ev_job(ev, "early"))
    ev.set()
    await reg.wait(jid, 5)
    settled = await asyncio.wait_for(reg.await_settled([jid]), 1)
    assert settled[0].status == "done" and settled[0].result == "early"


async def test_await_settled_cancelled_waiter_leaves_job_running():
    reg = JobRegistry()
    ev = asyncio.Event()
    jid = reg.register("agent", "a", _ev_job(ev))
    waiter = asyncio.ensure_future(reg.await_settled([jid]))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert reg.get(jid).status == "running"  # the shield's guarantee
    ev.set()
    await reg.wait(jid, 5)


async def test_await_settled_returns_cancelled_dependency_as_settled():
    reg = JobRegistry()
    ev = asyncio.Event()
    jid = reg.register("agent", "a", _ev_job(ev))
    waiter = asyncio.ensure_future(reg.await_settled([jid]))
    await asyncio.sleep(0)
    await reg.cancel(jid)
    settled = await asyncio.wait_for(waiter, 5)
    assert settled[0].status == "cancelled"  # returned, not raised


async def test_await_settled_missing_id_raises_prerequisite_failed():
    reg = JobRegistry()
    with pytest.raises(PrerequisiteFailed):
        await reg.await_settled(["job-99"])
```

Add the needed imports at the top of the test file if absent: `import asyncio`, `import pytest`, and extend the existing `from marim_harness.jobs import ...` line with `PrerequisiteFailed`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_jobs.py -v -k await_settled`
Expected: FAIL at import time — `ImportError: cannot import name 'PrerequisiteFailed'`.

- [ ] **Step 3: Implement**

In `src/marim_harness/jobs.py`, after the `Status` alias (line ~28), add:

```python
class PrerequisiteFailed(RuntimeError):
    """A dependent background job's prerequisite settled failed/cancelled (or
    vanished), so the dependent never started. Raised by the spawn wrapper and
    formatted by the registry's done-callback into the job's ``failed`` result."""
```

In `JobRegistry`, directly after `wait` (line ~182), add:

```python
    async def await_settled(self, ids: list[str]) -> list[Job]:
        """Block until every job in ``ids`` reaches a terminal state, then return
        their ``Job`` objects in the order the ids were given. No timeout — a
        dependent job legitimately waits as long as its prerequisites run.

        Each id is marked wake-consumed exactly as :meth:`wait` does: the waiter
        is the consumer, so an intermediate completion in a chain must not fire a
        redundant autonomous wake (digest entries are preserved, so the model
        still sees the full chain history next turn).

        Cancellation is two-sided and must not be conflated: a *dependency*
        being cancelled settles it and is returned like any terminal state (the
        caller decides what a cancelled prerequisite means), while the *waiter*
        being cancelled re-raises so the wrapper job itself settles cancelled.
        The shield makes the waiter's cancellation leave the dependency running.
        """
        settled: list[Job] = []
        for jid in ids:
            job = self._jobs.get(jid)
            if job is None:
                # Spawn-time validation guarantees existence; a vanished id means
                # the registry was swapped/cleared out from under the chain.
                raise PrerequisiteFailed(f"prerequisite {jid} no longer exists")
            while job.status == "running":
                if job.task is None:
                    break  # registered but never scheduled; settle can't come
                try:
                    await asyncio.shield(job.task)
                except asyncio.CancelledError:
                    # Ambiguous by construction: shield raises CancelledError both
                    # when the dependency's task was cancelled and when *we* were.
                    # The dependency's task state disambiguates.
                    if not job.task.cancelled():
                        raise  # the waiter itself was cancelled — propagate
                except Exception:  # noqa: BLE001 — job failures settle via the
                    pass  # done-callback; status is read below, never the exc.
                # The done-callback that settles the job runs *after* the await
                # returns; yield once so status is terminal before we re-check
                # (otherwise this loop would spin on a done-but-unsettled task).
                await asyncio.sleep(0)
            self._wake_consumed.add(jid)
            settled.append(job)
        return settled
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_jobs.py -v`
Expected: PASS — the 5 new tests plus every pre-existing test in the file.

- [ ] **Step 5: Lint and commit**

Run: `uv run ruff check src/marim_harness/jobs.py tests/test_jobs.py` → `All checks passed!`

```bash
git add src/marim_harness/jobs.py tests/test_jobs.py
git commit -m "feat(jobs): await_settled multi-wait + PrerequisiteFailed for spawn chaining"
```

---

### Task 2: `spawn_agent(after=...)` — validation, wrapper, docstring

**Files:**
- Modify: `src/marim_harness/tools/provider.py` (`_coerce_mcp` rename ~line 516; `spawn_agent` signature/docstring/body ~lines 558–700)
- Test: `tests/test_subagent_after.py` (new)

**Interfaces:**
- Consumes: `JobRegistry.await_settled(ids) -> list[Job]`, `PrerequisiteFailed` (Task 1); existing `run_background_agent(type, task, mcp_names, budget, model, isolation, stream_id, depth) -> str` service signature; `jobs.register(kind, label, coro, *, output_fn=...)`.
- Produces: `spawn_agent(..., after: list[str] | str | None = None)`; module-level `async def _run_after(jobs, after_ids, task, start_inner, state) -> str`; `_coerce_names` (renamed from `_coerce_mcp`). Task 3's SKILL.md text relies on the tool-facing behavior exactly as specified here.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_subagent_after.py`:

```python
"""spawn_agent(after=...): harness-enforced ordering between detached spawns.

Tool-level tests drive the module function directly with a stub RunContext —
spawn_agent only reads ``ctx.deps`` and ``ctx.tool_call_id`` — and a fake
``run_background_agent`` service that records the tasks it was started with."""

import asyncio

import pytest

from marim_harness.runtime.deps import HarnessServices
from marim_harness.runtime.permissions import Mode
from marim_harness.tools.provider import spawn_agent
from tests.conftest import _make_deps

pytestmark = pytest.mark.anyio


class _Ctx:
    def __init__(self, deps):
        self.deps = deps
        self.tool_call_id = "tc-1"


def _fake_runner(calls: list, gate: asyncio.Event | None = None):
    """A run_background_agent stub. Records each started task; result echoes the
    task's first line so tests can tell whose report got injected where."""

    async def run(type, task, mcp_names, budget, model, isolation, stream_id, depth):
        if gate is not None:
            await gate.wait()
        calls.append(task)
        return f"report[{task.splitlines()[0]}]"

    return run


def _ctx(tmp_path, calls, gate=None, **deps_kw):
    deps = _make_deps(
        tmp_path, mode=Mode.auto,
        services=HarnessServices(run_background_agent=_fake_runner(calls, gate)),
        **deps_kw,
    )
    return _Ctx(deps)


def _job_id(spawn_result: str) -> str:
    # "Started job-N (agent) — <label>"
    assert spawn_result.startswith("Started job-"), spawn_result
    return spawn_result.split()[1]


async def test_dependent_waits_then_receives_injected_report(tmp_path):
    calls: list = []
    gate = asyncio.Event()
    ctx = _ctx(tmp_path, calls, gate)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    await asyncio.sleep(0)
    assert calls == []  # A gated, B waiting on A — neither inner run started
    assert f"(waiting on {a})" in ctx.deps.jobs.output(b)
    gate.set()
    await ctx.deps.jobs.wait(b, 5)
    assert len(calls) == 2
    assert calls[0].startswith("task A")
    assert calls[1].startswith("task B")
    assert "## Results of prerequisite jobs" in calls[1]
    assert "report[task A]" in calls[1]
    assert ctx.deps.jobs.get(b).status == "done"


async def test_multiple_prerequisites_injected_in_order(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(ctx, type="general", task="task B", background=True))
    c = _job_id(await spawn_agent(
        ctx, type="general", task="task C", background=True, after=[b, a]))
    await ctx.deps.jobs.wait(c, 5)
    task_c = calls[-1]
    assert task_c.startswith("task C")
    # Injection follows the order given in after=[b, a].
    assert task_c.index("report[task B]") < task_c.index("report[task A]")


async def test_failed_prerequisite_skips_dependent(tmp_path):
    calls: list = []

    async def run(type, task, mcp_names, budget, model, isolation, stream_id, depth):
        if task.startswith("task A"):
            raise RuntimeError("boom")
        calls.append(task)
        return "ok"

    deps = _make_deps(tmp_path, mode=Mode.auto,
                      services=HarnessServices(run_background_agent=run))
    ctx = _Ctx(deps)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    result = await ctx.deps.jobs.wait(b, 5)
    assert ctx.deps.jobs.get(b).status == "failed"
    assert "PrerequisiteFailed" in result and a in result
    assert calls == []  # the dependent's sub-agent was never started


async def test_cancelled_prerequisite_fails_dependent(tmp_path):
    calls: list = []
    gate = asyncio.Event()
    ctx = _ctx(tmp_path, calls, gate)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    await ctx.deps.jobs.cancel(a)
    await ctx.deps.jobs.wait(b, 5)
    assert ctx.deps.jobs.get(b).status == "failed"
    assert calls == []


async def test_cancelling_waiting_dependent_leaves_prerequisite_running(tmp_path):
    calls: list = []
    gate = asyncio.Event()
    ctx = _ctx(tmp_path, calls, gate)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    await ctx.deps.jobs.cancel(b)
    assert ctx.deps.jobs.get(b).status == "cancelled"
    assert ctx.deps.jobs.get(a).status == "running"
    gate.set()
    await ctx.deps.jobs.wait(a, 5)
    assert calls and calls[0].startswith("task A")


async def test_unknown_after_id_registers_nothing(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    out = await spawn_agent(
        ctx, type="general", task="task B", background=True, after="job-77")
    assert "job-77" in out and "no such job" in out
    assert ctx.deps.jobs.list() == []


async def test_after_refused_on_foreground_spawn(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    out = await spawn_agent(
        ctx, type="general", task="task B", background=False, after=a)
    assert "detached" in out and "wait_for_job" in out
    assert len(ctx.deps.jobs.list()) == 1  # only A


async def test_after_refused_at_depth(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    # Same registry as seen from inside a depth-1 sub-agent: `after` must be
    # refused there (background spawning is main-agent-only, so a depth>0 spawn
    # can never end up detached). The dep must exist so the unknown-id check
    # doesn't fire first and mask the refusal under test.
    ctx.deps.subagent_depth = 1
    out = await spawn_agent(ctx, type="general", task="task B", after=a)
    assert "detached" in out
    assert len(ctx.deps.jobs.list()) == 1  # only A; nothing new registered


async def test_chain_runs_strictly_in_order(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)
    a = _job_id(await spawn_agent(ctx, type="general", task="task A", background=True))
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=a))
    c = _job_id(await spawn_agent(
        ctx, type="general", task="task C", background=True, after=b))
    await ctx.deps.jobs.wait(c, 5)
    starts = [t.splitlines()[0] for t in calls]
    assert starts == ["task A", "task B", "task C"]
    # C sees B's report; A's report reaches C only inside B's injected text,
    # so C's own prerequisite section must reference job B, not job A.
    assert f"### {b} " in calls[2] and f"### {a} " not in calls[2]


async def test_bash_job_as_prerequisite(tmp_path):
    calls: list = []
    ctx = _ctx(tmp_path, calls)

    async def _bash() -> str:
        return "bash-output"

    bash_id = ctx.deps.jobs.register("bash", "ls -la", _bash())
    b = _job_id(await spawn_agent(
        ctx, type="general", task="task B", background=True, after=bash_id))
    await ctx.deps.jobs.wait(b, 5)
    assert "bash-output" in calls[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_after.py -v`
Expected: FAIL — `TypeError: spawn_agent() got an unexpected keyword argument 'after'`.

- [ ] **Step 3: Implement in `src/marim_harness/tools/provider.py`**

3a. Rename `_coerce_mcp` → `_coerce_names` (it was always generic normalization; `after` reuses it). Update its docstring's first line to `"""Normalize a name-list argument (mcp grant, after ids) into a list, or None.` and update the one existing call site (`mcp_names = _coerce_mcp(mcp)` → `mcp_names = _coerce_names(mcp)`).

3b. Add the wrapper helper at module level, near `_detach_handoff`:

```python
async def _run_after(
    jobs: "JobRegistry",
    after_ids: list[str],
    task: str,
    start_inner: "Callable[[str], Awaitable[str]]",
    state: dict,
) -> str:
    """Body of a dependent background job: wait for prerequisites, fail fast if
    any didn't succeed, then run the real sub-agent with their reports appended
    to its task.

    ``start_inner`` creates the inner run_background_agent coroutine *lazily* —
    the prompt can't be finalized until the prerequisites' reports exist, and an
    eagerly-created coroutine would leak un-awaited on a cancel-before-start
    (the same concern JobRegistry.register's docstring guards). ``state`` is
    shared with the job's output_fn so the jobs panel can show the waiting
    phase without a new job status."""
    settled = await jobs.await_settled(after_ids)
    bad = next((j for j in settled if j.status != "done"), None)
    if bad is not None:
        tail = " ".join((bad.result or "").split())[-160:]
        raise PrerequisiteFailed(
            f"prerequisite {bad.id} {bad.status}" + (f" — {tail}" if tail else "")
        )
    sections = [
        f"### {j.id} — {j.label}\n{j.result or '(no output)'}" for j in settled
    ]
    full_task = task + "\n\n## Results of prerequisite jobs\n\n" + "\n\n".join(sections)
    state["waiting"] = False
    return await start_inner(full_task)
```

Imports: extend the existing `from ..jobs import ...` (or add one) so `JobRegistry` (type-checking only, if not already imported) and `PrerequisiteFailed` are available; `Awaitable`/`Callable` come from `collections.abc` (extend the existing import).

3c. In `spawn_agent`, add the parameter after `mcp`:

```python
    after: "list[str] | str | None" = None,
```

3d. Append to the docstring, after the `mcp` paragraph (this is model-facing product copy — keep it):

```
    `after` names background job ids (earlier detached spawns or bash jobs) that
    must finish before this spawn starts — use it to chain dependent work, e.g. a
    merge step after the jobs producing its inputs. It requires a detached spawn
    (`background=True`, or auto-detach). The prerequisites' final reports are
    appended to this sub-agent's task under "Results of prerequisite jobs"; size
    them with `max_output_chars` on the *prerequisite* spawns — injection never
    truncates. If a prerequisite fails or is cancelled, this job fails without
    starting (zero tokens spent) and the failure surfaces in the jobs digest.
```

3e. In the body: coerce alongside mcp (`after_ids = _coerce_names(after)` right after `mcp_names = _coerce_names(mcp)`), then insert validation between the `auto_detached = (...)` computation and the `if background or auto_detached:` branch:

```python
    if after_ids is not None:
        unknown = [jid for jid in after_ids if ctx.deps.jobs.get(jid) is None]
        if unknown:
            return (
                f"Cannot spawn with after={unknown}: no such job(s). "
                "after only accepts ids of already-started background jobs "
                "(see the jobs panel or the digest for valid ids)."
            )
        if not (background or auto_detached):
            return (
                "after= requires a detached spawn. Pass background=True (top-level "
                "agent only), or drop after and wait_for_job the prerequisite "
                "before a foreground spawn."
            )
```

3f. In the `if background or auto_detached:` branch, replace the single `job_id = ctx.deps.jobs.register(...)` call with:

```python
        if after_ids:
            state = {"waiting": True}
            waiting_note = f"(waiting on {', '.join(after_ids)})"

            def _waiting_output() -> str:
                return waiting_note if state["waiting"] else "(still running)"

            def _start_inner(full_task: str) -> "Awaitable[str]":
                return ctx.deps.services.run_background_agent(
                    type, full_task, mcp_names, budget, model, isolation,
                    ctx.tool_call_id or "", ctx.deps.subagent_depth,
                )

            job_id = ctx.deps.jobs.register(
                "agent", label,
                _run_after(ctx.deps.jobs, after_ids, task, _start_inner, state),
                output_fn=_waiting_output,
            )
        else:
            job_id = ctx.deps.jobs.register(
                "agent", label,
                ctx.deps.services.run_background_agent(
                    type, task, mcp_names, budget, model, isolation,
                    ctx.tool_call_id or "", ctx.deps.subagent_depth,
                ),
            )
```

(The `else` branch is the existing call, unchanged — keep the existing comment above it about preferring `description` for the label.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagent_after.py tests/test_jobs.py tests/test_jobs_tools.py tests/test_subagent_tool.py -v`
Expected: PASS — all new tests plus the neighboring job/spawn suites (the `_coerce_names` rename and register-call reshuffle must not disturb them).

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src tests` → `All checks passed!`
Run: `uv run pyright` → `0 errors`

```bash
git add src/marim_harness/tools/provider.py tests/test_subagent_after.py
git commit -m "feat(subagents): spawn_agent after= — harness-enforced spawn dependencies"
```

---

### Task 3: scraper-gen plugin — `after=` chains, `depends_on`/`derive`, same-host cap, polish

**Files:**
- Modify: `examples/scraper-gen/skills/web-scrapers/SKILL.md`
- Modify: `examples/scraper-gen/agents/planner.md`
- Modify: `examples/scraper-gen/agents/generator.md`
- Modify: `examples/scraper-gen/agents/healer.md`

**Interfaces:**
- Consumes: the tool behavior from Task 2 exactly (`background=True` spawns return `Started job-N …`; `after=[ids]` holds a spawn and injects prerequisite reports).
- Produces: prompt-only changes. Frontmatter (`tools:`/`name:`/`description:`) must NOT change — `tests/test_examples_scraper_gen_plugin.py` pins it.

All edits below are exact old→new replacements. Read each file first; the old strings are verbatim from the current files (post-commit `3d6342c`).

- [ ] **Step 1: planner.md — `derive` strategy + `depends_on` field**

In `examples/scraper-gen/agents/planner.md`, in the plan-format block, replace:

```
- strategy: http | api | browser | blocked
```

with:

```
- strategy: http | api | browser | derive | blocked
- depends_on: [<task-name>, ...]   # optional — only for derive tasks
```

And after the plan-format code block's closing fence, insert this paragraph before "One `## Task:` block per scraper.":

```
A **derive** task is pure post-processing — merge, join, or enrich the output
of other tasks. It fetches nothing: its inputs are the sample files
(`scrapers/samples/<task>.jsonl`) of the tasks named in `depends_on`, and its
`fields`/`min_records` validate the derived records the same way. Use it
whenever the user asks for combined or cross-referenced data; never fold a
merge into a scraping task. `depends_on` may only name tasks defined in this
plan.
```

- [ ] **Step 2: planner.md — sharpen the browser-server note in SKILL.md Step 0**

In `examples/scraper-gen/skills/web-scrapers/SKILL.md`, replace:

```
- Check the MCP servers enabled in this session (the MCP-servers index lists
  them). If one is a browser-automation server (Playwright MCP or similar),
  remember its name — you will grant it to the planner. If none, proceed
```

with:

```
- Check the MCP servers enabled in this session (the MCP-servers index lists
  them). If one is a browser-automation server — pick one whose tools include
  `browser_navigate`/`browser_snapshot`, preferring a general-purpose server
  (e.g. `playwright`) over the `playwright_test` run-test server — remember
  its name: you will grant it to the planner. If none, proceed
```

- [ ] **Step 3: SKILL.md — rewrite Step 3 (dependencies + same-host cap)**

Replace the entire `## Step 3 — generate, fan out` section (from its heading up to, not including, `## Step 4 — heal`) with:

```
## Step 3 — generate, fan out with dependencies

One generator spawn per non-blocked task; never batch two tasks into one
spawn. Each spawn's task is the full task block pasted verbatim plus the
plan's header lines (base_url, robots, politeness), with
`returns="script path, final exit code, record count, deviations from plan"`.

How you spawn depends on the plan:

- **No `depends_on` anywhere:** spawn all generators in one turn (leave
  `background` unset; that already runs them in parallel).
- **Any task has `depends_on`:** spawn every generator as a background job
  (`background=True`) so each has a job id, and give each dependent task
  `after=[<job ids of the generators for the tasks it depends on>]` — the
  harness holds it until its inputs exist and injects the prerequisites'
  reports into its prompt. Then end your turn (reports arrive in the digest)
  or `wait_for_job` the terminal jobs.

**Same-host politeness:** each script sleeps between its own requests, but
that guarantee is per-script — N parallel generators against one host is
still ~N requests/second plus their fix-and-rerun cycles. Tasks on
*different* hosts (and `derive` tasks, which never touch the network) fan
out freely; for tasks on the *same* host run at most 2–3 generators at a
time (spawn in waves), and if any generator reports 429/rate-limit
responses, drop to fully sequential for that host. A rate-limited site makes
generators "fix" working selectors and later trips the healer's pass-twice
rule into false flakiness.
```

- [ ] **Step 4: generator.md — `depends_on`/`derive` handling**

In `examples/scraper-gen/agents/generator.md`, replace:

```
**Task block fields you receive:** `script`, `strategy` (http|api|browser),
```

with:

```
**Task block fields you receive:** `script`, `strategy`
(http|api|browser|derive), optional `depends_on`,
```

And insert, after the "Script conventions" list (before the skeleton intro line "Skeleton to follow"):

```
**For `strategy: derive`:** the script reads its input records from the
dependency tasks' sample files (`scrapers/samples/<task>.jsonl` for each task
in `depends_on`) and never touches the network — no httpx client, no delays
needed. If an input file is missing or empty, print which one to stderr and
exit 2: a mis-ordered run must fail loudly, never write an empty merge that
"passes". Your spawner injects the dependency generators' reports under
"## Results of prerequisite jobs" — use them for expected record counts and
any deviations they made from the plan.
```

- [ ] **Step 5: healer.md — dependency-ordered runs + path polish**

In `examples/scraper-gen/agents/healer.md`, replace:

```
1. Read `specs/plan.md` for each task's strategy, schema, and `min_records`.
```

with:

```
1. Read `scrapers/specs/plan.md` for each task's strategy, schema,
   `min_records`, and `depends_on`.
```

And replace:

```
2. Run every `scrape_*.py`: `cd scrapers && uv run python <script> --limit 10 --out
   samples/<task>.jsonl`. Where the task has pagination, vary the entry
```

with:

```
2. Run every `scrape_*.py` in dependency order — tasks named in another
   task's `depends_on` first, `derive` scripts last — so downstream scripts
   always heal against fresh upstream samples:
   `cd scrapers && uv run python <script> --limit 10 --out
   samples/<task>.jsonl`. Where the task has pagination, vary the entry
```

(If the current file's line wrapping differs slightly from the old strings above, match on the full sentence and preserve the file's wrapping style.)

- [ ] **Step 6: SKILL.md — remaining polish (bare read path, snapshot label)**

Replace:

```
Read the returned `specs/plan.md` yourself, then show the user a short
```

with:

```
Read the returned `scrapers/specs/plan.md` yourself, then show the user a short
```

And replace:

```
- Diff `find scrapers -type f | sort` against the Step 2 snapshot; flag any
```

with:

```
- Diff `find scrapers -type f | sort` against the earlier file snapshot
  (taken in Step 2, or in Step 1 when entering in repair mode); flag any
```

- [ ] **Step 7: Verify and commit**

Run: `uv run pytest --no-cov tests/test_examples_scraper_gen_plugin.py -v`
Expected: PASS (3 passed — frontmatter untouched).
Run: `uv run marim plugin validate examples/scraper-gen`
Expected: `valid: scraper-gen (0.1.0) — 1 skills, 3 agents, 0 hooks, 0 MCP servers`
Reread each edited section in full for internal consistency (the plan-format block, Step 3, and the derive paragraph must tell one coherent story).

```bash
git add examples/scraper-gen/skills/web-scrapers/SKILL.md examples/scraper-gen/agents/planner.md examples/scraper-gen/agents/generator.md examples/scraper-gen/agents/healer.md
git commit -m "feat(examples): scraper-gen uses after= chains; derive tasks, same-host cap, polish"
```

---

### Task 4: Full verification sweep

**Files:** none; verification only.

- [ ] **Step 1: Lint** — `uv run ruff check src tests` → `All checks passed!`
- [ ] **Step 2: Type-check** — `uv run pyright` → `0 errors, 0 warnings`
- [ ] **Step 3: Full suite** — `uv run pytest` → all pass (2265+ passed; coverage ≥ 90%).
- [ ] **Step 4: Clean-tree check** — `git status --porcelain src/marim_harness/jobs.py src/marim_harness/tools/provider.py examples/scraper-gen tests/test_jobs.py tests/test_subagent_after.py` → empty.

---

## Out of scope (explicitly)

- No `waiting` job status / TUI glyph (spec Approach B — deferred until panel UX warrants it).
- No re-capping of injected reports (sized via `max_output_chars` on the prerequisite spawns).
- No `after` support for foreground spawns or sub-agent (depth > 0) spawns.
- No persistence of dependency edges — jobs remain in-memory and session-scoped.
