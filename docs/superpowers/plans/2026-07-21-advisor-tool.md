# Advisor Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the main agent an `advisor()` tool that forwards the full conversation to a separately-configured, typically stronger model and returns its strategic guidance — with env/builder config, session persistence, a `/advisor` command, a Settings-screen row, and soft system-prompt steering.

**Architecture:** A new root module `advisor.py` builds the advice callable (cloned from `compaction.make_summarizer`); it is bound to `deps.services.advise` (the `run_workflow` seam pattern). The tool registers on the main agent with a pydantic-ai `prepare` hook that omits it when the seam is `None`, so `/advisor off`/`/advisor <model>` toggle live with no agent rebuild. The advisor model id resolves per call: session override (`"off"` sentinel supported) → env/config default → none.

**Tech Stack:** Python 3.10+, pydantic-ai (`Agent`, `FunctionModel`/`TestModel` for tests, `Agent.tool(prepare=...)`), Textual (TUI), pytest + anyio.

**Spec:** `docs/superpowers/specs/2026-07-21-advisor-tool-design.md` (approved).

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`; cyclomatic complexity cap 10 (extract helpers, never `# noqa: C901`).
- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pytest`/`pip`.
- CI order before claiming done: ruff → pyright → pytest.
- Main loop only: the advisor tool must NOT be added to `_SUBAGENT_FNS` in `tools/provider.py`.
- The advisor tool is ungated (no `requires_approval=True`).
- Advisor failures return an error **string** from the tool — the turn never fails on advisor failure.
- Tool docstrings are model-facing product copy — write them accordingly.
- No live-model tests: use `TestModel`/`FunctionModel` only. (User rule: never run a paid model without explicit approval.)
- Preserve the codebase's long "why" comments; write new ones in the same style.
- Each task: run its tests, then commit before moving on.

---

### Task 1: Advisor core module (`advisor.py`)

**Files:**
- Create: `src/marim_harness/advisor.py`
- Test: `tests/test_advisor.py`

**Interfaces:**
- Consumes: `render_transcript(messages, max_part_chars)` from `marim_harness/compaction.py:259`; `aux_model_for(model, *, cwd)` from `marim_harness/session/ctrl.py:36`.
- Produces (later tasks rely on these exact names):
  - `AdviseFn = Callable[[list], Awaitable[str]]`
  - `ADVISOR_OFF: str = "off"` (session-persistence sentinel)
  - `ADVISOR_GUIDANCE: str` (soft-steering prompt block, used by Task 6)
  - `make_advisor(build_model: Callable[[str], Model], get_model_id: Callable[[], str | None], *, cwd: str, max_tokens: int = 2048) -> AdviseFn`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_advisor.py`:

```python
"""The advisor core: make_advisor (the advice callable) and its prompt/text
constants. No live models — TestModel/FunctionModel only."""

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.advisor import (
    ADVISOR_GUIDANCE,
    ADVISOR_OFF,
    _advise_prompt,
    make_advisor,
)


def test_advisor_off_sentinel_value():
    # Persisted into session JSON; changing it orphans saved sessions.
    assert ADVISOR_OFF == "off"


def test_advise_prompt_wraps_transcript_and_restates_task():
    prompt = _advise_prompt("User: hi\nAssistant: hello")
    assert "=== TRANSCRIPT START ===" in prompt
    assert "=== TRANSCRIPT END ===" in prompt
    assert "User: hi" in prompt
    # The task must be restated in the user turn (claude-cli appends our
    # instructions to Claude Code's own prompt, so system-only rules drift).
    assert "advice" in prompt.lower() or "guidance" in prompt.lower()


def test_guidance_mentions_the_tool_and_weighing():
    assert "advisor" in ADVISOR_GUIDANCE
    assert "transcript" in ADVISOR_GUIDANCE


@pytest.mark.anyio
async def test_make_advisor_returns_advice_with_usage_trailer():
    advise = make_advisor(
        lambda mid: TestModel(custom_output_text="Refactor the parser first."),
        lambda: "test:model",
        cwd=".",
        max_tokens=64,
    )
    out = await advise([])
    assert out.startswith("Refactor the parser first.")
    assert "[advisor usage:" in out


@pytest.mark.anyio
async def test_no_model_configured_returns_error_string():
    advise = make_advisor(lambda mid: TestModel(), lambda: None, cwd=".")
    out = await advise([])
    assert out.startswith("Advisor unavailable")
    assert "Continue without advice" in out


@pytest.mark.anyio
async def test_build_failure_returns_error_string():
    def broken(mid):
        raise ValueError("no credentials for provider 'nope'")

    advise = make_advisor(broken, lambda: "nope:model", cwd=".")
    out = await advise([])
    assert out.startswith("Advisor unavailable")
    assert "no credentials" in out
    assert "Continue without advice" in out


@pytest.mark.anyio
async def test_run_failure_retries_once_with_tighter_transcript_then_succeeds():
    calls = []

    def flaky(messages, info):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("context overflow")
        return ModelResponse(parts=[TextPart("second try")])

    advise = make_advisor(lambda mid: FunctionModel(flaky), lambda: "m", cwd=".")
    out = await advise([])
    assert out.startswith("second try")
    assert len(calls) == 2


@pytest.mark.anyio
async def test_run_failure_twice_returns_error_string():
    def always_broken(messages, info):
        raise RuntimeError("boom")

    advise = make_advisor(lambda mid: FunctionModel(always_broken), lambda: "m", cwd=".")
    out = await advise([])
    assert out.startswith("Advisor unavailable")
    assert "boom" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_advisor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.advisor'`

- [ ] **Step 3: Write the implementation**

Create `src/marim_harness/advisor.py`:

```python
"""The advisor: a separately-configured, typically stronger model the main
agent can consult mid-task through the ``advisor`` tool.

Client-side replica of Anthropic's advisor tool (theirs is the server-side
``advisor_20260301``; marim runs on arbitrary providers, so the consult is a
plain tool-free one-shot run here). ``make_advisor`` mirrors
``compaction.make_summarizer``: a dedicated tool-free agent reads the rendered
transcript and returns guidance text. Which model it consults is re-resolved
PER CALL through ``get_model_id`` — that per-call resolution is what makes a
mid-session ``/advisor`` switch live without an agent rebuild.

Every failure path returns a short actionable STRING, never raises: the advice
lands in a tool result, and a broken advisor must degrade the advice, not the
turn (see the design spec's error-handling section).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from .compaction import render_transcript
from .session.ctrl import aux_model_for

if TYPE_CHECKING:
    from pydantic_ai.models import Model

# (messages) -> advice text. The advisor tool passes the in-flight run history
# (ctx.messages); errors come back as text.
AdviseFn = Callable[[list], Awaitable[str]]

# Session-persistence sentinel: SessionStore.advisor_model == "off" means the
# user explicitly disabled the advisor for that session, which must survive
# restarts distinguishably from None ("unset — inherit the env default").
ADVISOR_OFF = "off"

_ADVISOR_INSTRUCTIONS = (
    "You are a senior engineer advising a coding agent mid-task. You will be "
    "shown the full transcript of its session so far: the user's request, the "
    "agent's reasoning, and every tool call and result. Give focused strategic "
    "guidance: whether the current approach is sound, risks or mistakes you "
    "see, and what to check or do before proceeding. Be concise and concrete. "
    "Do not restate the transcript, do not write the code yourself, and do not "
    "address the user — you are speaking to the agent."
)

# Appended to the main agent's system prompt (see runtime/instructions.py) only
# while an advisor is configured — the same ``services.advise`` seam gates the
# tool itself, so prompt and tool availability cannot drift. Soft steering only:
# Anthropic's timing + weigh-the-advice blocks, no hard-rule enforcement.
ADVISOR_GUIDANCE = (
    "An advisor tool is available: calling it sends the full conversation "
    "transcript to a stronger reviewer model and returns strategic guidance.\n"
    "When to consult the advisor:\n"
    "- Before starting substantive work on a non-trivial task, once you have "
    "gathered the relevant context.\n"
    "- When you are stuck, going in circles, or about to make a risky or "
    "hard-to-reverse change.\n"
    "- Before declaring a complex task done, to check for gaps.\n"
    "Skip it for trivial questions and simple mechanical edits.\n"
    "Weighing the advice: the advisor sees only the transcript, not the live "
    "workspace. Treat its guidance as a strong signal, not an order — when it "
    "conflicts with direct evidence you gathered from files or command "
    "output, trust your evidence and say why."
)

# First attempt renders the transcript at render_transcript's default clip;
# the retry tightens it hard, on the theory that the most likely run failure
# is a context overflow on the advisor's (unknown) window. One retry only —
# a second failure surfaces as the error string.
_CLIP_ATTEMPTS = (2000, 400)


def _advise_prompt(transcript: str) -> str:
    """Wrap the transcript in an explicit, in-message advice instruction. As
    with compaction's ``_summarize_prompt``: rules only in the system prompt
    let weaker models reply conversationally, and under a claude-cli advisor
    our instructions are merely appended to Claude Code's own prompt — so the
    task is restated in the user turn."""
    return (
        "You are advising the coding agent whose session transcript follows. "
        "Following the rules in your instructions, give focused strategic "
        "guidance: approach soundness, risks, and what to check before "
        "proceeding. Output only the advice — do not restate the transcript "
        "or address the user.\n\n"
        "=== TRANSCRIPT START ===\n"
        f"{transcript}\n"
        "=== TRANSCRIPT END ===\n\n"
        "Advice:"
    )


def make_advisor(
    build_model: Callable[[str], "Model"],
    get_model_id: Callable[[], str | None],
    *,
    cwd: str,
    max_tokens: int = 2048,
) -> AdviseFn:
    """Build the advice callable bound to ``services.advise``.

    ``build_model`` turns a model id into a Model (the Harness supplies
    ``MultiModelSource.build`` when a source exists, else ``infer_model``).
    ``get_model_id`` is a live getter (closing over the Harness's mutable
    advisor id) so ``/advisor`` switches apply to the next consultation.
    ``cwd`` feeds ``aux_model_for``'s claude-cli ephemeral clone."""

    async def advise(messages: list) -> str:
        model_id = get_model_id()
        if not model_id:
            return (
                "Advisor unavailable: no advisor model is configured. "
                "Continue without advice."
            )
        try:
            # A claude-cli advisor must not share the session-carrying CLI
            # instance — aux_model_for swaps in a stateless ephemeral clone,
            # the same guard the summarizer/titler get.
            model = aux_model_for(build_model(model_id), cwd=cwd)
        except Exception as exc:
            return (
                f"Advisor unavailable: can't build model {model_id!r}: {exc}. "
                "Continue without advice."
            )
        agent = Agent(
            model,
            instructions=_ADVISOR_INSTRUCTIONS,
            model_settings=ModelSettings(max_tokens=max_tokens),
        )
        last_error: Exception | None = None
        for clip in _CLIP_ATTEMPTS:
            try:
                result = await agent.run(
                    _advise_prompt(render_transcript(messages, max_part_chars=clip))
                )
            except Exception as exc:
                last_error = exc
                continue
            usage = result.usage()
            return (
                f"{result.output}\n\n"
                f"[advisor usage: {usage.input_tokens or 0} in, "
                f"{usage.output_tokens or 0} out tokens]"
            )
        return f"Advisor unavailable: {last_error}. Continue without advice."

    return advise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_advisor.py -v`
Expected: 8 passed

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check src/marim_harness/advisor.py tests/test_advisor.py && uv run pyright`
Expected: clean.

```bash
git add src/marim_harness/advisor.py tests/test_advisor.py
git commit -m "feat(advisor): advice-callable core (make_advisor, prompt, guidance text)"
```

---

### Task 2: Env config (`MARIM_ADVISOR_*` → `ModelConfig`)

**Files:**
- Modify: `src/marim_harness/config/model.py` (ModelConfig fields near line 188; `_common_kwargs()` returns near line 292)
- Modify: `.env.example` (after the sub-agent tier block, near line 141)
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `ModelConfig.advisor_model: str | None` (default `None`), `ModelConfig.advisor_max_tokens: int` (default `2048`), `ModelConfig.advisor_max_uses: int | None` (default `None`). Read from `MARIM_ADVISOR_MODEL` / `MARIM_ADVISOR_MAX_TOKENS` / `MARIM_ADVISOR_MAX_USES`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_advisor_env_config(monkeypatch):
    monkeypatch.setenv("MARIM_ADVISOR_MODEL", "openrouter:anthropic/claude-opus-4.8")
    monkeypatch.setenv("MARIM_ADVISOR_MAX_TOKENS", "1024")
    monkeypatch.setenv("MARIM_ADVISOR_MAX_USES", "3")
    cfg = load_config()
    assert cfg.advisor_model == "openrouter:anthropic/claude-opus-4.8"
    assert cfg.advisor_max_tokens == 1024
    assert cfg.advisor_max_uses == 3


def test_advisor_env_defaults(monkeypatch):
    for var in ("MARIM_ADVISOR_MODEL", "MARIM_ADVISOR_MAX_TOKENS", "MARIM_ADVISOR_MAX_USES"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.advisor_model is None
    assert cfg.advisor_max_tokens == 2048
    assert cfg.advisor_max_uses is None  # unset = unlimited


def test_advisor_max_uses_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("MARIM_ADVISOR_MAX_USES", "0")
    cfg = load_config()
    assert cfg.advisor_max_uses is None
```

