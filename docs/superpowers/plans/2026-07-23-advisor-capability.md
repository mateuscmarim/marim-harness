# Advisor Capability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export marim's advisor as `marim_harness.capabilities.Advisor`, a standard pydantic-ai capability, with marim's own advisor sharing the consult logic (spec: `docs/superpowers/specs/2026-07-23-advisor-capability-design.md`).

**Architecture:** Two parts. (1) Extract the consult logic from `make_advisor`'s inner closure into a module-level `consult()` in `advisor.py`; `make_advisor` keeps only marim's wrapper concerns (live model id, `aux_model_for` clone). (2) New `capabilities/` package with an `Advisor(AbstractCapability)` that exposes one `advisor` tool + the existing guidance instructions, calling the same `consult()`. Marim's runtime wiring is untouched.

**Tech Stack:** pydantic-ai (`AbstractCapability`, `FunctionToolset`, `infer_model`, `TestModel`/`FunctionModel`), pytest + anyio.

## Global Constraints

- Python `>=3.10` — no 3.11+-only syntax.
- Run everything through `uv` (`uv run pytest`, `uv run ruff`, `uv run pyright`). Never bare `python`/`pip`.
- Ruff line length 100; complexity cap C901=10; import sorting on.
- No new dependencies — everything used ships with `pydantic-ai-slim>=2.8,<3` (already a core dep).
- Existing `tests/test_advisor.py` must pass UNCHANGED — that is the proof the `consult()` extraction is faithful.
- Errors-as-text contract everywhere in advisor code: failure paths return strings, never raise.
- All work on branch `feat/advisor-capability` (already created off `origin/master`).
- Worktree gotcha: use worktree-absolute paths (`/home/mateuscmarim/Projects/marim.dev/marim-harness/.claude/worktrees/oss-hygiene/...`) for all file edits.

---

### Task 1: Extract `consult()` shared core in `advisor.py`

**Files:**
- Modify: `src/marim_harness/advisor.py` (extract `consult` from `make_advisor`'s inner `advise`)
- Test: `tests/test_advisor.py` (append two tests; existing tests untouched)

**Interfaces:**
- Consumes: existing `_ADVISOR_INSTRUCTIONS`, `_advise_prompt`, `_CLIP_ATTEMPTS`, `render_transcript` (all already in `advisor.py`).
- Produces: `async def consult(model: Model, messages: list, *, max_tokens: int = 2048) -> str` — Task 2 imports this from `..advisor`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_advisor.py`:

```python
@pytest.mark.anyio
async def test_consult_returns_advice_with_usage_trailer():
    out = await consult(TestModel(custom_output_text="Do X first."), [])
    assert out.startswith("Do X first.")
    assert "[advisor usage:" in out


@pytest.mark.anyio
async def test_consult_failure_twice_returns_error_string():
    def always_broken(messages, info):
        raise RuntimeError("boom")

    out = await consult(FunctionModel(always_broken), [])
    assert out.startswith("Advisor unavailable:")
    assert "Continue without advice" in out
```

Extend the existing import block at the top of the file (it already imports
from `marim_harness.advisor`) to also import `consult`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_advisor.py -q`
Expected: ImportError — `cannot import name 'consult'` (existing tests still pass if run without the import; the import failure is the RED signal).

- [ ] **Step 3: Implement `consult()` and delegate `make_advisor` to it**

In `src/marim_harness/advisor.py`, insert after `_advise_prompt` (module level):

```python
async def consult(model: Model, messages: list, *, max_tokens: int = 2048) -> str:
    """One advisor consultation: render the transcript, run a tool-free
    one-shot agent on ``model``, return advice text with a usage trailer.

    The shared core between marim's own advisor (``make_advisor``) and the
    exported ``marim_harness.capabilities.Advisor`` pydantic-ai capability —
    keep it free of marim runtime concerns (no Deps, no services, no model
    resolution). Every failure path returns a short actionable string, never
    raises (the errors-as-text contract in the module docstring)."""
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
        usage = result.usage
        return (
            f"{result.output}\n\n"
            f"[advisor usage: {usage.input_tokens or 0} in, "
            f"{usage.output_tokens or 0} out tokens]"
        )
    return f"Advisor unavailable: {last_error}. Continue without advice."
```

Then shrink `make_advisor`'s inner `advise` to (replacing everything from
`agent = Agent(...)` through the final `return f"Advisor unavailable: ..."`):

