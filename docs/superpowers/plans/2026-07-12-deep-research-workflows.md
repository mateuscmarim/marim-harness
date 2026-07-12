# Deep-Research Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let workflows run long enough for real research (model-requested `timeout_secs` on `run_workflow`, clamped to a config ceiling) and upgrade the builtin deep-research skill to drive the workflow engine.

**Architecture:** The per-call timeout threads through the existing tool → `services.run_workflow` → `WorkflowEngine.run` seam as one new trailing parameter. The engine clamps it (`min(requested or 300, ceiling)`) and uses the effective value for both the Monty VM duration limit and the outer `asyncio.wait` — the separate `_MAX_VM_DURATION_SECS` constant disappears. The ceiling (`HarnessConfig.workflow_timeout_secs`, already plumbed to the engine) gets wired to a new `MARIM_WORKFLOW_TIMEOUT` env var in the CLI config path. The deep-research SKILL.md is rewritten workflow-first with a spawn_agent fallback, and a test compiles its embedded reference script as Monty.

**Tech Stack:** Python ≥3.10, pydantic-monty (sandbox; dev dependency, so tests may import it unconditionally), pytest + anyio, existing fakes (no real models).

**Spec:** `docs/superpowers/specs/2026-07-12-deep-research-workflows-design.md`

## Global Constraints

- Branch: `feat/deep-research-workflows` off master `73b48cc`. Create it before Task 1.
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pip`.
- Ruff line length 100; cyclomatic complexity cap 10 (C901) — extract helpers, never `# noqa: C901`.
- `requires-python >=3.10` — no 3.11+-only syntax.
- Coverage gate is on by default; keep it green (`uv run pytest` runs with coverage).
- Tests use TestModel/FunctionModel/fakes ONLY. Never call a real model provider.
- `git add` ONLY files this plan creates/modifies — never `git add -A` or `git add .`. The untracked `scratch-canary.md` at the repo root is NOT ours: never add, modify, or delete it.
- Every commit message ends with exactly these two lines:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN`
- Exact values: per-call default timeout **300.0s** (`DEFAULT_RUN_TIMEOUT_SECS`); ceiling default **1800.0s** (`DEFAULT_TIMEOUT_SECS`, `HarnessConfig.workflow_timeout_secs`); env var **`MARIM_WORKFLOW_TIMEOUT`** (integer seconds); clamp rule **`min(requested or 300, ceiling)`**.
- Preserve the engine's cancellation invariant: NEVER cancel the Monty VM task (module docstring in `engine.py`). Nothing in this plan touches the abort/drain path.

---

### Task 1: Engine — per-call timeout with clamping

Replace the fixed `_MAX_VM_DURATION_SECS` cap with a per-run effective timeout: `WorkflowEngine.run` gains `timeout_secs`, clamps it against the engine's configured ceiling, and uses the effective value for the VM duration limit, the outer wait, and the timeout message.

**Files:**
- Modify: `src/marim_harness/workflows/engine.py`
- Test: `tests/test_workflow_engine.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `WorkflowEngine.run(self, script: str, args: object, tool_call_id: str, timeout_secs: float | None = None) -> str`; pure helper `_effective_timeout(requested: float | None, ceiling: float) -> float`; constant `DEFAULT_RUN_TIMEOUT_SECS = 300.0`. `_MAX_VM_DURATION_SECS` no longer exists. Task 2 calls `run` with a 4th positional argument.

- [ ] **Step 1: Write the failing tests**

In `tests/test_workflow_engine.py`, add (near the existing VM-limit tests, ~line 384):

