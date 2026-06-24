# Detached Fan-Out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A multi-spawn fan-out returns control to the user immediately and is synthesized in a later autonomous-wake turn, instead of freezing the session for minutes.

**Architecture:** Reuse the existing background-job + autonomous-wake machinery. When detach mode is on *and* the session is interactive, `spawn_agent` routes to a background job and returns an informational handoff so the agent can end its turn (wake synthesizes) or `wait_for_job` inline. Two supporting changes: gate the synthesis wake on "no jobs still running" (so an N-way fan-out wakes once, not N times), and inline finished agent-job reports into the digest (so synthesis needs no extra `job_output` round-trips).

**Tech Stack:** Python 3.10+, Pydantic AI 1.107, Textual, pytest. Spec: `docs/superpowers/specs/2026-06-24-detached-fanout-design.md`.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright src`. Never bare `python`/`pip`/`pytest`.
- Ruff line length 100; lint set `E,F,I` (import sorting enforced).
- `requires-python >=3.10` — no 3.11+-only syntax.
- CI order (match locally before claiming done): ruff → pyright → pytest. Coverage is on by default; use `--no-cov` only for fast single-test runs.
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File Structure

- `src/marim_harness/config/model.py` — add `ModelConfig.detach_fanout` + env parse (Task 1).
- `src/marim_harness/deps.py` — add `Deps.detach_fanout` and `Deps.interactive` (Task 1).
- `src/marim_harness/bootstrap.py` — pass `detach_fanout` into `Deps` (Task 1).
- `src/marim_harness/agent.py` — `bind_ui` sets `deps.interactive = True` (Task 1).
- `src/marim_harness/tools/provider.py` — `spawn_agent` auto-detach + handoff note (Task 2).
- `src/marim_harness/jobs.py` — `any_running()`; inline agent reports in `take_finished_digest` (Tasks 3, 4).
- `src/marim_harness/interfaces/tui/wake.py` — `should_wake` gains an all-settled gate (Task 3).
- `src/marim_harness/interfaces/tui/app.py` — pass the gate at the call site (Task 3).
- Tests: `tests/test_config.py`, `tests/test_detach_fanout.py` (new), `tests/test_wake.py` (existing — extend), `tests/test_jobs.py` (existing — extend).

---

### Task 1: Config + interactive flag

**Files:**
- Modify: `src/marim_harness/config/model.py` (ModelConfig field + `load_config`)
- Modify: `src/marim_harness/deps.py` (two fields)
- Modify: `src/marim_harness/bootstrap.py:48` (Deps construction)
- Modify: `src/marim_harness/agent.py` (`bind_ui`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ModelConfig.detach_fanout: bool` (default `True`); `Deps.detach_fanout: bool` (default `False`), `Deps.interactive: bool` (default `False`). `spawn_agent` (Task 2) reads `ctx.deps.detach_fanout` and `ctx.deps.interactive`.

- [ ] **Step 1: Write the failing config test**

In `tests/test_config.py`, after `test_subagent_concurrency_defaults_to_unbounded`:

```python
def test_detach_fanout_defaults_on(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_DETACH_FANOUT", raising=False)
    assert load_config().detach_fanout is True


def test_detach_fanout_opt_out(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_DETACH_FANOUT", "0")
    assert load_config().detach_fanout is False
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_config.py::test_detach_fanout_defaults_on -q --no-cov`
Expected: FAIL — `AttributeError: 'ModelConfig' object has no attribute 'detach_fanout'`.

- [ ] **Step 3: Add the ModelConfig field**

In `src/marim_harness/config/model.py`, after the `wake_depth_cap: int = 3` field (~line 49):

```python
    # Detached fan-out (interactive only): when on, spawn_agent runs detached as a
    # background job so a fan-out doesn't freeze the session; autonomous wake
    # synthesizes the reports. Default on; MARIM_DETACH_FANOUT=0 forces inline.
    detach_fanout: bool = True
```