```python
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
        return await consult(model, messages, max_tokens=max_tokens)
```

`Model` is currently under `TYPE_CHECKING` only; `consult`'s signature uses it
in an annotation, which stays valid under `from __future__ import annotations`
(already present). No import changes needed.

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest --no-cov tests/test_advisor.py -q`
Expected: ALL PASS (the pre-existing tests prove the extraction is faithful).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/advisor.py tests/test_advisor.py
git commit -m "refactor(advisor): extract consult() shared core from make_advisor"
```

---

### Task 2: `Advisor` capability — attach surface + consult round-trip

**Files:**
- Create: `src/marim_harness/capabilities/__init__.py`
- Create: `src/marim_harness/capabilities/advisor.py`
- Test: `tests/test_capability_advisor.py` (new)

**Interfaces:**
- Consumes: `consult` from Task 1; `ADVISOR_GUIDANCE` (existing constant in `advisor.py`).
- Produces: `class Advisor(AbstractCapability)` with `__init__(model: Model | str, *, max_uses: int | None = 5, max_tokens: int = 2048, id: str | None = None, description: str | None = None, defer_loading: bool = False)`, importable as `from marim_harness.capabilities import Advisor`. Task 3 extends this class in place.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_capability_advisor.py`:

```python
"""The exported Advisor capability. TestModel/FunctionModel only — no live
providers. The main-agent FunctionModel scripts count ToolReturnParts to
decide whether to call the advisor tool again or finish."""

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from marim_harness.advisor import ADVISOR_GUIDANCE
from marim_harness.capabilities import Advisor


def _consult_once_main():
    """A main model that calls the advisor tool once, then finishes."""

    def fn(messages, info):
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("advisor", {})])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


def _advisor_returns(text):
    return TestModel(custom_output_text=text)


def _advisor_tool_returns(result):
    return [
        p
        for m in result.all_messages()
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == "advisor"
    ]


@pytest.mark.anyio
async def test_capability_exposes_tool_and_guidance():
    seen = {}

    def capture(messages, info):
        seen["tools"] = [t.name for t in info.function_tools]
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(
        FunctionModel(capture),
        capabilities=[Advisor(model=_advisor_returns("advice"))],
    )
    result = await agent.run("hi")
    assert "advisor" in seen["tools"]
    assert ADVISOR_GUIDANCE in (result.all_messages()[0].instructions or "")


@pytest.mark.anyio
async def test_advisor_tool_round_trip_with_usage_trailer():
    agent = Agent(
        _consult_once_main(),
        capabilities=[Advisor(model=_advisor_returns("Check edge cases first."))],
    )
    result = await agent.run("build the thing")
    (ret,) = _advisor_tool_returns(result)
    assert "Check edge cases first." in ret.content
    assert "[advisor usage:" in ret.content
    assert result.output == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_capability_advisor.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.capabilities'`.

- [ ] **Step 3: Implement the capability**

Create `src/marim_harness/capabilities/__init__.py`:

```python
"""Capabilities marim exports for use with ANY pydantic-ai agent.

These are standard pydantic-ai ``AbstractCapability`` implementations —
attach them via ``Agent(capabilities=[...])`` or marim's
``HarnessBuilder.with_capability``. They deliberately depend only on
pydantic-ai plus marim's pure helpers, never on marim's runtime (Deps,
services, TUI)."""

from .advisor import Advisor

__all__ = ["Advisor"]
```

Create `src/marim_harness/capabilities/advisor.py`:

```python
"""The Advisor capability: marim's advisor pattern, exported for any
pydantic-ai agent.

Bundles one ``advisor`` tool (forwards the run's transcript to a
separately-configured reviewer model and returns strategic guidance) with
the guidance instructions telling the model when to consult it. The consult
logic itself is ``advisor.consult`` — the same core marim's own runtime
advisor uses, so the two can never drift.

Unlike marim's runtime advisor (live ``/advisor`` toggling, session
persistence, claude-cli clone handling), this capability is statically
configured: one model, fixed caps. Embedders using ``HarnessBuilder`` should
pick ONE of ``with_advisor(...)`` / ``with_capability(Advisor(...))`` —
attaching both registers two tools named ``advisor`` and pydantic-ai will
reject the duplicate at run time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import Model, infer_model
from pydantic_ai.toolsets import FunctionToolset

from ..advisor import ADVISOR_GUIDANCE, consult


@dataclass(init=False)
class Advisor(AbstractCapability[Any]):
    """Consult a separately-configured, typically stronger model mid-task.

    ```python
    from pydantic_ai import Agent
    from marim_harness.capabilities import Advisor

    agent = Agent(
        "anthropic:claude-sonnet-4-6",
        capabilities=[Advisor(model="openai:gpt-5.2", max_uses=5)],
    )
    ```
    """

    model: Model | str
    """The advisor model. A string resolves via ``infer_model`` lazily on the
    first consultation, so constructing the capability never needs provider
    credentials."""

    max_uses: int | None
    """Per-run cap on consultations; ``None`` = unlimited."""

    max_tokens: int
    """Advice budget forwarded to the one-shot advisor run."""

    def __init__(
        self,
        model: Model | str,
        *,
        max_uses: int | None = 5,
        max_tokens: int = 2048,
        id: str | None = None,
        description: str | None = None,
        defer_loading: bool = False,
    ) -> None:
        self.id = id
        self.description = description
        self.defer_loading = defer_loading
        self.model = model
        self.max_uses = max_uses
        self.max_tokens = max_tokens
        self._uses = 0
        self._resolved: Model | None = None

    def _resolve_model(self) -> Model:
        # Lazy + cached: a string slug is only resolved (and only needs
        # credentials) when the tool is first called, per the design spec.
        if self._resolved is None:
            m = self.model
            self._resolved = m if isinstance(m, Model) else infer_model(m)
        return self._resolved

    def get_instructions(self):
        return ADVISOR_GUIDANCE

    def get_toolset(self):
        toolset: FunctionToolset[Any] = FunctionToolset()

        @toolset.tool
        async def advisor(ctx: RunContext[Any]) -> str:
            """Consult your advisor: a stronger reviewer model that sees this
            entire conversation — the task, your reasoning, and every tool
            call and result — and returns strategic guidance.

            Call it before starting substantive work on a non-trivial task,
            when you are stuck or about to make a risky change, and before
            declaring a complex task done. It takes no arguments; the
            transcript is forwarded automatically. The advice is guidance to
            weigh against your own evidence, not an instruction to follow
            blindly.
            """
            if self.max_uses is not None and self._uses >= self.max_uses:
                return (
                    f"Advisor call cap reached (max_uses={self.max_uses} per "
                    "run). Continue without advice."
                )
            try:
                model = self._resolve_model()
            except Exception as exc:
                # Errors-as-text: a broken advisor degrades the advice,
                # never the run.
                return (
                    f"Advisor unavailable: can't build model {self.model!r}: "
                    f"{exc}. Continue without advice."
                )
            self._uses += 1
            return await consult(model, list(ctx.messages), max_tokens=self.max_tokens)

        return toolset
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_capability_advisor.py tests/test_advisor.py -q`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/capabilities tests/test_capability_advisor.py
git commit -m "feat(capabilities): export the advisor as a pydantic-ai capability"
```

---

### Task 3: Per-run cap isolation, error text, defer_loading

**Files:**
- Modify: `src/marim_harness/capabilities/advisor.py` (add `for_run`)
- Test: `tests/test_capability_advisor.py` (append four tests)

**Interfaces:**
- Consumes: `Advisor` from Task 2 (same module, extended in place).
- Produces: final `Advisor` behavior — per-run `max_uses` isolation via `for_run`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capability_advisor.py`:

```python
def _consult_twice_main():
    """A main model that calls the advisor tool until it has two returns."""

    def fn(messages, info):
        returns = [
            p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)
        ]
        if len(returns) < 2:
            return ModelResponse(parts=[ToolCallPart("advisor", {})])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


@pytest.mark.anyio
async def test_max_uses_caps_consultations_within_a_run():
    calls = {"n": 0}

    def advisor_model(messages, info):
        calls["n"] += 1
        return ModelResponse(parts=[TextPart("advice")])

    agent = Agent(
        _consult_twice_main(),
        capabilities=[Advisor(model=FunctionModel(advisor_model), max_uses=1)],
    )
    result = await agent.run("go")
    assert calls["n"] == 1  # second consult refused before reaching the model
    first, second = _advisor_tool_returns(result)
    assert "advice" in first.content
    assert "max_uses=1" in second.content
    assert "Continue without advice" in second.content


@pytest.mark.anyio
async def test_max_uses_resets_on_the_next_run():
    calls = {"n": 0}

    def advisor_model(messages, info):
        calls["n"] += 1
        return ModelResponse(parts=[TextPart("advice")])

    agent = Agent(
        _consult_once_main(),
        capabilities=[Advisor(model=FunctionModel(advisor_model), max_uses=1)],
    )
    await agent.run("first")
    await agent.run("second")
    assert calls["n"] == 2  # for_run gave the second run a fresh counter


@pytest.mark.anyio
async def test_unresolvable_model_returns_text_not_raise():
    agent = Agent(
        _consult_once_main(),
        capabilities=[Advisor(model="not-a-provider:not-a-model")],
    )
    result = await agent.run("go")
    (ret,) = _advisor_tool_returns(result)
    assert "Advisor unavailable" in ret.content
    assert result.output == "done"  # the run completed


@pytest.mark.anyio
async def test_defer_loading_hides_the_tool_until_loaded():
    seen = {}

    def capture(messages, info):
        seen.setdefault("tools", [t.name for t in info.function_tools])
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(
        FunctionModel(capture),
        capabilities=[
            Advisor(model=_advisor_returns("x"), id="advisor", defer_loading=True)
        ],
    )
    await agent.run("hi")
    assert "advisor" not in seen["tools"]
    assert any("load_capability" in name for name in seen["tools"])
```

- [ ] **Step 2: Run tests to verify the reset test fails**

Run: `uv run pytest --no-cov tests/test_capability_advisor.py -q`
Expected: `test_max_uses_resets_on_the_next_run` FAILS (shared instance keeps
`_uses` across runs → `calls["n"] == 1`). The other three may already pass —
that is fine; the reset test is the RED driver. If `test_defer_loading_...`
fails, read the actual `load_capability` tool name from the assertion output
and adjust only the *name string* in the test.

- [ ] **Step 3: Add `for_run` per-run isolation**

In `src/marim_harness/capabilities/advisor.py`, add this method to `Advisor`
(after `__init__`):

```python
    async def for_run(self, ctx: RunContext[Any]) -> Advisor:
        # Fresh instance per run so max_uses is a per-run cap (mirrors
        # marim's per-turn cap). The resolved model is carried over — it is
        # stateless — so a string slug is only resolved once per process.
        fresh = Advisor(
            self.model,
            max_uses=self.max_uses,
            max_tokens=self.max_tokens,
            id=self.id,
            description=self.description,
            defer_loading=self.defer_loading,
        )
        fresh._resolved = self._resolved
        return fresh
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest --no-cov tests/test_capability_advisor.py tests/test_advisor.py -q`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/capabilities/advisor.py tests/test_capability_advisor.py
git commit -m "feat(capabilities): per-run max_uses isolation and error text for Advisor"
```

---

### Task 4: Docs and changelog

**Files:**
- Create: `docs/sdk/capabilities.md`
- Modify: `docs/sdk/README.md` (add index entry)
- Modify: `docs/embedding.md` (cross-link under `with_capability`)
- Modify: `docs/sdk/builder.md` (cross-link near `with_capability` bullet, ~line 82)
- Modify: `CHANGELOG.md` (Unreleased → Added)
- Test: `tests/test_docs_reference.py` (existing doc-lint/link checker — run, don't edit)

**Interfaces:**
- Consumes: `Advisor` API exactly as produced by Tasks 2–3.
- Produces: nothing for later tasks.

- [ ] **Step 1: Create `docs/sdk/capabilities.md`**

```markdown
# Exported capabilities