```python
def test_effective_timeout_clamps_to_ceiling():
    """Pure clamp rule: min(requested or 300, ceiling). Unit-tested directly
    because the 300s default is unobservable in a fast integration test."""
    from marim_harness.workflows.engine import _effective_timeout

    assert _effective_timeout(None, 1800.0) == 300.0      # omitted -> default
    assert _effective_timeout(60.0, 1800.0) == 60.0       # under ceiling -> honored
    assert _effective_timeout(9999.0, 1800.0) == 1800.0   # over ceiling -> clamped
    assert _effective_timeout(None, 10.0) == 10.0         # tiny ceiling clamps the default too


@pytest.mark.anyio
async def test_requested_timeout_over_ceiling_reports_the_clamped_value(tmp_path):
    """A clamped request must be visible: the timeout message reports the
    EFFECTIVE duration, not the requested one."""
    async def slow_spawn(*a, **kw):
        await asyncio.sleep(60)
        return "never"

    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=0.3)
    out = await eng.run('await agent("x")\n"done"', None, "tc1", timeout_secs=500.0)
    # 0.3 rendered through the message's {effective:.0f} formatting.
    assert "timed out after 0s" in out
    # The requested (unclamped) value must NOT appear.
    assert "500" not in out


@pytest.mark.anyio
async def test_requested_timeout_extends_past_the_default(tmp_path):
    """A run whose requested timeout exceeds the old 300s-style bound (scaled
    down here) survives, proving the per-call request really widens the VM
    duration limit and the outer wait together."""
    async def slow_spawn(*a, **kw):
        await asyncio.sleep(0.2)
        return "ok"

    # Ceiling 5.0; request 2.0. With the old fixed-cap behavior scaled to this
    # test, a 0.2s host call would have died under a smaller default.
    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=5.0)
    out = await eng.run('await agent("x")\n"done"', None, "tc1", timeout_secs=2.0)
    assert "done" in out and "timed out" not in out and "raised" not in out
```

Then update the three existing tests that monkeypatch the removed constant, and delete the obsolete one:

1. `test_infinite_loop_is_killed_by_vm_limits` (~line 329) — drop the monkeypatch; pass the bound per-call instead:

```python
@pytest.mark.anyio
async def test_infinite_loop_is_killed_by_vm_limits(tmp_path):
    eng, _ = _engine(tmp_path, _echo_spawn)
    out = await eng.run("while True:\n    pass", None, "tc1", timeout_secs=0.5)
    assert "Workflow script raised" in out
```

2. `test_cumulative_host_call_time_counts_toward_vm_duration_cap` (~line 338) — drop the monkeypatch; the requested timeout is now the wall-clock budget the docstring pins:

```python
@pytest.mark.anyio
async def test_cumulative_host_call_time_counts_toward_vm_duration_cap(tmp_path):
    """pydantic-monty's max_duration_secs is not compute-only: real
    wall-clock time spent awaiting host (agent()) calls counts too, once the
    interpreter regains control. This pins that behavior so a future
    pydantic-monty upgrade that silently changes it gets caught here rather
    than in a live workflow (see engine.py's module docstring)."""
    async def slow_spawn(*a, **kw):
        await asyncio.sleep(0.05)
        return "ok"

    eng, _ = _engine(tmp_path, slow_spawn, timeout_secs=5.0)
    script = "r = 0\nfor i in range(10):\n    r = await agent('x')\nr\n"
    out = await eng.run(script, None, "tc1", timeout_secs=0.2)
    assert "raised" in out or "timed out" in out
```

   (Note the loosened assertion: with the VM limit and the outer wait now
   equal, either can fire first — both are correct timeout behavior.)

3. `test_several_real_host_calls_under_budget_do_not_spuriously_time_out` (~line 358) — drop the monkeypatch; pass `timeout_secs=1.0` to `eng.run` instead (keep `timeout_secs=5.0` on `_engine`; keep the docstring and assertions unchanged).

4. **Delete** `test_vm_duration_cap_is_generous_enough_for_real_workflows` (~line 384) — the constant it pins no longer exists; the clamp tests above replace it.

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py -x -q`
Expected: FAIL — `ImportError: cannot import name '_effective_timeout'` (and TypeError on the 4-arg `run` calls).

- [ ] **Step 3: Implement the engine changes**

In `src/marim_harness/workflows/engine.py`:

a) Replace the `_MAX_VM_DURATION_SECS` constant block (lines ~60–69) with:

```python
# Per-call default when run() is not given a timeout: short enough that an
# ordinary sweep fails fast, long enough for a handful of real agent()
# calls. A caller doing genuinely long work (a multi-researcher deep-research
# run) requests more via timeout_secs, up to the engine's configured ceiling.
DEFAULT_RUN_TIMEOUT_SECS = 300.0
```

b) Add the pure helper right after `_script_title` (module level, unit-tested directly per the repo convention for pure decision helpers):

```python
def _effective_timeout(requested: float | None, ceiling: float) -> float:
    """The wall-clock budget one run actually gets: the caller's request
    (defaulting to DEFAULT_RUN_TIMEOUT_SECS) clamped to the engine's
    configured ceiling. Pure; unit-tested directly."""
    return min(requested if requested is not None else DEFAULT_RUN_TIMEOUT_SECS, ceiling)
