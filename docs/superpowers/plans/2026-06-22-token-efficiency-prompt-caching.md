# Token Efficiency via OpenRouter Prompt Caching — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut redundant input-token cost by enabling Anthropic prefix caching through pydantic-ai's official `OpenRouterModel`, and keep the cached prefix stable across turns.

**Architecture:** Two parts. **Part A** (Task 1) rebases marim's OpenRouter model onto `pydantic_ai.models.openrouter.OpenRouterModel`, enabling its three `cache_control` settings and native usage accounting, while preserving marim's billed-cost capture via a thin `_map_usage` re-inject on both the model and its streamed-response class. **Part B** (Task 2) relocates the volatile task checklist out of the system prompt (where it busts the cache every turn) into the per-turn `<turn-context>` user envelope, so the cached prefix stays stable. Task 3 is end-to-end verification.

**Tech Stack:** Python 3.14, pydantic-ai 1.107.0, `uv` for env/test, pytest.

## Global Constraints

- OpenRouter provider path only. Do **not** touch the `google` or `local` branches of `config/model.py:build_model`.
- pydantic-ai floor: `1.107.0` (ships `OpenRouterModel`, `OpenRouterModelSettings`, `OpenRouterStreamedResponse`).
- Cost capture contract is unchanged: billed cost must still land in `RunUsage.details[COST_DETAIL_KEY]` as integer micro-USD. `usage.py` is **not** modified.
- Caching must fail soft: if the model profile reports no cache support, the settings are ignored by pydantic-ai and behavior is unchanged.
- Run tests with `uv run pytest <path>` (coverage is configured in `pyproject.toml`; the summary line "N passed" appears under the `tests coverage` banner).

## File Structure

- Modify: `src/marim_harness/config/openrouter_cost.py` — rebase model onto `OpenRouterModel`; enable cache settings; thin cost re-inject on model + streamed-response (Part A).
- Modify: `tests/test_openrouter_cost.py` — update the two `build_openrouter_model` assertions for the new settings/base class.
- Modify: `src/marim_harness/instructions.py` — delete the `_task_state` instruction closure (Part B).
- Modify: `src/marim_harness/agent.py` — render the task checklist into `_assemble_prompt`'s turn-context (Part B).
- Modify: `tests/test_agent_instructions.py` — replace the task-state-in-instructions test with one asserting the checklist now rides in the user prompt, not the instructions.

---

### Task 1: Adopt `OpenRouterModel` + enable caching, preserving cost capture

**Files:**
- Modify: `src/marim_harness/config/openrouter_cost.py:48-83` (the `build_openrouter_model` function)
- Test: `tests/test_openrouter_cost.py`

**Interfaces:**
- Consumes: existing module helpers `read_cost_micro_usd(response)` and `_with_cost(response, mapped)` (unchanged, lines 22-45).
- Produces: `build_openrouter_model(model_id: str, api_key: Optional[str])` returning an `OpenRouterModel` subclass instance whose `settings` is an `OpenRouterModelSettings` carrying `openrouter_usage`, `openrouter_cache_instructions`, `openrouter_cache_tool_definitions`, `openrouter_cache_messages`, and whose `_map_usage` re-injects billed cost.

- [ ] **Step 1: Update the two failing tests in `tests/test_openrouter_cost.py`**

Replace `test_build_openrouter_model_requests_usage_accounting` (lines 34-38) and `test_build_openrouter_model_overrides_usage_mapping` (lines 41-47) with:

```python
def test_build_openrouter_model_enables_caching_and_usage():
    # The built model must enable OpenRouter usage accounting and place
    # cache_control breakpoints on instructions, tool defs, and messages.
    model = build_openrouter_model("anthropic/claude-sonnet-4-6", api_key="sk-test")
    s = model.settings
    assert s is not None
    assert s.get("openrouter_usage") == {"include": True}
    assert s.get("openrouter_cache_instructions") == "5m"
    assert s.get("openrouter_cache_tool_definitions") == "5m"
    assert s.get("openrouter_cache_messages") == "5m"


def test_build_openrouter_model_subclasses_openrouter_and_reinjects_cost():
    # It must subclass the official OpenRouterModel (so native cache-token
    # mapping is preserved) and override _map_usage to re-inject billed cost.
    from pydantic_ai.models.openrouter import OpenRouterModel

    model = build_openrouter_model("anthropic/claude-sonnet-4-6", api_key="sk-test")
    assert isinstance(model, OpenRouterModel)
    assert type(model)._map_usage is not OpenRouterModel._map_usage
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_openrouter_cost.py -k "caching_and_usage or reinjects_cost"`
Expected: FAIL — current model sets `extra_body`, not the `openrouter_*` keys, and subclasses `OpenAIChatModel`.

- [ ] **Step 3: Rewrite `build_openrouter_model`**

Replace the function body (`config/openrouter_cost.py:48-83`) with:

```python
def build_openrouter_model(model_id: str, api_key: Optional[str]):
    """An OpenRouter chat model with prompt caching enabled that records the
    provider's billed cost.

    Built on pydantic-ai's official ``OpenRouterModel`` so cache-token mapping
    and OpenRouter usage parsing come natively; the only thing it adds is
    re-injecting the billed ``usage.cost`` (a float the base usage mapper drops)
    into ``RunUsage.details`` as integer micro-USD, where ``usage.py`` reads it.

    Imported lazily (it pulls in provider packages) so config-only code paths
    stay dependency-free."""
    from pydantic_ai.models.openrouter import (
        OpenRouterModel,
        OpenRouterModelSettings,
        OpenRouterStreamedResponse,
    )
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    class _CostStreamedResponse(OpenRouterStreamedResponse):
        # Subclass the OpenRouter streamed response (not the plain OpenAI one)
        # so super()._map_usage still maps cache_read/cache_write tokens.
        def _map_usage(self, response):
            return _with_cost(response, super()._map_usage(response))

    class _CostOpenRouterModel(OpenRouterModel):
        # Non-streaming path (e.g. the summarizer/titler agents).
        def _map_usage(self, response):
            return _with_cost(response, super()._map_usage(response))

        # Streaming path (every interactive/headless turn): swap the live
        # streamed-response instance to the cost-capturing subclass. Both
        # classes share OpenRouterStreamedResponse's layout, so the swap keeps
        # native cache-token mapping intact.
        @asynccontextmanager
        async def request_stream(self, *args, **kwargs):
            async with super().request_stream(*args, **kwargs) as stream:
                if isinstance(stream, OpenRouterStreamedResponse):
                    try:
                        stream.__class__ = _CostStreamedResponse
                    except TypeError:  # layout mismatch on some pydantic-ai build
                        pass
                yield stream

    provider = OpenRouterProvider(api_key=api_key)
    settings = OpenRouterModelSettings(
        openrouter_usage={"include": True},
        openrouter_cache_instructions="5m",
        openrouter_cache_tool_definitions="5m",
        openrouter_cache_messages="5m",
    )
    return _CostOpenRouterModel(model_id, provider=provider, settings=settings)
```

Leave the module docstring, `read_cost_micro_usd`, and `_with_cost` (lines 1-45) unchanged. The `from contextlib import asynccontextmanager` import at the top stays.

- [ ] **Step 4: Run the full cost test module to verify it passes**

Run: `uv run pytest tests/test_openrouter_cost.py`
Expected: all tests pass (the 4 `read_cost_*` tests are untouched; the 2 rewritten tests now pass).

- [ ] **Step 5: Confirm `usage.py` consumers still work (no regression)**