- [ ] **Step 4: Parse the env var in load_config**

In `load_config`, after the `subagent_concurrency` lines:

```python
    detach_fanout = _bool_env("MARIM_DETACH_FANOUT", True)
```

Then add to the `common` dict (next to `subagent_concurrency=subagent_concurrency,`):

```python
        detach_fanout=detach_fanout,
```

- [ ] **Step 5: Run config tests, verify pass**

Run: `uv run pytest tests/test_config.py -q --no-cov`
Expected: PASS.

- [ ] **Step 6: Add the Deps fields**

In `src/marim_harness/deps.py`, after the `mode: Mode = Mode.ask` field:

```python
    # Detached fan-out routing. detach_fanout is the config knob; interactive is
    # set True only when a UI is attached (bind_ui) — both required before
    # spawn_agent auto-detaches, since headless has no wake loop to synthesize.
    detach_fanout: bool = False
    interactive: bool = False
```

- [ ] **Step 7: Thread it from config into Deps**

In `src/marim_harness/bootstrap.py`, extend the `Deps(...)` construction (line 48):

```python
    deps = Deps(
        workspace_root=workspace,
        mode=mode,
        command_policy=command_policy,
        hooks=hook_runner,
        notifier=notifier,
        detach_fanout=cfg.detach_fanout,
    )
```

- [ ] **Step 8: Set interactive=True in bind_ui**

In `src/marim_harness/agent.py`, inside `bind_ui`, add at the start of the assignment block (before `self.deps.request_approval = request_approval`):

```python
        # A UI is attached → this session has a wake loop, so detached fan-out is
        # safe to activate (headless never calls bind_ui and stays inline).
        self.deps.interactive = True
```

- [ ] **Step 9: Full gate**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest tests/test_config.py -q --no-cov`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/config/model.py src/marim_harness/deps.py src/marim_harness/bootstrap.py src/marim_harness/agent.py tests/test_config.py
git commit -m "feat(detach): MARIM_DETACH_FANOUT config + Deps.detach_fanout/interactive

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: spawn_agent auto-detach + handoff note

**Files:**
- Modify: `src/marim_harness/tools/provider.py` (`spawn_agent`, ~308–389; add `_detach_handoff` helper)
- Test: `tests/test_detach_fanout.py` (new)

**Interfaces:**
- Consumes: `Deps.detach_fanout`, `Deps.interactive` (Task 1); existing `ctx.deps.services.run_background_agent`, `ctx.deps.jobs.register`.
- Produces: `spawn_agent(..., background: bool | None = None)`. When `background is None` and both deps flags are set, the spawn detaches and returns a handoff string starting with `"Started detached sub-agent"`. Explicit `background=True` keeps the old `"Started <id> (agent) — …"`; `background=False` forces inline.

- [ ] **Step 1: Write the failing test**

Create `tests/test_detach_fanout.py`:

```python
"""Detached fan-out: spawn_agent auto-routes to a background job when the
detach-fanout mode is on and the session is interactive."""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from tests.conftest import _last_instructions, _make_harness


def _spawn_once_model() -> FunctionModel:
    """Main agent: emit one spawn_agent (background omitted), then finish."""
    def fn(messages, info):
        if "sub-agent" in _last_instructions(messages):
            return ModelResponse(parts=[TextPart(content="SUB")])
        for m in messages:
            for p in getattr(m, "parts", []):
                if type(p).__name__ == "ToolReturnPart" and \
                        getattr(p, "tool_name", "") == "spawn_agent":
                    return ModelResponse(parts=[TextPart(content="done")])
        return ModelResponse(parts=[ToolCallPart(
            tool_name="spawn_agent", args={"type": "explore", "task": "look"})])
    return FunctionModel(fn)