```

c) Change `run`'s signature and use the effective value throughout:

```python
    async def run(self, script: str, args: object, tool_call_id: str,
                  timeout_secs: float | None = None) -> str:
```

Inside `run`, before building `vm_limits`, compute:

```python
        effective = _effective_timeout(timeout_secs, self._timeout)
```

Replace the `vm_limits` dict (lines ~178–186) with:

```python
        vm_limits: ResourceLimits = {
            # The VM's duration cap IS the run's effective timeout: it is the
            # only thing that can stop a non-yielding compute loop (the outer
            # asyncio.wait below can't preempt one — Monty holds the GIL and
            # direct cancellation is unsafe; see the module docstring), and
            # since it also counts host-await wall time it doubles as the
            # whole-script budget. Trade-off accepted with the per-call knob:
            # a spin loop can now burn up to the requested duration instead
            # of a fixed few minutes, bounded by the configured ceiling.
            "max_duration_secs": effective,
            "max_memory": _VM_MEMORY_LIMIT_BYTES,
        }
```

Replace `timeout=self._timeout` in the `asyncio.wait` call (~line 204) with `timeout=effective`, and the timeout message (~line 215) with:

```python
            outcome = (f"Workflow timed out after {effective:.0f}s; "
                       "in-flight sub-agents were cancelled.")
```

d) Update the stale prose:
   - Module docstring final paragraph (lines ~19–28): keep the empirical
     max_duration_secs finding but replace the trailing reference "see
     _MAX_VM_DURATION_SECS below" with "so it serves as the run's whole-script
     wall-clock budget — see `_effective_timeout`".
   - `WorkflowEngine.__init__`: document `timeout_secs` as the **ceiling**:

```python
    def __init__(self, deps: Deps, spawn, *, timeout_secs: float = DEFAULT_TIMEOUT_SECS):
        self.deps = deps
        self._spawn = spawn
        # The CEILING on any one run's wall-clock budget. Each run() call may
        # request its own timeout_secs (deep research legitimately needs 30
        # minutes); the request is clamped to this, and omitted requests get
        # DEFAULT_RUN_TIMEOUT_SECS. See _effective_timeout.
        self._timeout = timeout_secs
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_workflow_engine.py -q`
Expected: all PASS.

- [ ] **Step 5: Lint, type-check, full suite**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest -q`
Expected: clean. (`HarnessConfig.workflow_timeout_secs`'s comment in
`runtime/harness.py` lines 178–180 says "VM compute is separately bounded" —
update that comment now since it became false: replace with
`# Ceiling on the wall-clock budget any single run_workflow call may request;
# per-call requests are clamped to it (see workflows/engine.py).`)

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/workflows/engine.py src/marim_harness/runtime/harness.py tests/test_workflow_engine.py
git commit -m "feat(workflows): per-run timeout_secs clamped to the engine ceiling

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 2: Tool + seam — expose timeout_secs to the model

`run_workflow` gains the `timeout_secs` parameter, validates it, forwards it through the `WorkflowRunner` seam, and documents it in the model-facing docstring.

**Files:**
- Modify: `src/marim_harness/tools/workflow_tools.py`
- Modify: `src/marim_harness/runtime/deps.py:71` (the `WorkflowRunner` alias)
- Test: `tests/test_workflow_tool.py`

**Interfaces:**
- Consumes: `WorkflowEngine.run(script, args, tool_call_id, timeout_secs=None)` from Task 1 (the bound method is what `services.run_workflow` holds).
- Produces: `run_workflow(ctx, script: str, args: JsonValue = None, timeout_secs: float | None = None) -> str`; `WorkflowRunner = Callable[[str, object, str, float | None], Awaitable[str]]` (the 4th positional arg is the requested timeout). Task 4's skill text references this tool parameter.