marim exports parts of itself as standard
[pydantic-ai capabilities](https://ai.pydantic.dev/) so they can be used with
**any** pydantic-ai agent — no marim harness required. They live under
`marim_harness.capabilities` and depend only on pydantic-ai plus marim's pure
helpers, never on marim's runtime.

## Advisor

A second, separately-configured (typically stronger) model the agent can
consult mid-task. Calling the `advisor` tool forwards the run's transcript to
the reviewer model and returns strategic guidance; the capability also adds
instructions telling the model *when* consulting is worth it.

### With a plain pydantic-ai agent

```python
from pydantic_ai import Agent
from marim_harness.capabilities import Advisor

agent = Agent(
    "anthropic:claude-sonnet-4-6",
    capabilities=[Advisor(model="openai:gpt-5.2", max_uses=5)],
)
```

Parameters:

- `model` — the advisor model: a `provider:model` string (resolved lazily on
  the first consultation, so construction never needs credentials) or a
  pydantic-ai `Model` instance.
- `max_uses` — per-run cap on consultations (default `5`; `None` =
  unlimited). Hitting the cap returns a "continue without advice" tool
  result, never an error.
- `max_tokens` — advice budget for the one-shot advisor run (default `2048`).
- Plus the standard capability keywords: `id`, `description`, and
  `defer_loading` (with `defer_loading=True` the tool and its instructions
  stay out of context until the model loads the capability; requires `id`).

Failures follow the errors-as-text contract: a broken or unresolvable advisor
model degrades the advice ("Advisor unavailable: … Continue without
advice."), never the run.

Because `model` is a plain string, the capability also works in
`Agent.from_file` YAML specs:

```yaml
model: anthropic:claude-sonnet-4-6
capabilities:
  - Advisor:
      model: openai:gpt-5.2
```

### With the marim harness

Embedders using [`HarnessBuilder`](builder.md) should pick **one** of:

- `with_advisor(model, ...)` — marim-native: live `/advisor` toggling,
  session persistence, claude-cli handling.
- `with_capability(Advisor(...))` — the portable, statically-configured
  capability described here.

Attaching both registers two tools named `advisor`, which pydantic-ai
rejects at run time.

Both paths share the same consult core (`marim_harness.advisor.consult`), so
behavior cannot drift between them.
```

- [ ] **Step 2: Add the index entry**

In `docs/sdk/README.md`, add to the page list (match the surrounding entry
format exactly; place it after the builder entry):

```markdown
- [Exported capabilities](capabilities.md) — pydantic-ai capabilities marim ships (`Advisor`), usable with any pydantic-ai agent.
```

- [ ] **Step 3: Add cross-links**

In `docs/embedding.md`, at the end of the `### with_capability(capability)`
section (after the existing example around line 60), append:

```markdown
marim also ships its own capabilities to attach here — see
[Exported capabilities](sdk/capabilities.md).
```

In `docs/sdk/builder.md`, extend the `with_capability` bullet (~line 82-88)
with a sentence at its end:

```markdown
  marim's own exported capabilities (like `Advisor`) are listed in
  [Exported capabilities](capabilities.md).
```

- [ ] **Step 4: Changelog**

In `CHANGELOG.md` under the `## [Unreleased]` heading (create an `### Added`
subsection if absent):

```markdown
### Added

- `marim_harness.capabilities.Advisor` — marim's advisor exported as a
  standard pydantic-ai capability, attachable to any pydantic-ai agent (or
  via `HarnessBuilder.with_capability`). Marim's own advisor now shares the
  same consult core, so the two cannot drift.
```

- [ ] **Step 5: Run the doc link checker and docs-adjacent tests**

Run: `uv run pytest --no-cov tests/test_docs_reference.py -q`
Expected: PASS. If it flags the new page or links, fix paths to satisfy it
(the checker is the authority on the docs tree's link format).

- [ ] **Step 6: Commit**

```bash
git add docs/sdk/capabilities.md docs/sdk/README.md docs/embedding.md docs/sdk/builder.md CHANGELOG.md
git commit -m "docs: exported-capabilities page for the Advisor capability"
```

---

### Task 5: Full gate

**Files:** none new — verification only.

- [ ] **Step 1: Lint**

Run: `uv run ruff check src tests`
Expected: clean. Fix anything it flags (import order in new files is the
usual suspect), re-run until clean.

- [ ] **Step 2: Type-check**

Run: `uv run pyright`
Expected: 0 errors. (`get_instructions`/`get_toolset` return types are
covariant with the base class's `| None` returns.)

- [ ] **Step 3: Full test suite**

Run: `uv run pytest`
Expected: ALL PASS with coverage on (matches CI order: ruff → pyright →
pytest).

- [ ] **Step 4: Commit any gate fixes**

```bash
git add -A && git commit -m "fix: gate fixes for advisor capability" # only if fixes were needed
```
