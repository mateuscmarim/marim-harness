# Message Steering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user inject a message (with optional attachments) into a running turn via `Alt+Enter`, delivered to the model at the next request boundary — losslessly, no cancel/restart.

**Architecture:** No run-loop rewrite. The harness captures the live `RunContext` that its event-stream handler already receives, and `Harness.steer(text, attachments)` calls `run_ctx.enqueue(text, *BinaryContent, priority='asap')` — pydantic-ai's supported mid-run injection, which `agent.run()` already drains. A small buffer covers `ask`-mode between-round gaps; a stranded steer (finishing-gap race) falls back to the front of the message queue. The TUI adds an `Alt+Enter` action.

**Tech Stack:** Python 3, pydantic-ai 1.107.0 (`RunContext.enqueue`, `BinaryContent`, `FunctionModel`), Textual (TUI, `Pilot`), pytest (`pytest-anyio`), `uv`.

## Global Constraints

- Run tests with `uv run pytest`.
- **No rewrite of `_run_with_approval`** — it keeps calling `agent.run(...)`. Steering is additive.
- `RunContext.enqueue` is on-loop-safe; the `Alt+Enter` action and turn worker share the app's event loop, so no `call_soon_threadsafe`.
- Attachments are `list[tuple[bytes, str]]` (data, media_type), same shape as `run_turn`/the queue; injected as `BinaryContent(data=d, media_type=m)`.
- Only `priority='asap'` is used.
- Headless behavior must not change: when there is no event-stream handler **and** no hooks, `run_turn` must still pass `event_stream_handler=None` to `agent.run()` (don't force streaming just to capture a ctx nobody steers).
- The steer buffer is drained by the TUI **after** `run_turn` returns (stranded-steer fallback), so `run_turn` must NOT clear the buffer — it only clears `_active_run_ctx`.

## File Structure

- `src/marim_harness/agent.py` — **modify.** Harness: `_active_run_ctx`/`_steer_buffer` state, restructured handler wrapper that captures the ctx, `steer()`, `take_buffered_steers()`, `_active_run_ctx` clear in `run_turn`.
- `src/marim_harness/interfaces/tui/widgets/prompt.py` — **modify.** Add a `Steer` message + an `alt+enter` key branch.
- `src/marim_harness/interfaces/tui/app.py` — **modify.** Handle `PromptInput.Steer`; prepend stranded steers to the queue in `_after_turn`.
- `tests/test_steering.py` — **new.** Harness unit + end-to-end injection tests.
- TUI Pilot tests go in `tests/test_steering.py` too (reuse a local `_app` fixture).

---

### Task 1: Harness — RunContext capture, `steer()`, buffer, lifecycle

**Files:**
- Modify: `src/marim_harness/agent.py` (`Harness.__init__`, `_build_hooked_handler`, `run_turn`; add `steer`, `take_buffered_steers`)
- Create: `tests/test_steering.py`

**Interfaces:**
- Produces on `Harness`: `self._active_run_ctx` (a `RunContext | None`), `self._steer_buffer: list[tuple[str, Optional[list[tuple[bytes, str]]]]]`; `steer(text: str, attachments: Optional[list[tuple[bytes, str]]] = None) -> None`; `take_buffered_steers() -> list[tuple[str, Optional[list[tuple[bytes, str]]]]]`.
- Consumes: `BinaryContent` (already imported in agent.py), the existing `_build_hooked_handler`/`run_turn`.

- [ ] **Step 1: Write the failing tests for `steer` + buffer**

Create `tests/test_steering.py`:

```python
from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.permissions import Mode


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _harness(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    return Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(),
        Deps(workspace_root=tmp_path, mode=Mode.auto), instructions="test",
    )


class _FakeCtx:
    def __init__(self):
        self.calls = []

    def enqueue(self, *content, priority="asap"):
        self.calls.append((content, priority))


def test_steer_enqueues_on_active_ctx(tmp_path):
    h = _harness(tmp_path)
    ctx = _FakeCtx()
    h._active_run_ctx = ctx
    h.steer("go left")
    assert ctx.calls == [(("go left",), "asap")]
    assert h._steer_buffer == []  # flushed


def test_steer_with_attachments_enqueues_binary_content(tmp_path):
    from pydantic_ai import BinaryContent

    h = _harness(tmp_path)
    ctx = _FakeCtx()
    h._active_run_ctx = ctx
    h.steer("look", attachments=[(b"\x89PNG", "image/png")])
    (content,), priority = ctx.calls[0]
    assert content[0] == "look"
    assert isinstance(content[1], BinaryContent)
    assert content[1].media_type == "image/png"
    assert priority == "asap"


def test_steer_buffers_when_no_active_ctx(tmp_path):
    h = _harness(tmp_path)
    assert h._active_run_ctx is None
    h.steer("later")
    assert h._steer_buffer == [("later", None)]


def test_take_buffered_steers_returns_and_clears(tmp_path):
    h = _harness(tmp_path)
    h.steer("a")
    h.steer("b")
    assert h.take_buffered_steers() == [("a", None), ("b", None)]
    assert h._steer_buffer == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_steering.py -v`
Expected: FAIL — `AttributeError: 'Harness' object has no attribute '_active_run_ctx'` / no `steer`.

- [ ] **Step 3: Add state, `steer`, `take_buffered_steers`**

In `src/marim_harness/agent.py`, in `Harness.__init__` (near the other turn state), add:

```python
        # Live RunContext of the in-flight turn, captured by the event-stream
        # handler wrapper; None between turns. A steer enqueues onto it.
        self._active_run_ctx = None
        # Steers typed when no run is live yet (ask-mode between-round gap):
        # (text, attachments) buffered, flushed when a ctx is next captured.
        self._steer_buffer: list[tuple[str, Optional[list[tuple[bytes, str]]]]] = []
```

Add these methods to `Harness` (place near `run_turn`):

```python
    def steer(self, text: str,
              attachments: Optional[list[tuple[bytes, str]]] = None) -> None:
        """Inject a user message into the running turn. Reaches the model at the
        next request boundary (pydantic-ai drains 'asap' content before it).
        Buffers if no run is live yet; the buffer flushes when a ctx is captured."""
        self._steer_buffer.append((text, attachments))
        self._flush_steers()

    def _flush_steers(self) -> None:
        if self._active_run_ctx is None or not self._steer_buffer:
            return
        for text, atts in self._steer_buffer:
            self._active_run_ctx.enqueue(
                text,
                *(BinaryContent(data=d, media_type=m) for d, m in (atts or [])),
                priority="asap",
            )
        self._steer_buffer = []

    def take_buffered_steers(
        self,
    ) -> list[tuple[str, Optional[list[tuple[bytes, str]]]]]:
        """Return and clear any steers that were never flushed (the
        finishing-gap race). The caller decides what to do with them."""
        buffered, self._steer_buffer = self._steer_buffer, []
        return buffered
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `uv run pytest tests/test_steering.py -v`
Expected: PASS (the four tests above).

- [ ] **Step 5: Write the failing end-to-end injection test**

Add to `tests/test_steering.py` (drives a real turn through the harness with a streaming `FunctionModel` that records messages, steers mid-run from a concurrent task):

```python
import asyncio


def _recording_streaming_harness(tmp_path, calls):
    from collections.abc import AsyncIterator

    from pydantic_ai.models.function import (
        AgentInfo, DeltaToolCall, FunctionModel,
    )
    from pydantic_ai.messages import ModelMessage

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator:
        seen = []
        for m in messages:
            for p in getattr(m, "parts", []):
                seen.append(getattr(p, "content", getattr(p, "tool_name", None)))
        calls.append(seen)
        if len(calls) == 1:
            yield {0: DeltaToolCall(name="slow", json_args="{}", tool_call_id="c1")}
        else:
            yield "done"

    h = Harness(
        FunctionModel(stream_function=stream_fn), BuiltinToolProvider(),
        Deps(workspace_root=tmp_path, mode=Mode.auto), instructions="test",
    )

    @h.agent.tool_plain
    async def slow() -> str:
        await asyncio.sleep(0.3)  # window for a concurrent steer
        return "pong"

    return h


@pytest.mark.anyio
async def test_steer_reaches_a_later_model_request(tmp_path):
    calls: list[list] = []
    h = _recording_streaming_harness(tmp_path, calls)

    async def steerer():
        for _ in range(200):
            if h._active_run_ctx is not None:
                break
            await asyncio.sleep(0.01)
        h.steer("STEER NOW")

    # a no-op event handler so streaming is on and the ctx is captured
    async def handler(ctx, events):
        async for _ in events:
            pass

    out, _ = await asyncio.gather(
        h.run_turn("hello", event_stream_handler=handler),
        steerer(),
    )
    assert out == "done"
    flat = [str(c) for c in calls]
    assert any("STEER NOW" in c for c in flat), f"steer not injected: {calls}"
```

- [ ] **Step 6: Run to verify failure**

Run: `uv run pytest tests/test_steering.py::test_steer_reaches_a_later_model_request -v`
Expected: FAIL — `_active_run_ctx` is never set (the handler wrapper doesn't capture it yet), so the steer buffers and is never delivered; the assertion fails.

- [ ] **Step 7: Capture the RunContext in the handler wrapper; clear it in `run_turn`**

In `src/marim_harness/agent.py`, replace `_build_hooked_handler` so it ALWAYS wraps when streaming is active (a handler exists or hooks are configured), capturing the ctx; it returns `None` only when there's nothing to stream (preserving headless):

```python
    def _build_hooked_handler(self, base_handler):
        """Wrap the event-stream handler to (1) capture the live RunContext for
        steering and (2) fire Pre/PostToolUse hooks on tool events. Returns
        ``None`` when there's neither a base handler nor hooks, so headless runs
        don't stream just to capture a ctx nobody steers."""
        if base_handler is None and self.deps.hooks is None:
            return None
        _call_inputs: dict = {}

        async def _wrapped(stream_ctx, events):
            # Capture the live RunContext so steer() can enqueue onto it. Set on
            # every streamed node, so it stays current within the run.
            self._active_run_ctx = stream_ctx
            self._flush_steers()  # deliver any steers buffered before this ctx

            async def _relay():
                async for event in events:
                    if self.deps.hooks is not None:
                        await self.hooks.tool_event(event, _call_inputs)
                    yield event

            if base_handler is not None:
                await base_handler(stream_ctx, _relay())
            else:
                async for _ in _relay():
                    pass

        return _wrapped
```

Then wrap the `run_turn` body so the captured ctx is cleared when the turn ends. Replace the final `return await self._run_with_approval(...)` with:

```python
        try:
            return await self._run_with_approval(
                user_prompt, deferred_results=None, toolsets=toolsets,
                event_stream_handler=event_stream_handler, resumable=resumable,
            )
        finally:
            self._active_run_ctx = None
```

(Do NOT clear `_steer_buffer` here — the TUI drains it after `run_turn` returns for the stranded-steer fallback.)

- [ ] **Step 8: Run the end-to-end test + the full suite**

Run: `uv run pytest tests/test_steering.py -v`
Expected: PASS (all five tests).
Run: `uv run pytest --no-header -q -o addopts=""`
Expected: PASS, no regressions. The handler wrapper now always wraps when a handler/hook is present — confirm `test_agent_hooks.py` and the streaming tests in `test_app.py` still pass (the relay + hook-firing behavior is preserved; only ctx-capture is added).

- [ ] **Step 9: Commit**

```bash
git add src/marim_harness/agent.py tests/test_steering.py
git commit -m "feat(agent): Harness.steer — inject a message mid-turn via RunContext.enqueue"
```

---

### Task 2: TUI — Alt+Enter trigger, steer marker, stranded-steer fallback

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/prompt.py` (add `Steer` message + `alt+enter` branch)
- Modify: `src/marim_harness/interfaces/tui/app.py` (handle `PromptInput.Steer`; prepend stranded steers in `_after_turn`)
- Modify: `tests/test_steering.py` (Pilot tests)

**Interfaces:**
- Consumes from Task 1: `Harness.steer(text, attachments)`, `Harness.take_buffered_steers()`.
- Consumes existing app internals: `_turn_worker`, `_start_turn`, `_enqueue`/`_queue`/`_queue_seq`/`_render_queue`, `_after_turn`, `_image_block_reason`, `QueuedMessage`, `NoticeMessage`.
- Produces: `PromptInput.Steer` message; `HarnessApp.on_prompt_input_steer` handler.

- [ ] **Step 1: Write the failing test for the `alt+enter` key + `Steer` message**

Add to `tests/test_steering.py`:

```python
from marim_harness.interfaces.tui.widgets.prompt import PromptInput


@pytest.mark.anyio
async def test_alt_enter_posts_steer_message(tmp_path):
    from textual.app import App, ComposeResult

    posted = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield PromptInput()

        def on_prompt_input_steer(self, event: PromptInput.Steer) -> None:
            posted.append(event.value)

    app = _App()
    async with app.run_test() as pilot:
        await pilot.pause()
        pi = app.query_one(PromptInput)
        pi.focus()
        pi.text = "steer this"
        await pilot.press("alt+enter")
        await pilot.pause()
    assert posted == ["steer this"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_steering.py::test_alt_enter_posts_steer_message -v`
Expected: FAIL — no `PromptInput.Steer` / `alt+enter` not handled.

**IMPORTANT verification:** if this test fails because `pilot.press("alt+enter")` never reaches `_on_key` as key `"alt+enter"` (terminal/Textual may report a different key name or not distinguish it), STOP and report the actual `event.key` you observe (add a temporary debug print in `_on_key`). Alt+Enter delivery is terminal-dependent; if Textual doesn't deliver a distinct `"alt+enter"`, the trigger key must be reconsidered — surface this rather than guessing.

- [ ] **Step 3: Add the `Steer` message + `alt+enter` branch to `PromptInput`**

In `src/marim_harness/interfaces/tui/widgets/prompt.py`, add a `Steer` message class next to `Submitted`:

```python
    class Steer(Message):
        """Posted when the user presses Alt+Enter; carries the box's full text
        and any attached images, to inject into the running turn."""

        def __init__(self, value: str,
                     attachments: list[tuple[bytes, str]] | None = None) -> None:
            self.value = value
            self.attachments = attachments or []
            super().__init__()
```

In `_on_key`, add a branch BEFORE the `enter` branch (so `alt+enter` is matched first):

```python
        if event.key == "alt+enter":
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Steer(self.text, atts))
            self.attachments = []
            self._reset_nav()
            return
```

- [ ] **Step 4: Run the key test to verify it passes**

Run: `uv run pytest tests/test_steering.py::test_alt_enter_posts_steer_message -v`
Expected: PASS. (If it fails on key delivery, follow the STOP guidance in Step 2.)

- [ ] **Step 5: Write the failing tests for the app's steer handling + stranded fallback**

Add to `tests/test_steering.py` (uses a TUI app fixture — reuse the `_app` pattern from `tests/test_queue.py`):

```python
from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.queue import QueuedMessage


def _tui_app(tmp_path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps,
                      instructions="test")
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_steer_while_busy_calls_harness_steer(tmp_path):
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        seen = []
        app.harness.steer = lambda text, attachments=None: seen.append((text, attachments))
        app._turn_worker = object()  # simulate a running turn
        await app.on_prompt_input_steer(PromptInput.Steer("redirect", []))
        assert seen == [("redirect", [])]
        assert app._queue == []  # not queued
        assert app._turn_worker is not None  # no new worker


@pytest.mark.anyio
async def test_steer_while_idle_runs_normally(tmp_path):
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = []
        app._start_turn = lambda text, attachments=None: started.append(text) or _noop()
        app._turn_worker = None
        await app.on_prompt_input_steer(PromptInput.Steer("just run", []))
        assert started == ["just run"]


@pytest.mark.anyio
async def test_empty_steer_is_noop(tmp_path):
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        seen = []
        app.harness.steer = lambda *a, **k: seen.append(a)
        app._turn_worker = object()
        await app.on_prompt_input_steer(PromptInput.Steer("   ", []))
        assert seen == []  # empty text, no attachments -> no-op


@pytest.mark.anyio
async def test_stranded_steer_goes_to_front_of_queue(tmp_path):
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue = [QueuedMessage("existing", None, "1")]
        app._queue_paused = False
        # simulate a steer left buffered on the harness when the turn ended
        app.harness._steer_buffer = [("stranded", None)]
        app._start_turn = lambda text, attachments=None: _noop()  # don't really run
        await app._after_turn()
        assert app._queue[0].text == "stranded"  # prepended to the front


async def _noop():
    return None
```

- [ ] **Step 6: Run to verify failure**

Run: `uv run pytest tests/test_steering.py -k "steer_while or empty_steer or stranded" -v`
Expected: FAIL — no `on_prompt_input_steer`; `_after_turn` doesn't drain the steer buffer.

- [ ] **Step 7: Add the app handler + stranded-steer fallback**

In `src/marim_harness/interfaces/tui/app.py`, add the steer handler (next to `on_prompt_input_submitted`):

```python
    async def on_prompt_input_steer(self, event: PromptInput.Steer) -> None:
        text = event.value.strip()
        if not text and not event.attachments:
            return  # nothing to steer
        if self._turn_worker is None:
            # No turn running — just run it normally.
            await self._start_turn(text, event.attachments)
            return
        reason = self._image_block_reason(event.attachments)
        if reason is not None:
            log = self.query_one("#log", VerticalScroll)
            await log.mount(NoticeMessage(reason))
            return
        self.harness.steer(text, event.attachments)
        tag = f"  📎 {len(event.attachments)}" if event.attachments else ""
        log = self.query_one("#log", VerticalScroll)
        await log.mount(NoticeMessage(f"↪ steering: {text}{tag}"))
```

Then update `_after_turn` to prepend any stranded steers to the front of the queue before draining (drain the harness buffer always so it never leaks; prepend only on a clean, unpaused finish):

```python
    async def _after_turn(self) -> None:
        leftover = self.harness.take_buffered_steers()
        if not self._queue_paused and leftover:
            for text, atts in reversed(leftover):
                self._queue_seq += 1
                self._queue.insert(0, QueuedMessage(text, atts, str(self._queue_seq)))
            self._render_queue()
        if not self._queue_paused and self._queue:
            await self._drain_next()
        else:
            self._maybe_wake()
```

- [ ] **Step 8: Run the app tests**

Run: `uv run pytest tests/test_steering.py -k "steer_while or empty_steer or stranded" -v`
Expected: PASS.

- [ ] **Step 9: Run the full suite**

Run: `uv run pytest --no-header -q -o addopts=""`
Expected: PASS, no regressions.

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/prompt.py src/marim_harness/interfaces/tui/app.py tests/test_steering.py
git commit -m "feat(tui): Alt+Enter steers the running turn; stranded steer falls to queue front"
```

---

## Self-Review

**Spec coverage:**
- "Capture live RunContext in the handler wrapper" → Task 1 Step 7 (`_build_hooked_handler` rewrite, always-wrap-when-streaming, headless-preserving). ✔
- "`steer(text, attachments)` → `run_ctx.enqueue(text, *BinaryContent, 'asap')`" → Task 1 Step 3 (`steer`/`_flush_steers`). ✔
- "Buffer for ask-mode between-round gap; flush when ctx captured" → `_steer_buffer` + `_flush_steers` called on capture (Step 7). ✔
- "`_active_run_ctx` cleared on turn end" → Task 1 Step 7 `run_turn` finally. ✔
- "Alt+Enter trigger; idle→run; empty+no-atts→no-op; image-block check" → Task 2 Steps 3, 7. ✔
- "Visible steer marker (↪ + 📎N)" → Task 2 Step 7. ✔
- "Stranded steer → front of message queue" → Task 2 Step 7 (`_after_turn`). ✔
- "Attachments via BinaryContent" → Task 1 `_flush_steers`; Task 2 passes `event.attachments`. ✔
- "No run-loop rewrite; headless unchanged" → Global Constraints; `_build_hooked_handler` returns `None` when no handler+no hooks. ✔
- "Regression: existing suite green" → Task 1 Step 8, Task 2 Step 9. ✔

**Placeholder scan:** No TBD/"handle edge cases"/"similar to". The one explicit STOP-and-report is the Alt+Enter key-delivery verification (a genuine terminal-dependent risk, with concrete instructions), not a placeholder.

**Type consistency:** `steer(text, attachments=None)`, `take_buffered_steers()`, `_active_run_ctx`, `_steer_buffer`, `_flush_steers` are named identically across definition, tests, and call sites. `PromptInput.Steer(value, attachments)` mirrors `Submitted`; `on_prompt_input_steer` matches Textual's `on_<widget>_<message>` convention. `QueuedMessage(text, attachments, id)` construction in the stranded-steer path matches the queue's existing 3-field shape and `_queue_seq` id scheme.