- [ ] **Step 1: Write the failing tests**

In `tests/test_workflow_tool.py`, update the existing delegation test and add two:

```python
@pytest.mark.anyio
async def test_delegates_script_args_and_tool_call_id(tmp_path):
    deps = _make_deps(tmp_path)
    seen = {}

    async def fake_runner(script, args, tool_call_id, timeout_secs):
        seen.update(script=script, args=args, tool_call_id=tool_call_id,
                    timeout_secs=timeout_secs)
        return "result"

    deps.services.run_workflow = fake_runner
    out = await run_workflow(_ctx(deps, "abc"), "1 + 1", args={"k": 1})
    assert out == "result"
    assert seen == {"script": "1 + 1", "args": {"k": 1}, "tool_call_id": "abc",
                    "timeout_secs": None}


@pytest.mark.anyio
async def test_timeout_secs_is_forwarded(tmp_path):
    deps = _make_deps(tmp_path)
    seen = {}

    async def fake_runner(script, args, tool_call_id, timeout_secs):
        seen["timeout_secs"] = timeout_secs
        return "ok"

    deps.services.run_workflow = fake_runner
    await run_workflow(_ctx(deps), "1 + 1", timeout_secs=1800.0)
    assert seen["timeout_secs"] == 1800.0


@pytest.mark.anyio
async def test_invalid_timeout_is_rejected_without_running(tmp_path):
    """<=0 or non-finite timeouts are a model mistake: answer with a
    correctable error and never start the VM."""
    deps = _make_deps(tmp_path)

    async def fake_runner(*a):
        raise AssertionError("runner must not be called")

    deps.services.run_workflow = fake_runner
    for bad in (0.0, -5.0, float("inf"), float("nan")):
        out = await run_workflow(_ctx(deps), "1 + 1", timeout_secs=bad)
        assert "timeout_secs" in out and "positive" in out
```

Also extend the docstring guard test:

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
    assert "timeout_secs" in doc
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_workflow_tool.py -q`
Expected: FAIL — `run_workflow() got an unexpected keyword argument 'timeout_secs'` and the updated delegation test's 4-arg fake gets 3 args.

- [ ] **Step 3: Implement**

In `src/marim_harness/runtime/deps.py`, change line 71 (update the nearby comment to name the 4th element):

```python
# (script, args, tool_call_id, requested timeout_secs | None) -> tool result.
WorkflowRunner = Callable[[str, object, str, float | None], Awaitable[str]]
```

In `src/marim_harness/tools/workflow_tools.py`, add `import math` (stdlib, first import block), then:

```python
def _bad_timeout(timeout_secs: float | None) -> str | None:
    """A correctable-error message when the requested timeout is unusable,
    else None. Pure; keeps run_workflow itself thin."""
    if timeout_secs is None:
        return None
    if not math.isfinite(timeout_secs) or timeout_secs <= 0:
        return (f"Invalid timeout_secs={timeout_secs!r}: it must be a positive "
                "number of seconds. Omit it for the default, or request what "
                "the work needs (long multi-agent runs may use e.g. 1800).")
    return None
```

Change the signature and the seam call:

```python
async def run_workflow(
    ctx: RunContext[Deps], script: str, args: JsonValue = None,
    timeout_secs: float | None = None,
) -> str:
```

```python
    runner = ctx.deps.services.run_workflow
    if runner is None:
        return _UNAVAILABLE
    err = _bad_timeout(timeout_secs)
    if err is not None:
        return err
    return await runner(script, args, ctx.tool_call_id or "", timeout_secs)
```

Docstring changes (this is model-facing UI copy — insert verbatim):

1. After the `args` bullet (before the `asyncio.gather` bullet is fine; it is
   a tool parameter, not a script name, so place it as a new paragraph AFTER
   the bulleted name list instead), add:

```
    The tool's `timeout_secs` parameter is the run's wall-clock budget.
    Omit it for quick sweeps (default 300s). Request what the work needs
    when spawning agents that each take minutes — e.g. 1800 for a
    multi-researcher deep-research run. Requests are clamped to a
    harness-configured ceiling.