@pytest.mark.anyio
async def test_detach_mode_routes_spawn_to_background(tmp_path: Path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_spawn_once_model(), deps)
    harness.deps.detach_fanout = True
    harness.deps.interactive = True
    await harness.run_turn("go")
    # A background job was registered (not run inline) ...
    assert len(harness.deps.jobs.list()) == 1
    # ... and the tool returned the detached handoff, visible in history.
    blob = "".join(
        str(p.content)
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "ToolReturnPart"
    )
    assert "Started detached sub-agent" in blob


@pytest.mark.anyio
async def test_inline_when_not_interactive(tmp_path: Path):
    """detach_fanout on but no UI attached (headless) → spawn runs inline."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_spawn_once_model(), deps)
    harness.deps.detach_fanout = True
    harness.deps.interactive = False
    await harness.run_turn("go")
    assert harness.deps.jobs.list() == []
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_detach_fanout.py -q --no-cov`
Expected: FAIL — no job registered (currently `background` defaults `False` → inline), so `len(...jobs.list()) == 1` fails.

- [ ] **Step 3: Add the handoff helper**

In `src/marim_harness/tools/provider.py`, above `async def spawn_agent`:

```python
def _detach_handoff(job_id: str) -> str:
    """The return for an auto-detached spawn: tell the agent it's running in the
    background and that it may end its turn (wake will deliver the report) or wait."""
    return (
        f"Started detached sub-agent {job_id}, running in the background "
        f"(concurrency-capped). End your turn to let it run — its report will be "
        f"delivered to you when it finishes — or wait_for_job(\"{job_id}\") if you "
        f"need the result in this turn. For a fan-out, ending the turn is better."
    )
```

- [ ] **Step 4: Make `background` tri-state and add the auto-detach branch**

In `spawn_agent`, change the signature parameter `background: bool = False` to:

```python
    background: bool | None = None,
```

Replace the `if background:` block (provider.py ~373) with:

```python
    auto_detached = (
        background is None and ctx.deps.detach_fanout and ctx.deps.interactive
    )
    if background or auto_detached:
        if ctx.deps.services.run_background_agent is None:
            return "Background sub-agents are not available in this context."
        label = f"{type}: {task}"
        job_id = ctx.deps.jobs.register(
            "agent", label,
            ctx.deps.services.run_background_agent(
                type, task, mcp_names, max_output_chars, model, isolation
            ),
        )
        if auto_detached:
            return _detach_handoff(job_id)
        return f"Started {job_id} (agent) — {label[:60]}"
```

(The inline fallthrough below it is unchanged.)

- [ ] **Step 5: Update the `background` docstring line**

In the `spawn_agent` docstring, replace the sentence describing `background=True` with:

```
    Set `background=True` to force a detached job (returns a job id immediately);
    `background=False` forces an inline run. Left unset, a spawn auto-detaches when
    detached-fanout mode is on and the session is interactive — it returns a job
    handle and you choose to end your turn (its report is delivered later) or
    wait_for_job for it inline.
```

- [ ] **Step 6: Run, verify pass**

Run: `uv run pytest tests/test_detach_fanout.py -q --no-cov`
Expected: PASS (both tests).

- [ ] **Step 7: Regression — existing spawn tests**

Run: `uv run pytest tests/test_subagent_tool.py tests/test_agent_subagents.py -q --no-cov`
Expected: PASS (default `None` behaves as inline when flags unset, matching old `False`).

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/tools/provider.py tests/test_detach_fanout.py
git commit -m "feat(detach): spawn_agent auto-detaches under interactive detach mode

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wake fires only when all jobs are settled

**Files:**
- Modify: `src/marim_harness/jobs.py` (`any_running`)
- Modify: `src/marim_harness/interfaces/tui/wake.py` (`should_wake`)
- Modify: `src/marim_harness/interfaces/tui/app.py:337` (call site)
- Test: `tests/test_wake.py`, `tests/test_jobs.py`

**Interfaces:**
- Produces: `JobRegistry.any_running() -> bool`; `WakeController.should_wake(*, enabled, turn_busy, has_finished_pending, all_jobs_settled)`.

- [ ] **Step 1: Write the failing wake test**

`tests/test_wake.py` defines `_READY = dict(enabled=True, turn_busy=False, has_finished_pending=True)` and spreads it into existing tests. Since Step 3 adds a **required** kwarg, first update `_READY` so the existing tests keep passing:

```python
_READY = dict(enabled=True, turn_busy=False, has_finished_pending=True,
              all_jobs_settled=True)
```

Then add the two new tests:

```python
def test_does_not_wake_while_a_job_is_still_running():
    wc = WakeController(depth_cap=3)
    # A finished job is pending, but another is still running → hold off.
    assert wc.should_wake(
        enabled=True, turn_busy=False,
        has_finished_pending=True, all_jobs_settled=False,
    ) is False


def test_wakes_once_all_jobs_settled():
    wc = WakeController(depth_cap=3)
    assert wc.should_wake(
        enabled=True, turn_busy=False,
        has_finished_pending=True, all_jobs_settled=True,
    ) is True
```

(Add `from marim_harness.interfaces.tui.wake import WakeController` if not already imported.)

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_wake.py::test_does_not_wake_while_a_job_is_still_running -q --no-cov`
Expected: FAIL — `TypeError: should_wake() got an unexpected keyword argument 'all_jobs_settled'`.

- [ ] **Step 3: Add the gate to should_wake**

In `src/marim_harness/interfaces/tui/wake.py`, change `should_wake`:

```python
    def should_wake(
        self, *, enabled: bool, turn_busy: bool, has_finished_pending: bool,
        all_jobs_settled: bool,
    ) -> bool:
        """True iff an idle TUI should fire one autonomous digest turn now: wake
        is enabled, no turn is in flight, the depth cap is not yet reached, a
        finished-job digest is pending, and no job is still running (so an N-way
        detached fan-out wakes once, after the whole batch, not per completion).
        A pure predicate — it never mutates."""
        return (
            enabled
            and not turn_busy
            and self._depth < self._depth_cap
            and has_finished_pending
            and all_jobs_settled
        )
```

- [ ] **Step 4: Add JobRegistry.any_running**

In `src/marim_harness/jobs.py`, near `has_finished_pending` (~line 224):

```python
    def any_running(self) -> bool:
        """True if any job is still in the ``running`` state."""
        return any(j.status == "running" for j in self._jobs.values())
```

- [ ] **Step 5: Write the failing jobs test**

In `tests/test_jobs.py`:

```python
def test_any_running_reflects_live_jobs():
    reg = JobRegistry()
    assert reg.any_running() is False
```

(Extend with a running job using the file's existing job-registration helper if one exists; otherwise this no-job assertion plus the wake tests cover the predicate.)

- [ ] **Step 6: Pass the gate at the call site**

In `src/marim_harness/interfaces/tui/app.py`, the `should_wake(...)` call (~line 337):

```python
        if not self._wake.should_wake(
            enabled=self.autonomous_wake,
            turn_busy=self.turn_busy,
            has_finished_pending=self.harness.deps.jobs.has_finished_pending(),
            all_jobs_settled=not self.harness.deps.jobs.any_running(),
        ):
            return
```

- [ ] **Step 7: Run, verify pass**

Run: `uv run pytest tests/test_wake.py tests/test_jobs.py -q --no-cov`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/jobs.py src/marim_harness/interfaces/tui/wake.py src/marim_harness/interfaces/tui/app.py tests/test_wake.py tests/test_jobs.py
git commit -m "feat(detach): wake only when all jobs settled (no premature synthesis)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Inline finished agent reports into the digest

**Files:**
- Modify: `src/marim_harness/jobs.py` (`take_finished_digest`)
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: existing `Job.kind`, `Job.status`, `Job.result`.
- Produces: a digest where a finished `agent` job inlines its full `result` instead of only a tail (bash jobs keep the tail). Consumed unchanged by `_assemble_prompt` (`agent.py:629`).

- [ ] **Step 1: Write the failing test**

`tests/test_jobs.py` already has an `async def _settled(reg, *, tries=400)` helper that polls until no job is running — use it (do **not** use `wait()`, which marks the job wake-consumed and empties the digest). Add:

```python
@pytest.mark.anyio
async def test_digest_inlines_full_agent_report():
    reg = JobRegistry()

    async def _work():
        return "LINE1\nLINE2\nFULL-REPORT-BODY-VERDICT"

    reg.register("agent", "explore: x", _work())
    await _settled(reg)
    digest = reg.take_finished_digest()
    assert "FULL-REPORT-BODY-VERDICT" in digest        # full body, not just tail
    assert "full report" in digest
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_jobs.py::test_digest_inlines_full_agent_report -q --no-cov`
Expected: FAIL — digest contains only the trailing tail, not the full multi-line body.

- [ ] **Step 3: Inline agent reports in take_finished_digest**

In `src/marim_harness/jobs.py`, change the build loop inside `take_finished_digest`:

```python
        parts = []
        for jid in ids:
            job = self._jobs.get(jid)
            if job is None:
                continue
            if job.kind == "agent" and job.status == "done" and job.result:
                # Inline the whole report so the synthesis turn needs no extra
                # job_output round-trips. Size is bounded upstream by the spawn's
                # max_output_chars cap (already applied before the result lands).
                parts.append(
                    f"{job.id} ({job.kind}) {job.status} — full report:\n{job.result}"
                )
            else:
                parts.append(
                    f"{job.id} ({job.kind}) {job.status}{self._digest_tail(job)}"
                )
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_jobs.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/jobs.py tests/test_jobs.py
git commit -m "feat(detach): inline finished agent reports in the jobs digest

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Full-suite gate

- [ ] **Step 1: Run the whole CI sequence**

Run: `uv run ruff check src tests && uv run pyright src && uv run pytest -q`
Expected: ruff clean, pyright 0 errors, all tests pass, coverage ≥90%.

- [ ] **Step 2: Fix any fallout, then commit if anything changed**

If a pre-existing test asserted `spawn_agent`'s old `background=False` default signature or the old digest tail-only format, update it to the new behavior and commit:

```bash
git add -A
git commit -m "test(detach): update fixtures for tri-state background + inlined digest

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §1 Trigger & config (detach-all, default-on, interactive-gated, `background=False` forces inline) → Tasks 1 (config/flags) + 2 (tri-state + auto-detach).
- §2 Informed handoff → Task 2 (`_detach_handoff`).
- §3 Synthesis inlining → Task 4.
- §4 Premature-synthesis fix (no jobs running) → Task 3.
- §5 Config wiring + cap/resume composition → Task 1; composition is automatic (reuses `run_background_agent`, no code).
- §6 Testing → tests in Tasks 1–4 cover detach trigger, default-on/opt-out, interactive gate, wake gating, agent-choice (`wait_for_job` is the existing tool, exercised implicitly), config threading.

**Placeholder scan:** No TBD/TODO. The one soft spot — Task 4 Step 1's test note about `wait()` consuming the digest — is explicitly flagged with a concrete fallback (poll status, then `take_finished_digest`), not left vague.

**Type consistency:** `detach_fanout`/`interactive` bools consistent across `ModelConfig`/`Deps`. `background: bool | None`. `should_wake(... all_jobs_settled: bool)` matches the app call site. `any_running() -> bool`. `_detach_handoff(job_id: str) -> str` returns the `"Started detached sub-agent"` string asserted in Task 2.

**Note on the agent-choice path:** `wait_for_job` is unchanged existing code; no task needed. The handoff note steers the agent; reliability of the model ending vs. waiting is a prompt-behavior property, not a code path to test here.