(`load_config` is already imported at the top of `tests/test_config.py`; if not, add `from marim_harness.config import load_config`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_config.py -k advisor -v`
Expected: FAIL with `TypeError` (unexpected field) or `AttributeError: 'ModelConfig' object has no attribute 'advisor_model'`

- [ ] **Step 3: Implement**

In `src/marim_harness/config/model.py`, add fields to `ModelConfig` directly after the `job_tool_combined` field (line ~188):

```python
    # Advisor: a model the main agent can consult mid-task via the advisor
    # tool. ``advisor_model`` is a qualified ``provider:model_id`` (or a bare
    # slug for the default provider); None = no advisor. ``advisor_max_tokens``
    # caps each consultation's output; ``advisor_max_uses`` caps calls per turn
    # (None = unlimited).
    advisor_model: str | None = None
    advisor_max_tokens: int = 2048
    advisor_max_uses: int | None = None
```

In `_common_kwargs()`'s returned dict, after the `job_tool_combined=...` line (line ~292):

```python
        advisor_model=(os.getenv("MARIM_ADVISOR_MODEL") or None),
        advisor_max_tokens=_int_env("MARIM_ADVISOR_MAX_TOKENS", 2048),
        # Unset or 0 = unlimited — the same "0 falls through to None" pattern
        # context_window uses (see _int_env: non-positive returns the default).
        advisor_max_uses=(_int_env("MARIM_ADVISOR_MAX_USES", 0) or None),
```

Note: `_int_env("MARIM_ADVISOR_MAX_USES", 0)` returns `0` for unset/zero/garbage, which `or None` folds to `None` (unlimited) — same idiom as `context_window` at line 276.

In `.env.example`, after the `MARIM_SUBAGENT_TIERING` block (line ~141):

```
# --- Advisor ---
# A model the agent can consult mid-task for strategic guidance (the advisor
# tool): qualified provider:model_id, or a bare slug for the default provider.
# Unset = no advisor (the tool is not offered to the model).
# MARIM_ADVISOR_MODEL=openrouter:anthropic/claude-opus-4.8
# Output cap per consultation (default 2048).
# MARIM_ADVISOR_MAX_TOKENS=2048
# Per-turn call cap. Unset or 0 = unlimited.
# MARIM_ADVISOR_MAX_USES=3
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_config.py -v`
Expected: all pass (including pre-existing tests).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/config/model.py .env.example tests/test_config.py
git commit -m "feat(advisor): MARIM_ADVISOR_* env config"
```

---

### Task 3: The `advisor` tool — seam, prepare hook, per-turn cap

**Files:**
- Create: `src/marim_harness/tools/advisor_tools.py`
- Modify: `src/marim_harness/runtime/deps.py` (type alias near line 74; `HarnessServices` field near line 137; `Deps` fields near line 262)
- Modify: `src/marim_harness/tools/provider.py` (import list line 29-40; `BuiltinToolProvider.register` line 186-193)
- Modify: `src/marim_harness/runtime/controller.py` (`run_turn`, line 916 — reset counter)
- Test: `tests/test_advisor_tool.py`

**Interfaces:**
- Consumes: nothing from Task 1 at runtime (the seam holds any `AdviseFn`).
- Produces:
  - `deps.py`: `AdviseFn = Callable[[list], Awaitable[str]]`; `HarnessServices.advise: AdviseFn | None = None`; `Deps.advisor_uses: int = 0`; `Deps.advisor_max_uses: int | None = None`
  - `advisor_tools.py`: `async def advisor(ctx: RunContext[Deps]) -> str`; `async def prepare_advisor(ctx, tool_def) -> ToolDefinition | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_advisor_tool.py`:

```python
"""The advisor tool: presence gated by the services.advise seam (prepare
hook), advice pass-through, and the per-turn call cap."""

from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.output import DeferredToolRequests

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.tools import advisor_tools
from marim_harness.tools.provider import BuiltinToolProvider


def _deps(tmp_path) -> Deps:
    return Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))


def _tool_capture_agent(seen: list):
    async def fn(messages, info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(fn), deps_type=Deps,
        output_type=[str, DeferredToolRequests],
    )
    BuiltinToolProvider().register(agent)
    return agent


async def _noop_advise(messages: list) -> str:
    return "stub advice"


@pytest.mark.anyio
async def test_tool_absent_when_no_advisor_configured(tmp_path):
    seen: list[str] = []
    deps = _deps(tmp_path)
    assert deps.services.advise is None
    await _tool_capture_agent(seen).run("hi", deps=deps)
    assert "advisor" not in seen
    assert "read_file" in seen  # sanity: registration itself worked


@pytest.mark.anyio
async def test_tool_present_when_advisor_configured(tmp_path):
    seen: list[str] = []
    deps = _deps(tmp_path)
    deps.services.advise = _noop_advise
    await _tool_capture_agent(seen).run("hi", deps=deps)
    assert "advisor" in seen


@pytest.mark.anyio
async def test_advisor_forwards_messages_and_returns_advice(tmp_path):
    got: list = []

    async def advise(messages: list) -> str:
        got.append(messages)
        return "Use TDD."

    deps = _deps(tmp_path)
    deps.services.advise = advise
    ctx = SimpleNamespace(deps=deps, messages=["m1", "m2"])
    out = await advisor_tools.advisor(ctx)
    assert out == "Use TDD."
    assert got == [["m1", "m2"]]
    assert deps.advisor_uses == 1


@pytest.mark.anyio
async def test_advisor_cap_exhaustion_returns_error_string(tmp_path):
    deps = _deps(tmp_path)
    deps.services.advise = _noop_advise
    deps.advisor_max_uses = 1
    ctx = SimpleNamespace(deps=deps, messages=[])
    assert await advisor_tools.advisor(ctx) == "stub advice"
    second = await advisor_tools.advisor(ctx)
    assert "limit" in second
    assert "Continue without" in second
    assert deps.advisor_uses == 1  # a refused call doesn't consume budget


@pytest.mark.anyio
async def test_advisor_without_seam_degrades(tmp_path):
    # Defensive: the prepare hook normally hides the tool, but a race (advisor
    # turned off mid-turn) can still land a call on a None seam.
    ctx = SimpleNamespace(deps=_deps(tmp_path), messages=[])
    out = await advisor_tools.advisor(ctx)
    assert "No advisor is configured" in out


@pytest.mark.anyio
async def test_run_turn_resets_advisor_uses(tmp_path):
    from marim_harness.runtime.harness import Harness

    deps = _deps(tmp_path)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, "Be helpful.")
    deps.advisor_uses = 3
    await harness.run_turn("hi")
    assert deps.advisor_uses == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_advisor_tool.py -v`
Expected: FAIL with `ImportError: cannot import name 'advisor_tools'`

- [ ] **Step 3: Implement — deps.py**

In `src/marim_harness/runtime/deps.py`, after the `WorkflowRunner` alias (line ~74):

```python
# (messages) -> advice text. The advisor tool forwards the in-flight run
# history (ctx.messages) to the configured advisor model; failures come back
# as text so the turn never fails on advisor failure. None ⇒ no advisor is
# configured — the tool's prepare hook then omits it from the run entirely.
AdviseFn = Callable[[list], Awaitable[str]]
```

In `HarnessServices`, after the `get_scratchpad` field (line ~137):

```python
    # Lets the advisor tool consult the configured advisor model. Live on/off
    # seam (like run_workflow): Harness.set_advisor_model flips it at runtime,
    # and both the tool's prepare hook and the steering-instructions closure
    # read it per request, so tool schema and prompt toggle together.
    advise: AdviseFn | None = None
```

In `Deps`, after the `subagent_max_depth` field (line ~262):

```python
    # Advisor per-turn call accounting: how many consultations this turn has
    # made (reset by TurnController.run_turn at each turn start) and the cap
    # (None = unlimited; stamped from HarnessConfig at build). On Deps rather
    # than a tool parameter for the same reason as subagent_max_depth: anything
    # in the advertised schema is model-writable, and the model must not be
    # able to raise its own ceiling.
    advisor_uses: int = 0
    advisor_max_uses: int | None = None
```

- [ ] **Step 4: Implement — the tool module**

Create `src/marim_harness/tools/advisor_tools.py`:

```python
"""The advisor tool: consult a separately-configured, typically stronger model
for strategic guidance mid-task.

Main-agent only — deliberately NOT in provider._SUBAGENT_FNS (sub-agents have
model tiering instead; see the design spec's scope section). Registered plain
(ungated): configuring an advisor is consent to send the transcript to that
provider, the same consent running the main model implies.

The ``prepare`` hook is the live toggle: it omits the tool from any run where
``services.advise`` is None, so an unconfigured install never advertises it
and ``/advisor off`` takes effect on the next model request with no agent
rebuild.
"""

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from ..runtime.deps import Deps


async def prepare_advisor(
    ctx: RunContext[Deps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Omit the advisor tool from the run schema when no advisor is
    configured. Reads the live seam per request, so toggling the advisor
    mid-session applies on the next request."""
    if ctx.deps.services.advise is None:
        return None
    return tool_def


async def advisor(ctx: RunContext[Deps]) -> str:
    """Consult your advisor: a stronger reviewer model that sees this entire
    conversation — the task, your reasoning, and every tool call and result —
    and returns strategic guidance.

    Call it before starting substantive work on a non-trivial task, when you
    are stuck or about to make a risky change, and before declaring a complex
    task done. It takes no arguments; the transcript is forwarded
    automatically. The advice is guidance to weigh against your own evidence,
    not an instruction to follow blindly.
    """
    advise = ctx.deps.services.advise
    if advise is None:
        # The prepare hook normally hides the tool in this state; this is the
        # race window where the advisor was turned off after the request that
        # advertised the tool was already in flight.
        return "No advisor is configured. Continue without advice."
    cap = ctx.deps.advisor_max_uses
    if cap is not None and ctx.deps.advisor_uses >= cap:
        return (
            f"Advisor call limit reached for this turn ({cap}). "
            "Continue without further advice."
        )
    ctx.deps.advisor_uses += 1
    return await advise(list(ctx.messages))
```

- [ ] **Step 5: Implement — registration and per-turn reset**

In `src/marim_harness/tools/provider.py`, add `advisor_tools` to the concern-module import block (lines 29-40, alphabetical — before `edit_tools`):

```python
from . import (
    advisor_tools,
    edit_tools,
    ...
)
```

In `BuiltinToolProvider.register` (line 186-193), register between `_register_action_tools` and `_register_jobs`:

```python
        g = self._groups
        _register_read_tools(agent, g)
        _register_action_tools(agent, g)
        # The advisor tool registers unconditionally rather than behind a
        # ToolGroups flag: its prepare hook already omits it from every run
        # where services.advise is None, so an unconfigured install (or an
        # embedder that never calls with_advisor) advertises nothing — a
        # second build-time gate would be redundant state to keep in sync.
        agent.tool(prepare=advisor_tools.prepare_advisor)(advisor_tools.advisor)
        self._register_jobs(agent)
```

In `src/marim_harness/runtime/controller.py`, at the very top of `run_turn`'s body (line ~924, before `await self._maybe_compact()`):

```python
        # Fresh per-turn advisor budget: the cap is per TURN, but Deps is
        # session-lived, so the counter must be re-zeroed as each turn starts.
        self.deps.advisor_uses = 0
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_advisor_tool.py -v`
Expected: 6 passed.

Also run the provider/registration guard suites:
`uv run pytest --no-cov tests/test_provider.py tests/test_agent.py -q`
Expected: pass (the new tool must not break tool-name collision checks or sub-agent registration).

- [ ] **Step 7: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/tools/advisor_tools.py src/marim_harness/tools/provider.py \
    src/marim_harness/runtime/deps.py src/marim_harness/runtime/controller.py \
    tests/test_advisor_tool.py
git commit -m "feat(advisor): advisor tool with prepare-hook gating and per-turn cap"
```

---

### Task 4: Session persistence (`SessionStore.advisor_model`)

**Files:**
- Modify: `src/marim_harness/session/store.py` (`SessionInfo` line ~150; `SessionStore.__init__` line 159; `save` payload line 179; `save_meta` line 240; `list()` line ~351; `store()` line 357-376; `create()` line 378-408; add `latest_advisor_model`)
- Modify: `src/marim_harness/session/ctrl.py` (after `set_model`, line ~316)
- Test: `tests/test_advisor_session.py`

**Interfaces:**
- Consumes: `ADVISOR_OFF` from Task 1 (tests only; the store treats it as an opaque string).
- Produces: `SessionStore.advisor_model: str | None`; `SessionInfo.advisor_model: str | None`; `SessionManager.latest_advisor_model() -> str | None`; `SessionController.saved_advisor_id` property; `SessionController.set_advisor(value: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_advisor_session.py`:

```python
"""Session persistence of the advisor model, mirroring the ``model`` field:
save/save_meta/store() round-trip, create()-inherits-latest, and the "off"
sentinel treated as an ordinary persisted string."""

from pydantic_ai.usage import RunUsage

from marim_harness.advisor import ADVISOR_OFF
from marim_harness.session import SessionManager


def test_advisor_model_round_trips_through_save(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.advisor_model = "openrouter:anthropic/claude-opus-4.8"
    store.save([], RunUsage())
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.advisor_model == "openrouter:anthropic/claude-opus-4.8"


def test_save_meta_patches_advisor_without_touching_messages(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.save([], RunUsage())
    store.advisor_model = ADVISOR_OFF
    store.save_meta()
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.advisor_model == ADVISOR_OFF


def test_create_inherits_latest_advisor_model(tmp_path):
    manager = SessionManager(tmp_path)
    first = manager.create()
    first.advisor_model = "openrouter:opus"
    first.save([], RunUsage())
    fresh = manager.create()
    assert fresh.advisor_model == "openrouter:opus"


def test_create_without_history_has_no_advisor(tmp_path):
    manager = SessionManager(tmp_path)
    assert manager.create().advisor_model is None


def test_old_session_files_load_with_none(tmp_path):
    # A pre-advisor session file simply has no key.
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.save([], RunUsage())
    import json
    data = json.loads(store.path.read_text())
    data.pop("advisor_model", None)
    store.path.write_text(json.dumps(data))
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.advisor_model is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_advisor_session.py -v`
Expected: FAIL with `AttributeError: 'SessionStore' object has no attribute 'advisor_model'`

- [ ] **Step 3: Implement — store.py**

`SessionInfo` (line ~150): add field after `model`:

```python
    model: str | None = None
    advisor_model: str | None = None
```

`SessionStore.__init__` (line 159): add parameter and assignment:

```python
    def __init__(self, path, workspace_root, session_id: str, name: str,
                 auto_named: bool = False, model: str | None = None,
                 advisor_model: str | None = None) -> None:
        ...
        # The model id this session was last using (None -> the env default).
        self.model = model
        # The advisor this session chose: a provider:slug, the "off" sentinel
        # (explicitly disabled — must survive restarts distinguishably from
        # unset), or None (unset -> inherit MARIM_ADVISOR_MODEL).
        self.advisor_model = advisor_model
```

`save` payload (line 179): after `"model": self.model,` add:

```python
            "advisor_model": self.advisor_model,
```

`save_meta` (line 240): after `data["model"] = self.model` add:

```python
            data["advisor_model"] = self.advisor_model
```

`list()` `SessionInfo(...)` construction (line ~351): after `model=data.get("model"),` add:

```python
                    advisor_model=data.get("advisor_model"),
```

`store()` (line ~371): after `model = meta.get("model")` add `advisor_model = meta.get("advisor_model")`, and pass it through:

```python
        return SessionStore(
            path, self.workspace_root, session_id, name,
            auto_named=auto_named, model=model, advisor_model=advisor_model,
        )
```

`create()` (line ~406): after the model-inherit block add:

```python
        # Same inheritance as the model above: a new session keeps the advisor
        # the user last chose (including the "off" sentinel — an explicit
        # disable carries forward too, not just positive picks).
        if store.advisor_model is None:
            store.advisor_model = self.latest_advisor_model()
        return store
```

After `latest_model()` (line ~425) add:

```python
    def latest_advisor_model(self) -> str | None:
        """The advisor id of the most recent session, or *None*."""
        latest = self.latest()
        return latest.advisor_model if latest is not None else None
```

- [ ] **Step 4: Implement — ctrl.py**

In `src/marim_harness/session/ctrl.py`, after `set_model` (line ~316):

```python
    @property
    def saved_advisor_id(self) -> str | None:
        """The advisor persisted with this session — a provider:slug, the
        "off" sentinel, or None (unset) — or None if no store."""
        return self.store.advisor_model if self.store is not None else None

    def set_advisor(self, value: str) -> None:
        """Persist the session's advisor choice (a provider:slug or the "off"
        sentinel). Same metadata-only patch rules as ``set_model``: a switch
        can land mid-turn when in-memory history must never reach disk, so
        patch the header when a file exists, else force one clean persist."""
        if self.store is not None:
            self.store.advisor_model = value
            if self.store.path.exists():
                self.store.save_meta()
            else:
                self.persist(force=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_advisor_session.py tests/test_session.py tests/test_agent_sessions.py -q`
Expected: all pass.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/session/store.py src/marim_harness/session/ctrl.py tests/test_advisor_session.py
git commit -m "feat(advisor): persist the session advisor choice (off-sentinel aware)"
```

---

### Task 5: Harness wiring (config, live setter, builder, bootstrap)

**Files:**
- Modify: `src/marim_harness/runtime/harness.py` (`HarnessConfig` after `subagent_tiers`, line ~187; `Harness.__init__` after the `TurnController` construction, line ~480; new methods after `set_model`; `resume`/`new_session`/`switch_session` at lines 544, 565, 575)
- Modify: `src/marim_harness/runtime/builder.py` (new `with_advisor` after `with_hooks`, line ~161)
- Modify: `src/marim_harness/runtime/bootstrap.py` (`with_config_overrides`, line ~189)
- Modify: `docs/embedding.md` (document `with_advisor`)
- Test: `tests/test_advisor_wiring.py`

**Interfaces:**
- Consumes: `make_advisor`, `ADVISOR_OFF` (Task 1); `HarnessServices.advise`, `Deps.advisor_max_uses` (Task 3); `SessionController.saved_advisor_id`/`set_advisor` (Task 4); `ModelConfig.advisor_*` (Task 2).
- Produces: `HarnessConfig.advisor_model / advisor_max_tokens / advisor_max_uses`; `Harness.advisor_model_id: str | None`; `Harness.set_advisor_model(model_id: str | None, *, persist: bool = True)`; `HarnessBuilder.with_advisor(model: str, *, max_tokens: int = 2048, max_uses: int | None = None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_advisor_wiring.py`:

```python
"""Harness-level advisor wiring: config default -> live seam, the session
"off" sentinel beating the env default, the live setter's persist rules, the
builder front door, and the bootstrap env pass-through."""

import pytest
from pydantic_ai.models.test import TestModel

from marim_harness.advisor import ADVISOR_OFF
from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.session import SessionManager
from marim_harness.tools.provider import BuiltinToolProvider


def _harness(tmp_path, **kwargs) -> Harness:
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    return Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, "Be helpful.", **kwargs
    )


def test_config_default_activates_the_seam(tmp_path):
    h = _harness(tmp_path, advisor_model="openrouter:opus", advisor_max_uses=2)
    assert h.advisor_model_id == "openrouter:opus"
    assert h.deps.services.advise is not None
    assert h.deps.advisor_max_uses == 2


def test_unconfigured_leaves_the_seam_none(tmp_path):
    h = _harness(tmp_path)
    assert h.advisor_model_id is None
    assert h.deps.services.advise is None
    assert h.deps.advisor_max_uses is None


def test_session_off_sentinel_beats_config_default(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.advisor_model = ADVISOR_OFF
    h = _harness(tmp_path, store=store, manager=manager, advisor_model="openrouter:opus")
    assert h.advisor_model_id is None
    assert h.deps.services.advise is None


def test_session_slug_beats_config_default(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.advisor_model = "local:small"
    h = _harness(tmp_path, store=store, manager=manager, advisor_model="openrouter:opus")
    assert h.advisor_model_id == "local:small"


def test_set_advisor_model_switches_and_persists(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(tmp_path, store=store, manager=manager)
    h.set_advisor_model("openrouter:opus")
    assert h.advisor_model_id == "openrouter:opus"
    assert h.deps.services.advise is not None
    assert store.advisor_model == "openrouter:opus"


def test_set_advisor_model_none_disables_and_persists_sentinel(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    h = _harness(
        tmp_path, store=store, manager=manager, advisor_model="openrouter:opus"
    )
    h.set_advisor_model(None)
    assert h.advisor_model_id is None
    assert h.deps.services.advise is None
    assert store.advisor_model == ADVISOR_OFF


def test_builder_with_advisor(tmp_path):
    from marim_harness.runtime.builder import HarnessBuilder

    h = (
        HarnessBuilder(workspace=tmp_path, model=TestModel(call_tools=[]))
        .with_advisor("openrouter:opus", max_tokens=512, max_uses=2)
        .build()
    )
    assert h.advisor_model_id == "openrouter:opus"
    assert h.deps.services.advise is not None
    assert h.deps.advisor_max_uses == 2


def test_bootstrap_passes_advisor_env(monkeypatch, tmp_path):
    from marim_harness.runtime.bootstrap import build_harness

    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("MARIM_ADVISOR_MODEL", "openrouter:anthropic/claude-opus-4.8")
    harness = build_harness(tmp_path)
    assert harness.advisor_model_id == "openrouter:anthropic/claude-opus-4.8"
    assert harness.deps.services.advise is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_advisor_wiring.py -v`
Expected: FAIL with `AttributeError: 'Harness' object has no attribute 'advisor_model_id'` (and `TypeError: unknown HarnessConfig fields` variants).

- [ ] **Step 3: Implement — HarnessConfig + Harness**

In `src/marim_harness/runtime/harness.py`:

Add import near the other package-root imports at the top of the file:

```python
from ..advisor import ADVISOR_OFF, make_advisor
```

`HarnessConfig`: after `subagent_tiers` (line ~187) add:

```python
    # Advisor: the DEFAULT advisor model (provider:slug, or any pydantic-ai
    # model string when no model_source is composed; None = no advisor), the
    # output cap per consultation, and the per-turn call cap (None =
    # unlimited). The session store's advisor_model overrides advisor_model
    # at runtime — see Harness._apply_saved_advisor.
    advisor_model: str | None = None
    advisor_max_tokens: int = 2048
    advisor_max_uses: int | None = None
```

`Harness.__init__`: after the `TurnController` construction (line ~480) add:

```python
        # Advisor: build ONE advise callable for the harness lifetime; which
        # model it consults is re-resolved PER CALL through the closure over
        # advisor_model_id, so /advisor switches apply to the next
        # consultation with no rebuild. services.advise is the live on/off
        # seam (the run_workflow pattern): the tool's prepare hook and the
        # steering-instructions closure both read it per request.
        self._advisor_env_default = cfg.advisor_model
        self.advisor_model_id: str | None = None
        self.deps.advisor_max_uses = cfg.advisor_max_uses
        self._advise_fn = make_advisor(
            self._build_advisor_model,
            lambda: self.advisor_model_id,
            cwd=str(deps.workspace.root),
            max_tokens=cfg.advisor_max_tokens,
        )
        self._apply_saved_advisor()
```

New methods, placed directly after `set_model`/`_wire_cli_model` (line ~635):

```python
    def _build_advisor_model(self, model_id: str) -> Model:
        """Build the advisor's model: through the active model source when one
        exists (cross-provider qualified slugs, the same routing /model uses),
        else pydantic-ai's stock ``infer_model`` — so an embedded harness
        without a source can still pass standard model strings to
        ``with_advisor``. Errors propagate to make_advisor, which folds them
        into the advice-unavailable string."""
        if self.model_source is not None:
            return self.model_source.build(model_id)
        from pydantic_ai.models import infer_model

        return infer_model(model_id)

    def _resolve_advisor_id(self) -> str | None:
        """Session override → env/config default → None. The "off" sentinel is
        itself an override: it beats a configured default, so an explicit
        disable survives restarts distinguishably from "unset"."""
        saved = self.session.saved_advisor_id
        if saved == ADVISOR_OFF:
            return None
        return saved or self._advisor_env_default

    def _apply_saved_advisor(self) -> None:
        """Point the advisor seam at the active session's choice. Called at
        build and after every session change (resume/new/switch), mirroring
        ``_apply_saved_model``."""
        self.advisor_model_id = self._resolve_advisor_id()
        if self.deps.services is not None:
            self.deps.services.advise = (
                self._advise_fn if self.advisor_model_id is not None else None
            )

    def set_advisor_model(self, model_id: str | None, *, persist: bool = True) -> None:
        """Switch the advisor at runtime (None = disable). Unlike set_model
        this is safe mid-turn: resolution is per-consultation, so a switch
        simply applies to the next advisor call; the prepare hook and the
        steering block follow ``services.advise`` on the next model request
        (breaking the prompt cache once — inherent to a client-side advisor)."""
        self.advisor_model_id = model_id
        if self.deps.services is not None:
            self.deps.services.advise = (
                self._advise_fn if model_id is not None else None
            )
        if persist:
            self.session.set_advisor(model_id if model_id is not None else ADVISOR_OFF)
```

Session-change hooks — add `self._apply_saved_advisor()` in three places:

- `resume()` (line 544): after `self._apply_saved_model()`
- `new_session()` (line 565): after the saved-model `if` block, add `self._apply_saved_advisor()`
- `switch_session()` (line 575): after `self._apply_saved_model()`

- [ ] **Step 4: Implement — builder + bootstrap + docs**

`src/marim_harness/runtime/builder.py`, after `with_hooks` (line ~161):

```python
    def with_advisor(self, model: str, *, max_tokens: int = 2048,
                     max_uses: int | None = None) -> HarnessBuilder:
        """Configure an advisor: a model the main agent can consult mid-task
        via the ``advisor`` tool (the full transcript is forwarded to it).
        ``model`` is a pydantic-ai model string, or a qualified
        ``provider:slug`` when a model_source override is composed.
        ``max_tokens`` caps each consultation's output; ``max_uses`` caps
        calls per turn (None = unlimited)."""
        return self.with_config_overrides(
            advisor_model=model,
            advisor_max_tokens=max_tokens,
            advisor_max_uses=max_uses,
        )
```

`src/marim_harness/runtime/bootstrap.py`, inside `.with_config_overrides(...)` after `subagent_tiers=cfg.subagent.tiers,` (line ~186):

```python
            advisor_model=cfg.advisor_model,
            advisor_max_tokens=cfg.advisor_max_tokens,
            advisor_max_uses=cfg.advisor_max_uses,
```

`docs/embedding.md`: in the with_* composition section, add (adapt heading level to the surrounding doc):

```markdown
### with_advisor(model, *, max_tokens=2048, max_uses=None)

Gives the main agent an `advisor` tool: calling it forwards the full
conversation transcript to `model` (a pydantic-ai model string) and returns
its strategic guidance as the tool result. The tool is only advertised while
an advisor is configured (`harness.set_advisor_model(None)` disables it live),
advice output is capped at `max_tokens`, and `max_uses` caps calls per turn.
Advisor failures come back as text inside the tool result — a broken advisor
never fails the turn. Note: the transcript is sent to `model`'s provider.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_advisor_wiring.py tests/test_builder.py tests/test_bootstrap.py tests/test_agent.py -q`
Expected: all pass.

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/harness.py src/marim_harness/runtime/builder.py \
    src/marim_harness/runtime/bootstrap.py docs/embedding.md tests/test_advisor_wiring.py
git commit -m "feat(advisor): harness wiring — config, live setter, with_advisor, bootstrap"
```

---

### Task 6: Soft-steering instructions

**Files:**
- Modify: `src/marim_harness/runtime/instructions.py` (module-level closure near line 306; `gated` list at line 365-376)
- Test: `tests/test_advisor_instructions.py`

**Interfaces:**
- Consumes: `ADVISOR_GUIDANCE` (Task 1); `services.advise` seam (Task 3).
- Produces: `_advisor_guidance(ctx) -> str` (module-level, unit-testable).

- [ ] **Step 1: Write the failing test**

Create `tests/test_advisor_instructions.py`:

```python
"""The advisor steering block is gated on the same seam as the tool itself."""

from types import SimpleNamespace

from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.instructions import _advisor_guidance


def _ctx(tmp_path):
    return SimpleNamespace(deps=Deps(workspace=WorkspaceConfig(root=tmp_path)))


def test_empty_when_no_advisor(tmp_path):
    assert _advisor_guidance(_ctx(tmp_path)) == ""


def test_guidance_when_advisor_configured(tmp_path):
    ctx = _ctx(tmp_path)

    async def advise(messages):
        return "x"

    ctx.deps.services.advise = advise
    text = _advisor_guidance(ctx)
    assert "advisor" in text
    assert "transcript" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_advisor_instructions.py -v`
Expected: FAIL with `ImportError: cannot import name '_advisor_guidance'`

- [ ] **Step 3: Implement**

In `src/marim_harness/runtime/instructions.py`, add the import near the top with the other package imports:

```python
from ..advisor import ADVISOR_GUIDANCE
```

Add the module-level closure after `_agent_index` (line ~306):

```python
def _advisor_guidance(ctx: RunContext[Deps]) -> str:
    # Gated on the SAME seam that gates the tool (the prepare hook in
    # tools/advisor_tools.py), so the prompt can never advertise a tool that
    # isn't in the schema, or vice versa. Toggling the advisor mid-session
    # therefore changes both together — one prompt-cache break per toggle,
    # inherent to a client-side advisor and accepted (see the design spec).
    if ctx.deps.services.advise is None:
        return ""
    return ADVISOR_GUIDANCE
```

In `register_instructions`, add to the `gated` list after `(spawn_on, _agent_index),` (line ~372):

```python
        (True, _advisor_guidance),
```

(Unconditionally registered like `_project_instructions` — the runtime seam is the gate, not a ToolGroups flag, because the tool registers unconditionally too.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_advisor_instructions.py tests/test_agent_instructions.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/runtime/instructions.py tests/test_advisor_instructions.py
git commit -m "feat(advisor): soft-steering prompt block gated on the advise seam"
```

---

### Task 7: `/advisor` command, picker, startup notice

**Files:**
- Modify: `src/marim_harness/interfaces/tui/commands.py` (handler after `_cmd_model` line ~174; `COMMANDS` entry after `"model"` line 504)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`open_advisor_picker`/`_on_advisor_chosen` after `_on_advisor_chosen`'s sibling `_on_model_chosen` line ~829; startup notice in `on_mount` after `self._render_queue()` line 220)
- Test: `tests/test_advisor_command.py`

**Interfaces:**
- Consumes: `Harness.set_advisor_model`, `Harness.advisor_model_id` (Task 5).
- Produces: `HarnessApp.open_advisor_picker()` (used by the command); `/advisor` in `COMMANDS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_advisor_command.py`:

```python
"""/advisor dispatch: off, direct slug, and blank-opens-picker."""

from types import SimpleNamespace

import pytest

from marim_harness.interfaces.tui.commands import COMMANDS_BY_NAME, dispatch


class _App:
    def __init__(self):
        self.posted: list[str] = []
        self.picker_opened = False
        self.advisor_calls: list = []
        self.harness = SimpleNamespace(
            set_advisor_model=lambda mid: self.advisor_calls.append(mid),
        )

    async def post_system(self, msg: str) -> None:
        self.posted.append(msg)

    async def open_advisor_picker(self) -> None:
        self.picker_opened = True


def test_advisor_command_registered():
    assert "advisor" in COMMANDS_BY_NAME


@pytest.mark.anyio
async def test_advisor_off_disables_and_confirms():
    app = _App()
    await dispatch(app, "/advisor off")
    assert app.advisor_calls == [None]
    assert any("off" in p for p in app.posted)


@pytest.mark.anyio
async def test_advisor_with_slug_sets_it():
    app = _App()
    await dispatch(app, "/advisor openrouter:anthropic/claude-opus-4.8")
    assert app.advisor_calls == ["openrouter:anthropic/claude-opus-4.8"]
    assert any("openrouter:anthropic/claude-opus-4.8" in p for p in app.posted)


@pytest.mark.anyio
async def test_advisor_blank_opens_picker():
    app = _App()
    await dispatch(app, "/advisor")
    assert app.picker_opened
    assert app.advisor_calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_advisor_command.py -v`
Expected: FAIL — `"advisor" in COMMANDS_BY_NAME` is False; dispatch posts "Unknown command".

- [ ] **Step 3: Implement — commands.py**

After `_cmd_model` (line ~174):

```python
async def _cmd_advisor(app: HarnessApp, arg: str) -> None:
    # Unlike /model, no mid-turn refusal: the advisor model is resolved per
    # consultation, so a switch simply applies to the next advisor call.
    arg = arg.strip()
    if arg.lower() == "off":
        app.harness.set_advisor_model(None)
        await app.post_system("Advisor: **off** (persisted for this session)")
        return
    if arg:
        app.harness.set_advisor_model(arg)
        await app.post_system(f"Advisor: `{arg}` — applies to the next consultation.")
        return
    await app.open_advisor_picker()
```

In `COMMANDS` after the `"model"` entry (line 504):

```python
    Command("advisor", "set the advisor model: /advisor [id|off] (picker if blank)", _cmd_advisor),
```

- [ ] **Step 4: Implement — app.py**

After `_on_model_chosen` (line ~829):

```python
    async def open_advisor_picker(self) -> None:
        """Model picker for the advisor. Mirrors open_model_picker, but the
        choice lands on the advisor seam (session-persisted) rather than the
        live turn model."""
        source = self.harness.model_source
        if source is None:
            await self.post_system("Model switching isn't available here.")
            return
        self.push_screen(
            ModelPickerModal(
                current=self.harness.advisor_model_id,
                fetch=source.list_models,
                is_local=source.is_local,
            ),
            self._on_advisor_chosen,
        )

    def _on_advisor_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        self.harness.set_advisor_model(chosen)
        self._append_log(NoticeMessage(f"advisor: {chosen}"))
```

(`ModelPickerModal` and `NoticeMessage` are already imported in app.py — they're used by `open_model_picker`/`_on_model_chosen`.)

In `on_mount`, after `self._render_queue()` (line 220):

```python
        # One-line advisor status at session start, so an active advisor (env
        # default or session-persisted) is visible without opening settings.
        if self.harness.advisor_model_id is not None:
            self._append_log(
                NoticeMessage(f"Advisor: {self.harness.advisor_model_id} · /advisor")
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_advisor_command.py tests/test_commands.py tests/test_app.py -q`
Expected: all pass. (If a `test_commands.py` help-listing test asserts an exact command count, update it for the new entry.)

- [ ] **Step 6: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/commands.py src/marim_harness/interfaces/tui/app.py tests/test_advisor_command.py
git commit -m "feat(advisor): /advisor command, picker, and startup status line"
```

---

### Task 8: Settings-screen row + numeric knobs

**Files:**
- Modify: `src/marim_harness/interfaces/tui/settings.py` (`_ENV_INT_INPUTS` line 77-84; `_ZERO_OK_INPUTS` line 88; `_tools_widgets` after the tier rows line ~436; `on_button_pressed` line 563-568; picker/apply methods after `_on_tier_chosen` line ~784; value-text helper after `_tier_value_text` line ~561)
- Test: `tests/test_settings_screen.py` (append)

**Interfaces:**
- Consumes: `ModelConfig.advisor_model/advisor_max_tokens/advisor_max_uses` (Task 2, via `env_cfg`); `save_env_settings` (already imported in settings.py); `MultiModelSource.refresh_from_env`.
- Produces: widget ids `advisor-value`, `advisor-change`, `advisor-max-tokens`, `advisor-max-uses`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings_screen.py` (reuses the file's existing `_Host`, `_fake_harness`, `_env_cfg`, `isolated_env` helpers):

```python
@pytest.mark.anyio
async def test_settings_has_advisor_row_defaulting_off():
    app = _Host(_fake_harness(), _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        value = str(app.screen.query_one("#advisor-value").render())
    assert value == "off"


@pytest.mark.anyio
async def test_advisor_choice_saves_env_and_refreshes_catalog(
    isolated_env, monkeypatch, tmp_path
):
    from marim_harness.config.model import MultiModelSource

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-advisor-test")
    multi = MultiModelSource.from_env()
    refresh_calls = []
    monkeypatch.setattr(multi, "refresh_from_env", lambda: refresh_calls.append(True))
    harness = _fake_harness()
    harness.model_source = multi
    app = _Host(harness, _env_cfg())
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen._on_advisor_chosen("openrouter:advisor-model")
        await pilot.pause()
        value = str(app.screen.query_one("#advisor-value").render())
    env_text = (tmp_path / "marim" / ".env").read_text()
    assert "MARIM_ADVISOR_MODEL=openrouter:advisor-model" in env_text
    assert os.environ.get("MARIM_ADVISOR_MODEL") == "openrouter:advisor-model"
    assert refresh_calls == [True]
    assert value == "openrouter:advisor-model"


@pytest.mark.anyio
async def test_advisor_off_choice_drops_the_env_var(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("MARIM_ADVISOR_MODEL", "openrouter:old-advisor")
    env_cfg = _env_cfg()
    env_cfg.advisor_model = "openrouter:old-advisor"
    app = _Host(_fake_harness(), env_cfg)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        app.screen.active_section = "tools"
        await pilot.pause()
        app.screen._on_advisor_chosen("off")
        await pilot.pause()
        value = str(app.screen.query_one("#advisor-value").render())
    assert os.environ.get("MARIM_ADVISOR_MODEL") is None
    assert value == "off"


@pytest.mark.anyio
async def test_advisor_numeric_knobs_are_registered():
    from marim_harness.interfaces.tui.settings import _ENV_INT_INPUTS, _ZERO_OK_INPUTS

    assert _ENV_INT_INPUTS["advisor-max-tokens"][0] == "MARIM_ADVISOR_MAX_TOKENS"
    assert _ENV_INT_INPUTS["advisor-max-uses"][0] == "MARIM_ADVISOR_MAX_USES"
    # 0 = unlimited must be commit-able, like the context budget's 0.
    assert "advisor-max-uses" in _ZERO_OK_INPUTS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_settings_screen.py -k advisor -v`
Expected: FAIL with `NoMatches` on `#advisor-value` / `KeyError: 'advisor-max-tokens'`

- [ ] **Step 3: Implement**

Registries (lines 77-88):

```python
_ENV_INT_INPUTS: dict[str, tuple[str, str]] = {
    "ctx-input": ("MARIM_CONTEXT_BUDGET", "Context budget"),
    "toolsearch-threshold": ("MARIM_TOOL_SEARCH_THRESHOLD", "Tool-search threshold"),
    "mask-keep-recent": ("MARIM_MASK_KEEP_RECENT", "Mask: keep recent returns"),
    "mask-min-chars": ("MARIM_MASK_MIN_CHARS", "Mask: min chars to elide"),
    "subagent-req-limit": ("MARIM_SUBAGENT_REQUEST_LIMIT", "Sub-agent request limit"),
    "wake-depth-cap": ("MARIM_WAKE_DEPTH_CAP", "Autonomous wake turns"),
    "advisor-max-tokens": ("MARIM_ADVISOR_MAX_TOKENS", "Advisor max tokens"),
    "advisor-max-uses": ("MARIM_ADVISOR_MAX_USES", "Advisor max uses/turn"),
}
```

```python
# Integer inputs whose domain includes 0. The context budget's label promises
# "0 = unbudgeted"; the advisor per-turn cap's promises "0 = unlimited".
_ZERO_OK_INPUTS = frozenset({"ctx-input", "advisor-max-uses"})
```

In `_tools_widgets`, after the tier-rows `for` loop (line ~436):

```python
        yield Static(
            "Advisor — a model the agent can consult mid-task for strategic "
            "guidance (the advisor tool). This row saves the global default "
            "to .env (new sessions); /advisor overrides it per session, live. "
            "Type 'off' in the picker to clear it. Uses/turn: 0 = unlimited.",
            classes="muted",
        )
        with Horizontal(classes="srow"):
            yield Static("Advisor", classes="tier-row-label")
            yield Static(
                self._advisor_value_text(), id="advisor-value", classes="tier-row-value"
            )
            yield Button(
                "change", id="advisor-change", variant="primary", compact=True
            )
        with Horizontal(classes="frow"):
            yield Label("Advisor max tokens")
            yield Input(
                value=str(self.env_cfg.advisor_max_tokens),
                id="advisor-max-tokens",
                type="integer",
            )
        with Horizontal(classes="frow"):
            yield Label("Advisor max uses/turn")
            yield Input(
                value=str(self.env_cfg.advisor_max_uses or 0),
                id="advisor-max-uses",
                type="integer",
            )
```

Helper after `_tier_value_text` (line ~561):

```python
    def _advisor_value_text(self) -> str:
        return self.env_cfg.advisor_model or "off"
```

`on_button_pressed` (line 563-568): add a branch:

```python
        elif bid == "advisor-change":
            self._open_advisor_picker()
```

Picker/apply methods after `_on_tier_chosen` (line ~784):

```python
    def _open_advisor_picker(self) -> None:
        """Model picker for the global advisor default. Mirrors the tier rows:
        the pick persists to .env (new sessions); the live per-session switch
        is /advisor. Typing ``off`` in the picker clears the default."""
        source = self.harness.model_source
        if source is None:
            self.query_one("#advisor-value", Static).update(
                "Model switching isn't available here."
            )
            return
        self.app.push_screen(
            ModelPickerModal(
                current=self.env_cfg.advisor_model,
                fetch=source.list_models,
                is_local=source.is_local,
            ),
            self._on_advisor_chosen,
        )

    def _on_advisor_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        try:
            if chosen.strip().lower() == "off":
                # An explicit off DROPS the var rather than writing a
                # sentinel: unset is the env layer's own "no advisor", and a
                # written "off" would round-trip as a bogus model slug.
                save_env_settings({}, drop=("MARIM_ADVISOR_MODEL",))
                self.env_cfg.advisor_model = None
            else:
                save_env_settings({"MARIM_ADVISOR_MODEL": chosen})
                self.env_cfg.advisor_model = chosen
        except Exception as exc:  # surface any write failure on the status line
            self._status(f"Save failed: {exc}")
            return
        source = self.harness.model_source
        if isinstance(source, MultiModelSource):
            source.refresh_from_env()
        self.query_one("#advisor-value", Static).update(self._advisor_value_text())
        self._status("✓ saved MARIM_ADVISOR_MODEL · applies to new sessions")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_settings_screen.py -q`
Expected: all pass (pre-existing settings tests included).

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/settings.py tests/test_settings_screen.py
git commit -m "feat(advisor): settings-screen advisor row and numeric knobs"
```

---

### Task 9: Transcript rendering — standalone advisor card

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py` (`_TopLevelSink.intercept_tool`, lines 310-334)
- Test: `tests/test_advisor_render.py`

**Interfaces:**
- Produces: `_STANDALONE_TOOLS: frozenset[str]` module constant in `stream_render.py` (the membership set the intercept checks).

- [ ] **Step 1: Write the failing test**

Create `tests/test_advisor_render.py`:

```python
"""The advisor renders standalone (outside the collapsed tool-run group), the
same treatment ask_user gets — the advice is conversation content, not
mechanical work to fold behind a '≡ N tools' group."""

from marim_harness.interfaces.tui.stream_render import _STANDALONE_TOOLS


def test_advisor_and_ask_user_render_standalone():
    assert "advisor" in _STANDALONE_TOOLS
    assert "ask_user" in _STANDALONE_TOOLS
    # spawn_agent has its own SubAgentWidget claim path, not this one.
    assert "spawn_agent" not in _STANDALONE_TOOLS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_advisor_render.py -v`
Expected: FAIL with `ImportError: cannot import name '_STANDALONE_TOOLS'`

- [ ] **Step 3: Implement**

In `src/marim_harness/interfaces/tui/stream_render.py`, add a module-level constant near the top of the file (with the module's other constants):

```python
# Tools whose calls mount standalone in the main log instead of joining the
# collapsed tool-run group: user-facing conversation content (a question and
# its answer; the advisor's guidance) would otherwise hide behind a
# "≡ N tools" fold. spawn_agent is NOT here — it has its own claim path that
# builds a live SubAgentWidget (see _TopLevelSink.intercept_tool).
_STANDALONE_TOOLS = frozenset({"ask_user", "advisor"})
```

In `_TopLevelSink.intercept_tool` (line 325), change the `ask_user` branch condition and its comment:

```python
        # ask_user (a user-facing Q&A) and advisor (the reviewer's guidance)
        # are conversation content, not mechanical work — keep them out of the
        # collapsed tool group, where they'd hide behind a "≡ N tools" fold.
        # Render a normal tool widget but mount it standalone and break the
        # run on both sides (same rationale as the foreground spawn_agent
        # case above).
        if event.part.tool_name in _STANDALONE_TOOLS:
```

(The widget-construction body below the condition is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_advisor_render.py tests/test_ask_user_render.py tests/test_app.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run ruff check src tests && uv run pyright
git add src/marim_harness/interfaces/tui/stream_render.py tests/test_advisor_render.py
git commit -m "feat(advisor): render advisor calls standalone in the transcript"
```

---

### Task 10: Docs, full CI parity, wrap-up

**Files:**
- Modify: `CLAUDE.md` (Supporting subsystems section)
- No new tests — this task is the full-suite gate.

- [ ] **Step 1: CLAUDE.md**

In `CLAUDE.md`, add one bullet to "Supporting subsystems (one concern each)" after the `workflows/` bullet:

```markdown
- `advisor.py` (root) — the advisor: an `advisor()` tool on the main agent
  forwards the full transcript to a separately-configured model
  (`MARIM_ADVISOR_MODEL`, any provider) and returns strategic guidance.
  Live seam is `services.advise` (a pydantic-ai `prepare` hook omits the tool
  when it's `None`, so `/advisor <model>`/`/advisor off` toggle without a
  rebuild — at the cost of one prompt-cache break per toggle). Session
  persistence mirrors `store.model` (`"off"` sentinel = explicitly disabled);
  per-turn call cap `MARIM_ADVISOR_MAX_USES` rides on `Deps`. Main loop only
  (sub-agents have tiering); the tool doesn't exist under the claude-cli
  main-loop provider (marim's tools don't apply there), but a claude-cli
  *advisor* model works via the `aux_model_for` clone.
```

- [ ] **Step 2: Full CI parity run**

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```

Expected: all three clean/green. Fix anything that surfaces (coverage runs by default here — that's fine).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(advisor): CLAUDE.md subsystem note"
```

- [ ] **Step 4: Manual smoke recipe (record in PR notes, do NOT run unattended)**

Free-local-only smoke (per the no-paid-models rule; requires LM Studio serving `ornith-1.0-9b`):

```bash
MARIM_PROVIDER=local MARIM_ADVISOR_MODEL=local:ornith-1.0-9b uv run marim
# In-session: confirm the startup "Advisor: local:ornith-1.0-9b · /advisor" line,
# ask for a small task and watch for a standalone advisor card, then
# /advisor off → confirm next turn's model no longer sees the tool.
```

---

## Out of scope (tracked in the spec)

Sub-agent advisor inheritance; advisor-side prompt caching; nudge injection; hard-rule enforcement.