```

2. Replace the closing paragraph's first sentence
   ("Scripts are wall-clock bounded and aborted cleanly on interrupt.") with:

```
    Scripts are bounded by timeout_secs and aborted cleanly on interrupt.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_workflow_tool.py tests/test_workflow_engine.py tests/test_workflow_acceptance.py -q`
Expected: all PASS (`test_workflow_acceptance.py` wires `engine.run` straight into the seam; Task 1's default keeps it working).

- [ ] **Step 5: Lint, type-check, full suite**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest -q`
Expected: clean. pyright confirms the bound method `WorkflowEngine.run` still satisfies the widened `WorkflowRunner` alias.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/tools/workflow_tools.py src/marim_harness/runtime/deps.py tests/test_workflow_tool.py
git commit -m "feat(workflows): model-facing timeout_secs on run_workflow

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 3: Config wiring — MARIM_WORKFLOW_TIMEOUT

Wire the ceiling end to end for the CLI path: env var → `ModelConfig` → bootstrap override → `HarnessConfig` → engine. Embedders already reach it via `HarnessBuilder.with_config_overrides(workflow_timeout_secs=...)` — no builder change needed.

**Files:**
- Modify: `src/marim_harness/config/model.py` (field ~line 121 area; env parse ~line 215)
- Modify: `src/marim_harness/runtime/bootstrap.py:132` area (the `with_config_overrides` call)
- Modify: `.env.example` (~line 111, the "Dynamic workflows" section)
- Test: `tests/test_config.py`, `tests/test_workflow_wiring.py`

**Interfaces:**
- Consumes: `WorkflowEngine.__init__(..., timeout_secs=...)` ceiling semantics from Task 1; `HarnessConfig.workflow_timeout_secs` (exists).
- Produces: `ModelConfig.workflow_timeout_secs: float = 1800.0`; env var `MARIM_WORKFLOW_TIMEOUT` (integer seconds; invalid/≤0 → default). Task 5's smoke run may use it.

- [ ] **Step 1: Write the failing tests**

In `tests/test_config.py`, next to `test_workflows_env_gate` (~line 43):

```python
def test_workflow_timeout_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_WORKFLOW_TIMEOUT", "3600")
    cfg = load_config()
    assert cfg.workflow_timeout_secs == 3600.0


def test_workflow_timeout_defaults_and_rejects_garbage(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("MARIM_WORKFLOW_TIMEOUT", raising=False)
    assert load_config().workflow_timeout_secs == 1800.0
    for bad in ("banana", "0", "-5"):
        monkeypatch.setenv("MARIM_WORKFLOW_TIMEOUT", bad)
        assert load_config().workflow_timeout_secs == 1800.0
```

In `tests/test_workflow_wiring.py`:

```python
def test_harness_threads_workflow_timeout_to_the_engine(tmp_path):
    """The configured ceiling must reach the engine — services.run_workflow
    holds the bound method, so the engine is its __self__."""
    h: Harness = _make_harness(TestModel(), _make_deps(tmp_path),
                               workflow_timeout_secs=42.0)
    runner = h.deps.services.run_workflow
    assert runner is not None
    assert runner.__self__._timeout == 42.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_config.py tests/test_workflow_wiring.py -q`