Run: `uv run pytest tests/test_usage.py`
Expected: PASS — `usage.py` is unchanged and still reads `details[COST_DETAIL_KEY]`.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/config/openrouter_cost.py tests/test_openrouter_cost.py
git commit -m "feat(openrouter): enable prompt caching via official OpenRouterModel"
```

---

### Task 2: Relocate the task checklist out of the system prompt into the turn-context

**Files:**
- Modify: `src/marim_harness/instructions.py:149-159` (delete the `_task_state` closure) and its `render_tasks` import (line 14, now unused there)
- Modify: `src/marim_harness/agent.py` (add `render_tasks` import; inject checklist in `_assemble_prompt`)
- Test: `tests/test_agent_instructions.py`

**Interfaces:**
- Consumes: `self.deps.tasks.items` (a `list[Task]`) and `render_tasks(items) -> str` from `marim_harness.tasks`.
- Produces: when `deps.tasks.items` is non-empty, the rendered checklist block appears in the turn's user prompt (inside the `<turn-context>` envelope), and no longer appears in the request `instructions`.

- [ ] **Step 1: Replace the task-state test in `tests/test_agent_instructions.py`**

Replace `test_task_state_injected_and_dynamic` (lines 160-185) with a version that asserts the checklist rides in the user prompt, not the instructions. Add this helper near the top of the file (after the imports) if not present:

```python
def _last_user_prompt(messages) -> str:
    """The text of the most recent user-prompt part across the request list."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    text = ""
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    text = part.content
    return text
```

```python
@pytest.mark.anyio
async def test_task_checklist_rides_in_turn_context_not_instructions(tmp_path: Path):
    captured: dict = {}

    def fn(messages, info):
        captured["instructions"] = _last_instructions(messages)
        captured["prompt"] = _last_user_prompt(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        model=FunctionModel(fn), provider=BuiltinToolProvider(), deps=deps,
        instructions="BASE PROMPT",
    )

    # No tasks yet -> no checklist anywhere.
    await harness.run_turn("hi")
    assert "checklist" not in captured["instructions"].lower()
    assert "checklist" not in captured["prompt"].lower()

    # Setting tasks makes the next turn surface them in the user prompt
    # (turn-context), and keeps them OUT of the cached system instructions.
    deps.tasks.replace([
        {"text": "read the code", "status": "done"},
        {"text": "write the test", "status": "in_progress"},
    ])
    await harness.run_turn("hi again")
    assert "write the test" in captured["prompt"]
    assert "read the code" in captured["prompt"]
    assert "write the test" not in captured["instructions"]
    # Base system prompt is unaffected.
    assert "BASE PROMPT" in captured["instructions"]
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `uv run pytest tests/test_agent_instructions.py -k task_checklist_rides_in_turn_context`
Expected: FAIL — today the checklist is in `instructions`, so `"write the test" not in captured["instructions"]` fails and the prompt assertion fails.

- [ ] **Step 3: Delete the `_task_state` closure from `instructions.py`**

Remove these lines (`instructions.py:149-159`):

```python
    @agent.instructions
    def _task_state(ctx: RunContext[Deps]) -> str:
        items = ctx.deps.tasks.items
        if not items:
            return ""
        return (
            "Your current task checklist (✔ done · ▸ in progress · ○ "
            "pending):\n\n" + render_tasks(items) + "\n\nKeep it current with "
            "the update_tasks tool: pass the full list, keep one item in "
            "progress, and mark items done as you complete them."
        )
```

Then remove the now-unused import `from .tasks import render_tasks` (`instructions.py:14`).

- [ ] **Step 4: Add the `render_tasks` import to `agent.py`**

Insert after line 30 (`from .subagents import SubagentRunner`), keeping the local import block roughly alphabetical:

```python
from .tasks import render_tasks
```

- [ ] **Step 5: Inject the checklist in `_assemble_prompt`**

In `agent.py`, inside `_assemble_prompt`, immediately after the finished-jobs digest block (the lines that prepend `digest`), add a checklist block. The function currently reads:

```python
        prompt = typed
        digest = self.deps.jobs.take_finished_digest()
        if digest:
            prompt = f"{digest}\n\n{prompt}"
```

Change it to:

```python
        prompt = typed
        # Current task checklist as turn-state (not consumed): it lives here in
        # the per-turn envelope rather than the system prompt so the cached
        # system/tool prefix stays stable across turns.
        items = self.deps.tasks.items
        if items:
            checklist = (
                "Your current task checklist (✔ done · ▸ in progress · ○ "
                "pending):\n\n" + render_tasks(items) + "\n\nKeep it current "
                "with the update_tasks tool: pass the full list, keep one item "
                "in progress, and mark items done as you complete them."
            )
            prompt = f"{checklist}\n\n{prompt}"
        digest = self.deps.jobs.take_finished_digest()
        if digest:
            prompt = f"{digest}\n\n{prompt}"
```

The existing tail of `_assemble_prompt` (which wraps the injected prefix in the turn-context envelope when `prompt != typed`) needs no change — the checklist block makes it wrap automatically.

- [ ] **Step 6: Run the new test to verify it passes**

Run: `uv run pytest tests/test_agent_instructions.py -k task_checklist_rides_in_turn_context`
Expected: PASS.

- [ ] **Step 7: Run the full instruction + tasks suites for regressions**

Run: `uv run pytest tests/test_agent_instructions.py tests/test_tasks.py`
Expected: PASS. (If any other test asserted the checklist appears in `instructions`, update it to check the user prompt via `_last_user_prompt`.)

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/instructions.py src/marim_harness/agent.py tests/test_agent_instructions.py
git commit -m "feat(cache): move task checklist into turn-context to keep prefix stable"
```

---

### Task 3: End-to-end verification of cache behavior

**Files:**
- Create: none (manual/scripted verification; no committed live test to avoid a network dependency in CI)

**Interfaces:**
- Consumes: `usage_summary(usage, model_ref)` from `marim_harness.usage` (already returns `cache_read_tokens`, `cache_write_tokens`, `cost_usd`).

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest`
Expected: PASS (no regressions from Tasks 1–2).

- [ ] **Step 2: Live two-turn smoke check (requires `OPENROUTER_API_KEY`)**

Run a real two-turn session against `anthropic/claude-sonnet-4-6` (the default) — e.g. ask one question, then a follow-up in the same session — and inspect the per-turn usage breakdown that the status bar / headless output already surfaces (`usage_summary`).

Expected:
- Turn 1: `cache_write_tokens` > 0 (the prefix is written to cache), `cache_read_tokens` ≈ 0.
- Turn 2: `cache_read_tokens` > 0 and dominates input — the system + tools + turn-1 prefix is read from cache.
- `cost_usd` is still populated and `cost_is_exact` is `True` (billed cost re-inject intact).

- [ ] **Step 3: Confirm the prefix stays cached while the checklist changes**

In the same session, trigger a turn where the agent updates its task list (or pre-seed `deps.tasks` and run another turn). Expected: `cache_read_tokens` stays high on the following turn — proving the checklist move (Task 2) keeps the cached prefix stable even as the checklist churns.

- [ ] **Step 4: Record the result**

Note the before/after per-turn `cost_usd` and `cache_read_tokens` in the PR description as evidence the change works. No commit needed.

---

## Self-Review

**Spec coverage:**
- Part A (adopt `OpenRouterModel`, three cache settings, `openrouter_usage`) → Task 1. ✔
- Cost capture via thin re-inject on model + streamed response → Task 1, Step 3. ✔
- Streamed-response class question resolved: subclass `OpenRouterStreamedResponse` (verified its `_map_usage` maps cache tokens) → Task 1, Step 3. ✔
- Part B (relocate `_task_state` to turn-context) → Task 2. ✔
- Scope: OpenRouter only; `google`/`local` untouched → Global Constraints; no task edits `model.py`. ✔
- Fail-soft caching → Global Constraints (pydantic-ai ignores settings when the profile lacks support). ✔
- Testing (cost re-inject unchanged; settings present; checklist in turn-context; two-turn cache read) → Tasks 1–3. ✔
- Verification (before/after cache_read + cost) → Task 3. ✔

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✔

**Type consistency:** `build_openrouter_model(model_id, api_key)` signature unchanged; `_with_cost`/`read_cost_micro_usd` reused verbatim; `render_tasks(items)` and `deps.tasks.items` match `tasks.py` and `deps.py`; `OpenRouterModel`/`OpenRouterModelSettings`/`OpenRouterStreamedResponse` import paths verified against pydantic-ai 1.107. ✔