Expected: FAIL — `ModelConfig` has no attribute `workflow_timeout_secs` (the wiring test may already pass via legacy kwargs → HarnessConfig; that's fine — it pins behavior).

- [ ] **Step 3: Implement**

In `src/marim_harness/config/model.py`, add a field to `ModelConfig` directly under `workflows_enabled` (~line 121):

```python
    # Ceiling (seconds) on the wall-clock budget a single run_workflow call
    # may request via its timeout_secs parameter. MARIM_WORKFLOW_TIMEOUT.
    workflow_timeout_secs: float = 1800.0
```

In the common-config `dict(...)` (~line 215), directly under the
`workflows_enabled=` line:

```python
        workflow_timeout_secs=float(_int_env("MARIM_WORKFLOW_TIMEOUT", 1800)),
```

(`_int_env` already logs-and-defaults on garbage and on values ≤ 0 — exactly
the spec's "garbage or missing → 1800".)

In `src/marim_harness/runtime/bootstrap.py`, inside `with_config_overrides(...)`
directly under `workflows_enabled=cfg.workflows_enabled,` (line 132):

```python
            workflow_timeout_secs=cfg.workflow_timeout_secs,
```

In `.env.example`, extend the "Dynamic workflows" section:

```
# --- Dynamic workflows ---
# Dynamic workflows (the run_workflow tool; needs the [workflows] extra). Default on.
# MARIM_WORKFLOWS=1
# Ceiling (seconds) on the wall-clock budget one run_workflow call may request
# via its timeout_secs parameter. Default 1800 (30 min).
# MARIM_WORKFLOW_TIMEOUT=1800
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_config.py tests/test_workflow_wiring.py -q`
Expected: all PASS.

- [ ] **Step 5: Lint, type-check, full suite**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest -q`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/config/model.py src/marim_harness/runtime/bootstrap.py .env.example tests/test_config.py tests/test_workflow_wiring.py
git commit -m "feat(config): wire MARIM_WORKFLOW_TIMEOUT to the workflow ceiling

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 4: Deep-research skill — workflow-first rewrite + compile test

Rewrite `builtin/skills/deep-research/SKILL.md` around one `run_workflow` script (schema-validated fan-out → one bounded coverage round → adversarial verify → return data), keep a condensed `spawn_agent` fallback, and add a test that extracts the embedded reference script and validates it as Monty.

**Files:**
- Modify: `src/marim_harness/builtin/skills/deep-research/SKILL.md` (full replacement below)
- Modify: `src/marim_harness/builtin/agents/researcher.md` — NO changes (spec: schema enforcement is spawn-level). Listed only so the reviewer knows its absence from the diff is deliberate.
- Create: `tests/test_deep_research_skill.py`

**Interfaces:**
- Consumes: `run_workflow`'s `timeout_secs` parameter name (Task 2) — the skill text tells the model to pass it; `agent(..., schema=...)` host-function semantics (existing).
- Produces: nothing other tasks consume.

- [ ] **Step 1: Write the failing test**

Create `tests/test_deep_research_skill.py`:

```python
"""The deep-research SKILL.md embeds a reference workflow script. Skill text
with a script the sandbox rejects is a shipped bug, so this test extracts the
fenced block and puts it through the same parse + static-validation gates the
engine applies to a live script."""

import re
from pathlib import Path

from pydantic_monty import Monty

from marim_harness.config import builtin_root
from marim_harness.workflows.engine import _VALIDATION_PREFIX

SKILL = Path(builtin_root()) / "skills" / "deep-research" / "SKILL.md"


def _reference_script() -> str:
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    assert len(blocks) == 1, "SKILL.md must embed exactly one python reference script"
    return blocks[0]


def test_reference_script_parses_and_validates_as_monty():
    script = _reference_script()
    monty = Monty(script, inputs=["args"], script_name="workflow.py")
    monty.type_check(_VALIDATION_PREFIX)  # raises MontyTypingError on failure


def test_skill_text_teaches_the_workflow_path():
    text = SKILL.read_text(encoding="utf-8")
    assert "run_workflow" in text
    assert "timeout_secs" in text
    # The fallback for installs without the [workflows] extra must survive edits.
    assert "spawn_agent" in text
    # Synthesis stays in the main turn; the script returns data.
    assert "last expression" in text.lower() or "returns data" in text.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest --no-cov tests/test_deep_research_skill.py -q`
Expected: FAIL — the current SKILL.md has no ```python block (the `_reference_script` assert trips) and no `run_workflow` mention.

- [ ] **Step 3: Replace SKILL.md**

Write this as the full content of
`src/marim_harness/builtin/skills/deep-research/SKILL.md`:

````markdown
---
name: deep-research
description: Produce a multi-source, fact-checked, cited research report. Use when the user wants deep research on a topic — fans out parallel researchers, adversarially verifies claims, then synthesizes.
---
# Deep research

Produce a thorough, cited research report by DELEGATING — do NOT do the research
yourself in this turn. Your job is to orchestrate researchers and synthesize their
findings.

## 1. Scope, then plan
Restate the question. Then do a quick SCOPING pass yourself — this is the one place you
research inline. If the domain is unfamiliar, run a couple of `web_search` calls to learn the
field's terminology, map the shape of the debate, and see what the real axes of disagreement
are. Skip the pass for topics you already know well; do NOT let it grow into full research.
(`web_search` is approval-gated — one more reason to keep the pass to a couple of queries.)

Scope FIRST because it makes any question to the user sharper — you only interrupt once, so
spend that interruption on what the landscape shows actually matters, not generic guesses.
After scoping, if scope/constraints are still ambiguous (region, timeframe, budget, use
case), ask the user 1–3 clarifying questions, then continue. The one exception: if the
question is so underspecified you cannot even search meaningfully, ask first.

Then decompose into 3–6 INDEPENDENT sub-questions that can be researched in parallel,
grounded in what the scoping pass surfaced — split along the seams you actually found (not
guessed), phrase each with the domain's real vocabulary, and check the set for gaps and
overlap so no two researchers cover the same ground.

## 2. Run the pipeline (one run_workflow call)
Author ONE `run_workflow` script implementing fan-out → coverage check → adversarial
verify, adapting the reference below. Pass the sub-questions as a list of strings via
`args`, and set the tool's `timeout_secs` to what the fan-out needs — researchers take
minutes each; 1800 covers 4–6 of them. The script returns DATA (its last expression);
you write the report from it afterward.

```python
# Deep research pipeline: fan out -> coverage -> adversarial verify
import asyncio

FINDINGS = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "source": {"type": "string"},
                    "evidence_type": {"type": "string"},
                    "quality": {"type": "string"},
                    "load_bearing": {"type": "boolean"},
                },
                "required": ["claim", "source", "quality", "load_bearing"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "open_questions"],
}
VERDICT = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["holds", "downgrade", "refuted"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
}

async def research(sub_q, sharpen):
    task = ("Research this sub-question and report findings, marking which are "
            "load-bearing: " + sub_q + sharpen)
    try:
        return await agent(task, type="researcher", schema=FINDINGS)
    except Exception:
        return {"findings": [], "open_questions": ["researcher failed: " + sub_q]}

# Wave 1: one researcher per sub-question.
waves = await asyncio.gather(*[research(q, "") for q in args])
log("wave 1 done: " + str(sum(len(w["findings"]) for w in waves)) + " findings")

# Coverage: exactly ONE follow-up round for sub-questions that came back thin.
thin = [i for i in range(len(waves))
        if not any(f["load_bearing"] for f in waves[i]["findings"])]
if thin:
    log("coverage round for " + str(len(thin)) + " thin sub-questions")
    retries = await asyncio.gather(*[
        research(args[i], "\n\nA first pass found little; dig for primary sources.")
        for i in thin])
    for j in range(len(thin)):
        waves[thin[j]]["findings"] = waves[thin[j]]["findings"] + retries[j]["findings"]
        waves[thin[j]]["open_questions"] = (waves[thin[j]]["open_questions"]
                                            + retries[j]["open_questions"])

# Adversarial verify: try to refute each load-bearing claim.
flat = [f for w in waves for f in w["findings"]]
load_bearing = [f for f in flat if f["load_bearing"]]

async def refute(f):
    task = ("Try to REFUTE this claim, and confirm the cited source actually "
            "supports it. Claim: " + f["claim"] + " -- Source: " + f["source"])
    try:
        return await agent(task, type="explore", schema=VERDICT)
    except Exception:
        return {"verdict": "downgrade", "reason": "verifier failed; treat as unverified"}

log("verifying " + str(len(load_bearing)) + " load-bearing claims")
verdicts = await asyncio.gather(*[refute(f) for f in load_bearing])

dropped = []
for k in range(len(load_bearing)):
    f = load_bearing[k]
    v = verdicts[k]
    if v["verdict"] == "refuted":
        dropped.append({"claim": f["claim"], "reason": v["reason"]})
    else:
        f["verified"] = v["verdict"] + ": " + v["reason"]

kept = [f for f in flat
        if not f["load_bearing"] or "verified" in f]
{"findings": kept,
 "dropped": dropped,
 "open_questions": [q for w in waves for q in w["open_questions"]]}
```

The script returns data; the model writes prose. Do not synthesize inside the script.

## 3. Synthesize
From the returned bundle, write ONE report:
- Every nontrivial claim keeps its citation (the `source` field).
- Note verification: claims whose `verified` starts with "downgrade" are presented as
  weaker; `dropped` claims are omitted or explicitly called out as refuted.
- Where good sources genuinely DISAGREE, say so and explain why (effect size, trial
  quality, population) — do not flatten into a single verdict.
- End with: (a) 5 bullets separating what's well-supported from what's shaky or
  overstated, and (b) a per-sub-question confidence rating (high/medium/low) with
  the main limiting factor.

## If run_workflow is unavailable
Some installs lack the workflows extra. Run the same pipeline with `spawn_agent`
directly: in a SINGLE turn, spawn one `researcher` per sub-question (`task` = the
sub-question; `returns` = "a list of findings; each = CLAIM + source + evidence type +
quality (high/medium/low) + whether it is load-bearing"). Collect the reports, then spawn
one `explore` refuter per load-bearing claim, tasked to refute it and confirm the cited
source supports it. Drop or downgrade what does not survive, then synthesize as above.
Weaker guarantees than the script (no schema validation, no coverage loop) — keep the
verify pass even so.

## Example
Topic: "Evidence on creatine for cognition (not muscle)." Sub-questions → researchers:
healthy adults; special populations (sleep-deprived, vegetarians, aging, mood);
dosing/kinetics for a brain effect; safety & study quality. Then refute the
load-bearing claims, then synthesize.
````

If `test_reference_script_parses_and_validates_as_monty` rejects a construct
(Monty is a Python subset — no classes, no `match`, limited builtins), adjust
the script to equivalent simpler constructs (index loops, string concatenation)
until it passes, keeping the pipeline semantics: wave → ONE coverage round →
refute load-bearing → return `{"findings", "dropped", "open_questions"}`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_deep_research_skill.py tests/test_skills.py -q`
Expected: all PASS (`test_skills.py` re-checks builtin discovery of `deep-research` — the frontmatter `name`/`description` keys must survive the rewrite).

- [ ] **Step 5: Lint, type-check, full suite**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest -q`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/builtin/skills/deep-research/SKILL.md tests/test_deep_research_skill.py
git commit -m "feat(deep-research): workflow-first pipeline with spawn_agent fallback

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EVaAPNAjvrEsXQvsEWb1gN"
```

---

### Task 5: Live smoke — long workflow on the local model (MANUAL)

**Do NOT dispatch a subagent for this task.** It is run by the session controller with the user present, per the spec's validation decision (units + local live smoke; no paid models without explicit approval).

**Files:** none (no code changes; observations recorded in the PR description).

**Interfaces:**
- Consumes: everything above, merged on the branch.

- [ ] **Step 1: Start marim in tmux on the local model**

```bash
tmux new-session -d -s marim-smoke -x 220 -y 50
tmux send-keys -t marim-smoke "cd /home/mateuscmarim/Projects/marim.dev/marim-harness && MARIM_PROVIDER=local MARIM_MODEL=ornith-1.0-9b uv run marim" Enter
```

(LM Studio must be serving; see the `owl-alpha-live-tmux-model-slug` memory for the launch pattern.)

- [ ] **Step 2: Drive a >5-minute workflow**

Ask marim to run the deep-research skill on a small topic (workspace-scoped questions keep the local model competent, e.g. "deep research: how does this repo bound and time out dynamic workflows?"). Confirm the model passes `timeout_secs` > 300 in the `run_workflow` approval prompt before approving.

- [ ] **Step 3: Verify the run**

Watch for: the workflow card appears and streams `log()` lines; the run passes the 5-minute mark WITHOUT "Workflow timed out after 300s"; researchers spawn as children in the sub-agents screen; the final turn synthesizes from returned data. Capture `tmux capture-pane -t marim-smoke -p` output for the PR notes.

- [ ] **Step 4: Tear down**

```bash
tmux kill-session -t marim-smoke
```

---

## Final verification (before finishing-a-development-branch)

Run in CI order: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all clean on Python 3.10 semantics (no 3.11+ syntax was introduced).
