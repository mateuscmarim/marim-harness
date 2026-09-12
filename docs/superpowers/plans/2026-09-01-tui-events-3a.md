# Phase 3a — TUI Renders From Events: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the TUI an event-driven client of an in-process `SessionHost` — sole `bind_ui` consumer, wire-dict renderer, asks via `ask.pending`/`ask.resolved` — with single-client behavior parity.

**Architecture:** `HarnessApp` wraps the harness in `EventBus` + `SessionHost(claim=None)` (claim stays on the Harness). A pump worker owns a bus `Subscription`, parses events into typed wire models (`server/wire_events.py`), and dispatches to the renderers. A provisional `SessionHost.run_turn` keeps the TUI's await-style turn worker until 3b inverts it. The old local `bind_ui` wiring in `app.py` is deleted — no compat flag.

**Tech Stack:** Python 3.10+, Pydantic AI, Textual, pydantic v2 (wire models), pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-tui-events-3a-design.md` (commit `9cc1c304`).

## Global Constraints

- `requires-python >=3.10` — no 3.11+-only syntax.
- Ruff line length 100; rules `E,F,I,UP,B,SIM,C901`; complexity cap 10; **no `# noqa: C901`**.
- CI enforces `ruff format --check` — run `uv run ruff format src tests` before every commit.
- Use `uv` for everything: `uv run ruff check src tests`, `uv run ruff format --check src tests`, `uv run pyright`, `uv run pytest`. Never bare python/pip/pytest.
- **No compat flag** for the old callback path — it is deleted, not gated (spec: "Context").
- **Claim invariant:** the in-process host is built with `claim=None`. The Harness keeps claim ownership (`default_cmd` adopts it, `Harness.aclose()` releases it). Never pass a claim to the in-process host — same-process flock on a second fd is denied even by the holder.
- **Teardown invariant:** the TUI keeps its own exit path (`cancel_autoname` → persist → `session_end` → `harness.aclose()` in `app.py` + `harness.release_claim()` in `default_cmd`'s finally). Do **not** route TUI exit through `SessionHost.aclose()` — it waits for autoname (daemon idle-eviction semantics).
- Wire vocabulary (this plan pins exact names; the parent spec §2 defines families): existing — `turn.started`, `turn.finished`, `turn.error`, `text.delta`, `thinking.delta`, `tool.call`, `tool.result`, `ask.pending`, `ask.resolved`, `session.status`, `session.renamed`, `tasks.changed`, `jobs.changed`, `compaction.started`, `compaction.finished`, `subagent.event`, `stream.gap`. New in 3a — `workflow.spawned`, `workflow.started`, `workflow.logged`, `workflow.finished`, `workflow.spawn_finished`, `subagent.notice`, `subagent.model`, `subagent.thinking`, `subagent.usage`, `subagent.cli_activity`, `session.ttft`, `session.mode_changed`, `session.notice`.
- Textual traps (parent spec §6): `pilot.pause()` is **not** a wait — tests must wait for a real condition (an event, a worker, a widget state); the CI 3.12 leg is 3.12.3 (no `asyncio` quirks to paper over).
- If a quoted "current code" block below doesn't match what's on disk, STOP and report BLOCKED rather than improvising (line numbers drift; match on code shape).

---

## File Map

- **Create** `src/marim_harness/server/wire_events.py` — pydantic v2 wire models + `parse_wire_event` (renderer contract; Task 1).
- **Create** `tests/test_wire_events.py` — parse round-trips (Task 1).
- **Modify** `src/marim_harness/server/host.py` — full `bind_ui` wiring, new publishes, plan ask kind, provisional `run_turn` (Task 2).
- **Modify** `src/marim_harness/server/schema.py` — `AskAnswerIn` plan arm (Task 2).
- **Modify** `tests/test_server_host.py` — publish-coverage headline test, run_turn tests, plan-ask test (Task 2).
- **Modify** `src/marim_harness/interfaces/tui/stream_render.py` — wire-dict dispatch replaces pydantic-ai event dispatch; drop `pydantic_ai.messages` imports (Task 3).
- **Modify** `tests/test_app.py` (+ ask-flow tests) — renderer fed by scripted wire events (Task 3).
- **Modify** `src/marim_harness/interfaces/tui/app.py` — EventBus+host construction, pump worker, drop `bind_ui`, provisional turn call (Task 4).
- **Modify** `src/marim_harness/interfaces/tui/interactions/base.py` — non-blocking panel mount for event-driven asks (Task 5).
- **Modify** `tests/test_approval.py`, `tests/test_ask_user_render.py`, `tests/test_app_present_plan.py` — adapt to event-driven asks (Task 5).

Dependencies: Task 1 → Task 2 (host payload shapes), Task 1 → Task 3 (models), Tasks 2+3 → Task 4, Task 4 → Task 5. Do not merge Task 3 alone — Task 4 is what actually feeds it.

---

### Task 1: Typed wire models (`server/wire_events.py`)

**Files:**
- Create: `src/marim_harness/server/wire_events.py`
- Test: `tests/test_wire_events.py`

**Interfaces:**
- Consumes: nothing (defines the contract).
- Produces: `WireEvent` union + `parse_wire_event(d: dict) -> WireEvent | None` — Task 3's renderer and Task 4's pump import these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_wire_events.py`:

```python
from marim_harness.server.wire_events import (
    AskPending,
    TextDelta,
    ToolCall,
    TurnFinished,
    parse_wire_event,
)


def test_parse_every_known_type():
    cases = [
        ({"type": "turn.started", "turn_id": "t1", "prompt": "hi", "trigger": "user"}, "turn.started"),
        ({"type": "turn.finished", "turn_id": "t1", "output": "ok", "usage": {}}, "turn.finished"),
        ({"type": "turn.finished", "turn_id": "t1", "interrupted": True}, "turn.finished"),
        ({"type": "turn.error", "turn_id": "t1", "error": "boom"}, "turn.error"),
        ({"type": "text.delta", "text": "hel"}, "text.delta"),
        ({"type": "thinking.delta", "text": "hmm"}, "thinking.delta"),
        ({"type": "tool.call", "id": "c1", "name": "bash", "args": {"command": "ls"}}, "tool.call"),
        ({"type": "tool.result", "id": "c1", "content": "ok"}, "tool.result"),
        ({"type": "ask.pending", "id": "a1", "kind": "approval", "payload": {}, "created": "now"}, "ask.pending"),
        ({"type": "ask.resolved", "id": "a1", "answer": {"approve": True}}, "ask.resolved"),
        ({"type": "session.status", "status": "running"}, "session.status"),
        ({"type": "subagent.event", "stream_id": "s1", "event": {"type": "text.delta", "text": "x"}}, "subagent.event"),
        ({"type": "stream.gap", "resync": "history"}, "stream.gap"),
    ]
    for data, expected in cases:
        model = parse_wire_event(data)
        assert model is not None, data
        assert model.type == expected


def test_parse_unknown_type_returns_none():
    assert parse_wire_event({"type": "never.heard.of"}) is None


def test_parse_missing_type_returns_none():
    assert parse_wire_event({"nope": 1}) is None


def test_models_carry_fields():
    m = parse_wire_event({"type": "tool.call", "id": "c1", "name": "bash", "args": {}})
    assert isinstance(m, ToolCall)
    assert m.name == "bash"
    ask = parse_wire_event(
        {"type": "ask.pending", "id": "a1", "kind": "plan", "payload": {"summary": "s"}, "created": "now"}
    )
    assert isinstance(ask, AskPending)
    assert ask.kind == "plan"
    done = parse_wire_event({"type": "turn.finished", "turn_id": "t", "interrupted": True})
    assert isinstance(done, TurnFinished)
    assert done.interrupted is True
    delta = parse_wire_event({"type": "text.delta", "text": "x"})
    assert isinstance(delta, TextDelta)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_wire_events.py -x --no-cov`
Expected: FAIL — `ModuleNotFoundError: marim_harness.server.wire_events`.

- [ ] **Step 3: Implement the module**

Create `src/marim_harness/server/wire_events.py`. Every model: a `Literal` `type` field matching its wire name, fields optional where the emitter omits them (e.g. `turn.finished` carries `output`/`usage` OR `interrupted`). Use pydantic v2 (`BaseModel`, `Field`, `TypeAdapter`). Structure:

```python
"""Typed view of the session-event wire vocabulary (parent spec §2).

The bus and the HTTP layer stay dict-typed (transport); this module is the
RENDERER contract — the TUI pump parses each wire dict once and every
front-end handler consumes only these models. Unknown types parse to None
(forward-compatible: a newer server can add events an older client skips).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


class TurnStarted(BaseModel):
    type: Literal["turn.started"]
    turn_id: str
    prompt: str = ""
    trigger: str = "user"


class TurnFinished(BaseModel):
    type: Literal["turn.finished"]
    turn_id: str
    output: str | None = None
    usage: dict | None = None
    interrupted: bool = False


class TurnError(BaseModel):
    type: Literal["turn.error"]
    turn_id: str
    error: str


class TextDelta(BaseModel):
    type: Literal["text.delta"]
    text: str


class ThinkingDelta(BaseModel):
    type: Literal["thinking.delta"]
    text: str


class ToolCall(BaseModel):
    type: Literal["tool.call"]
    id: str
    name: str
    args: dict


class ToolResult(BaseModel):
    type: Literal["tool.result"]
    id: str
    content: Any = None


class AskPending(BaseModel):
    type: Literal["ask.pending"]
    id: str
    kind: str  # "approval" | "question" | "plan"
    payload: dict
    created: str


class AskResolved(BaseModel):
    type: Literal["ask.resolved"]
    id: str
    answer: dict


class SessionStatus(BaseModel):
    type: Literal["session.status"]
    status: str


class SessionRenamed(BaseModel):
    type: Literal["session.renamed"]
    from_: str = Field(alias="from")  # "from" is a keyword
    to: str


class TasksChanged(BaseModel):
    type: Literal["tasks.changed"]


class JobsChanged(BaseModel):
    type: Literal["jobs.changed"]


class CompactionStarted(BaseModel):
    type: Literal["compaction.started"]


class CompactionFinished(BaseModel):
    type: Literal["compaction.finished"]
    before: int | None = None
    after: int | None = None


class SubagentEvent(BaseModel):
    type: Literal["subagent.event"]
    stream_id: str
    event: dict  # nested wire stream event (text.delta/tool.call/...)
    usage: dict | None = None


class SubagentNotice(BaseModel):
    type: Literal["subagent.notice"]
    stream_id: str
    message: str


class SubagentModel(BaseModel):
    type: Literal["subagent.model"]
    stream_id: str
    model: str


class SubagentThinking(BaseModel):
    type: Literal["subagent.thinking"]
    stream_id: str
    level: str


class SubagentUsage(BaseModel):
    type: Literal["subagent.usage"]
    stream_id: str
    usage: dict


class SubagentCliActivity(BaseModel):
    type: Literal["subagent.cli_activity"]
    events: list[dict]  # wire stream events (claude-cli tool_use/tool_result)


class WorkflowSpawned(BaseModel):
    type: Literal["workflow.spawned"]
    stream_id: str
    spawn_type: str
    task: str
    parent_tool_call_id: str


class WorkflowStarted(BaseModel):
    type: Literal["workflow.started"]
    tool_call_id: str
    title: str


class WorkflowLogged(BaseModel):
    type: Literal["workflow.logged"]
    tool_call_id: str
    message: str


class WorkflowFinished(BaseModel):
    type: Literal["workflow.finished"]
    tool_call_id: str
    outcome: str
    failed: bool


class WorkflowSpawnFinished(BaseModel):
    type: Literal["workflow.spawn_finished"]
    stream_id: str
    report: str


class SessionTtft(BaseModel):
    type: Literal["session.ttft"]
    seconds: float


class SessionModeChanged(BaseModel):
    type: Literal["session.mode_changed"]
    mode: str


class SessionNotice(BaseModel):
    type: Literal["session.notice"]
    message: str


class StreamGap(BaseModel):
    type: Literal["stream.gap"]
    resync: str = "history"


WireEvent = Annotated[
    Union[
        TurnStarted, TurnFinished, TurnError, TextDelta, ThinkingDelta,
        ToolCall, ToolResult, AskPending, AskResolved, SessionStatus,
        SessionRenamed, TasksChanged, JobsChanged, CompactionStarted,
        CompactionFinished, SubagentEvent, SubagentNotice, SubagentModel,
        SubagentThinking, SubagentUsage, SubagentCliActivity,
        WorkflowSpawned, WorkflowStarted, WorkflowLogged, WorkflowFinished,
        WorkflowSpawnFinished, SessionTtft, SessionModeChanged,
        SessionNotice, StreamGap,
    ],
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter = TypeAdapter(WireEvent)


def parse_wire_event(d: dict) -> WireEvent | None:
    """Parse one wire dict. Unknown/malformed → None (log at the call site)."""
    if not isinstance(d.get("type"), str):
        return None
    try:
        return _ADAPTER.validate_python(d)
    except Exception:
        return None
```

**Watch:** `SessionRenamed` — the wire key is `"from"` (host publishes `{"from": old, "to": new}`); a keyword-named field needs `Field(alias="from")` and the model needs `model_config = ConfigDict(populate_by_name=True)` if you also want `from_=` construction; the pump only ever parses, so the alias alone suffices — but the field NAME must then be `from_` (write `from_: str = Field(alias="from")`). Get this exactly right; pyright must pass.

- [ ] **Step 4: Run tests + gates**

Run: `uv run pytest tests/test_wire_events.py --no-cov` → all pass.
Run: `uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright` → clean.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests
git add src/marim_harness/server/wire_events.py tests/test_wire_events.py
git commit -m "feat(server): typed wire-event models + parse_wire_event"
```

---

### Task 2: SessionHost publishes the full `bind_ui` vocabulary + provisional `run_turn`

**Files:**
- Modify: `src/marim_harness/server/host.py`
- Modify: `src/marim_harness/server/schema.py` (`AskAnswerIn` plan arm)
- Modify: `tests/test_server_host.py`

**Interfaces:**
- Consumes: `bind_ui` named params + `UIHooks` (`runtime/deps.py`), `event_to_dict`/`STREAM_EVENT_TYPES` (`server/stream_events.py`), `PlanDecision`, `usage_summary`.
- Produces: every wire event in the Global Constraints vocabulary; `SessionHost.run_turn(prompt, attachments) -> TurnOutcome`.

- [ ] **Step 1: Verify the seams on disk**

Before writing code, confirm these (STOP/BLOCKED if they don't match):
- `Harness.bind_ui` parameter list (harness.py ~646-680): 12 named params (`request_approval`, `ask_user`, `on_present_plan`, `on_subagent_event`, `on_tasks_changed`, `on_jobs_changed`, `on_rename`, `on_cli_activity`, `on_compact_start`, `on_compact`, `on_ttft`, `on_mode_change`) + `on_notice` (~675) + `**ui: Unpack[UIHooks]` (`on_subagent_notice/model/thinking/usage`, `on_workflow_spawn/start/log/done/spawn_done`, `detach_fanout`, `interactive`, `notifier`). The app's binding list (`app.py` ~166-189, 22 kwargs) is the authoritative inventory of what the TUI needs — `detach_fanout`/`interactive`/`notifier` are not bindable UI hooks; skip them in the coverage test.
- Where `bind_ui` stores callbacks (the coverage test reads them back — grep for the assignment, e.g. `self.deps.ui[...] = ...`).
- `PlanDecision`'s module + constructor fields (grep `class PlanDecision`).
- Exact `on_workflow_*` callback signatures (bind_ui docstring + call sites) and `on_subagent_notice/model/thinking/usage` signatures (UIHooks types).

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_server_host.py` (follow its existing host+harness fixture style):

```python
import inspect

from marim_harness.runtime.harness import Harness

_BIND_UI_PARAMS = [
    p
    for p in inspect.signature(Harness.bind_ui).parameters
    if p not in ("self", "detach_fanout", "interactive", "notifier")
]


def test_every_bind_ui_param_is_wired_by_the_host(<host fixture args>):
    # The headline guarantee of phase 3a: SessionHost is the SOLE bind_ui
    # consumer, so every hook must be installed (a missed param leaves a
    # silent dead callback that only the daemon ever exercised).
    host = SessionHost(harness, bus)
    for name in _BIND_UI_PARAMS:
        assert harness.deps.ui[name] is not None, name
```

(Adjust `harness.deps.ui[name]` to the actual storage found in step 1; build the host exactly like the file's existing tests.)

Then behavior tests (names indicative):
- `test_workflow_events_published`: drive each `deps.ui["on_workflow_*"]` with sample args; assert the bus ring carries `workflow.spawned/started/logged/finished/spawn_finished` with the renderer-contract keys (`stream_id`, `spawn_type`, `task`, `parent_tool_call_id` / `tool_call_id`, `title` / `tool_call_id`, `message` / `tool_call_id`, `outcome`, `failed` / `stream_id`, `report`).
- `test_subagent_side_channels_published`: drive `on_subagent_notice("s1", "retrying")`, `on_subagent_model("s1", "opus")`, `on_subagent_thinking("s1", "high")`, `on_subagent_usage("s1", <usage object>); assert `subagent.notice/model/thinking/usage` payloads.
- `test_cli_activity_converts_events_to_wire`: drive `on_cli_activity([<one real pydantic-ai stream event that event_to_dict handles>])`; assert `subagent.cli_activity` with `events` a non-empty list of dicts whose `"type"` is a wire name (reuse event-building helpers from `tests/test_stream_events.py`).
- `test_ttft_mode_and_notice_published`: drive `on_ttft(0.5)`, `on_mode_change(...)`, `on_notice("heads up")`; assert `session.ttft {"seconds": 0.5}`, `session.mode_changed {"mode": <workspace mode value>}`, `session.notice {"message": "heads up"}`.
- `test_present_plan_parks_and_resolves`: launch `deps.ui["on_present_plan"]("Sum", ["a"], ["Now", "Later"])` as a task; await the `ask.pending` event (kind `"plan"`, payload carrying summary/steps/choices); `host.answer_ask(ask_id, {"choice": "Now", "feedback": None})`; assert the task returns a `PlanDecision` with that choice. Also assert a `{"cancel": True}` answer maps to whatever the TUI's Esc currently produces for plans (verify in step 1 — mirror it exactly).
- `test_run_turn_publishes_lifecycle_and_returns_outcome`: scripted harness (pattern: existing host tests); `outcome = await host.run_turn("hi")`; bus carries `turn.started` then `turn.finished` with `output`/`usage`; `outcome.result` matches; session history persisted.
- `test_run_turn_error_publishes_and_raises`: harness whose model raises; assert `turn.error` on the bus AND the exception propagates to the caller (the TUI's error arms rely on it).
- `test_run_turn_rejects_concurrent_second_call`: start one `run_turn` (block it via the existing test hook), second call raises `RuntimeError`.
- `test_run_turn_interrupt_publishes_finished_interrupted`: cancel the awaiting task; assert `turn.finished` with `{"interrupted": True}` is published and `CancelledError` propagates.

Run: `uv run pytest tests/test_server_host.py -x --no-cov` → new tests FAIL (callbacks unwired / no run_turn).

- [ ] **Step 3: Implement host.py**

a) **Extend the `bind_ui` call in `__init__`** — keep the 8 existing entries, add all others. New publishers (methods next to `_on_subagent_event`/`_on_jobs_changed`):

```python
    def _on_subagent_notice(self, stream_id: str, message: str) -> None:
        self._publish("subagent.notice", {"stream_id": stream_id, "message": message})

    def _on_subagent_model(self, stream_id: str, model: str) -> None:
        self._publish("subagent.model", {"stream_id": stream_id, "model": model})

    def _on_subagent_thinking(self, stream_id: str, level: str) -> None:
        self._publish("subagent.thinking", {"stream_id": stream_id, "level": level})

    def _on_subagent_usage(self, stream_id: str, usage: object) -> None:
        dump = usage.model_dump() if hasattr(usage, "model_dump") else {}
        self._publish("subagent.usage", {"stream_id": stream_id, "usage": dump})
```

`_on_subagent_event`: keep its conversion, but include usage when provided (`{"stream_id", "event": obj, "usage": <dump or omitted>}`) — the renderer folds usage onto the card (parity with today's `on_subagent_event(stream_id, event, usage=None)`). Match the actual UIHooks signature found in step 1.

```python
    def _on_cli_activity(self, events: list) -> None:
        wire = [obj for e in events if (obj := event_to_dict(e)) is not None]
        if wire:
            self._publish("subagent.cli_activity", {"events": wire})
```

Workflow adapters publish the renderer-contract dicts (keys fixed by Task 1's models); map each callback's positional args per the step-1 signatures. Example shape (fix names to disk):

```python
    def _on_workflow_start(self, engine_id: str, title: str) -> None:
        self._publish("workflow.started", {"tool_call_id": engine_id, "title": title})
```

Simple lambdas inline in the `bind_ui` call:

```python
            on_ttft=lambda seconds: self._publish("session.ttft", {"seconds": seconds}),
            on_mode_change=lambda _mode: self._publish(
                "session.mode_changed", {"mode": harness.deps.workspace.mode.value}
            ),
            on_notice=lambda message: self._publish("session.notice", {"message": message}),
```

(Verify `mode` lives where shown; if `harness.deps.workspace.mode` isn't the right path, read the current mode from wherever `on_mode_change`'s callers have it — the payload must be the mode string.)

b) **`_present_plan`** next to `_request_approval`/`_ask_user` (import `PlanDecision` from its verified module):

```python
    async def _present_plan(self, summary: str, steps: list[str], choices: list[str]):
        ask = self._park("plan", {"summary": summary, "steps": steps, "choices": choices})
        try:
            answer = await ask.future
        finally:
            self._pending.pop(ask.id, None)
            self._publish_status()
        if answer.get("cancel"):
            return <mirror current TUI Esc behavior found in step 1>
        return PlanDecision(choice=str(answer.get("choice") or ""), feedback=answer.get("feedback"))
```

c) **Provisional `run_turn`** — split `_run_one_turn`:

```python
    async def _turn_body(self, turn_id, prompt, attachments, trigger: str):
        self.bus.publish("turn.started", {"turn_id": turn_id, "prompt": prompt, "trigger": trigger})
        self._publish_status()

        async def handler(ctx, events):
            async for event in events:
                obj = event_to_dict(event)
                if obj is None:
                    continue
                wire_type = STREAM_EVENT_TYPES.get(obj.pop("type"))
                if wire_type is not None:
                    self.bus.publish(wire_type, obj)

        return await self.harness.run_turn(
            prompt, event_stream_handler=handler, attachments=attachments
        )

    def _publish_finished(self, turn_id: str, outcome) -> None:
        # `turn.finished.output` is a wire string: the CLI preset never configures
        # structured output, so result is always the turn's text.
        self.bus.publish(
            "turn.finished",
            {
                "turn_id": turn_id,
                "output": outcome.result or "",
                "usage": usage_summary(self.harness.session.usage, self.harness.model_id),
            },
        )
```

`_run_one_turn` keeps the queue-path semantics (swallow → `turn.error`, return; `CancelledError` re-raises into the worker loop, which owns the `interrupted` publish). New public method:

```python
    async def run_turn(
        self, prompt: str, attachments: list[tuple[bytes, str]] | None = None
    ):
        """Provisional (phase 3a): drive ONE turn synchronously for the
        in-process TUI, which still awaits turn completion. Bypasses the
        queue. Publishes the same vocabulary as the queued path. Raises on
        provider error (the TUI's error arms own the UX) — unlike the queued
        path, which swallows into turn.error. 3b removes this when turn
        completion inverts onto the host."""
        if self._turn_task is not None:
            raise RuntimeError("a turn is already in flight")
        loop = asyncio.get_running_loop()
        turn_id = secrets.token_hex(8)
        self._turn_task = loop.create_task(self._turn_body(turn_id, prompt, attachments, "user"))
        try:
            try:
                outcome = await self._turn_task
            except asyncio.CancelledError:
                if not self._closing:
                    self.bus.publish("turn.finished", {"turn_id": turn_id, "interrupted": True})
                raise
            except Exception as exc:  # publish for event consumers, then surface
                detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
                logger.warning("turn %s failed: %s", turn_id, detail, exc_info=True)
                self.bus.publish("turn.error", {"turn_id": turn_id, "error": detail})
                raise
        finally:
            self._turn_task = None
            self._cancel_pending("interrupted")
            self._idle_since = loop.time()
            self._publish_status()
            if not self._closing:
                self._wake.maybe_wake()
        self._publish_finished(turn_id, outcome)
        return outcome
```

Annotate the return type with `TurnOutcome` (import from wherever `harness.run_turn`'s annotation gets it).

d) **`AskAnswerIn` plan arm** (`schema.py`): add `choice: str | None = None` and `feedback: str | None = None` with a docstring note that plan answers carry `{"choice", "feedback"}`; keep `as_answer` passing them through if it whitelists fields (verify — extend if so).

- [ ] **Step 4: Gates**

`uv run pytest tests/test_server_host.py --no-cov` → green; then full: `uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright && uv run pytest` → green.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests
git add src/marim_harness/server/host.py src/marim_harness/server/schema.py tests/test_server_host.py
git commit -m "feat(server): SessionHost publishes the full bind_ui vocabulary + provisional run_turn"
```

---

### Task 3: Renderer consumes wire events

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py`
- Modify: `tests/test_app.py` (or a new `tests/test_stream_render_wire.py` following `tests/test_ask_user_render.py`'s fixture style)

**Interfaces:**
- Consumes: Task 1 models (`parse_wire_event`/typed union) or raw wire dicts at the renderer boundary; `event_to_dict` (`server/stream_events.py`) for the temporary adapters.
- Produces: `StreamRenderer.on_wire(wire: WireEvent)` (top-level stream events), `on_subagent_wire(stream_id, wire, usage=None)`, `on_cli_activity_wire(events: list[WireEvent])`.

**Key seam (current code, stream_render.py ~946+):** `dispatch_stream_event` isinstance-routes pydantic-ai events to `_on_text_start` / `_on_text_delta` / `_on_thinking_start` / `_on_thinking_delta` / `_on_tool_call` / `_on_tool_result`, then calls `_finalize_stale_blocks(event, sink)`. The wire format flattens start+delta into one `text.delta` type — the renderer must carry the "is a message open" state itself (the sinks already track current assistant/thinking widgets; use that).

- [ ] **Step 1: Read the dispatch seam on disk**

Read `dispatch_stream_event`, all six `_on_*` handlers, `_finalize_stale_blocks`, `_TopLevelSink`/`_SubAgentSink`, `on_events`, `on_subagent_event`, `on_cli_activity` (~831-1038). STOP/BLOCKED if they don't match the description above.

- [ ] **Step 2: Write failing wire-dispatch tests**

Follow existing renderer-test fixture style (app tests build a `HarnessApp` under a Pilot; drive `app.stream` directly). Script wire DICTS through the new entry points and assert widget outcomes:
- two `text.delta` in a row → ONE assistant message with concatenated text; a `text.delta` after a `tool.result` → a NEW assistant message below the tool row (start-state reconstruction).
- `thinking.delta` stream followed by `text.delta` → thinking block finalized/collapsed, assistant message opens.
- `tool.call` → pending `ToolCallWidget` with name/args; matching `tool.result` → finished with content.
- `tool.call` after open assistant text → stale text finalized first (the `_finalize_stale_blocks` parity).
- subagent: `on_subagent_wire(stream_id, <tool.call dict>)` routes into the owning `SubAgentWidget`'s pane sink (mirror existing subagent stream tests).
- cli activity: `on_cli_activity_wire([...])` renders main-transcript cards like today's `on_cli_activity`.

Run → FAIL (entry points missing).

- [ ] **Step 3: Implement**

a) Refactor the six handlers to take PLAIN DATA (text: str / tool name, args, id / result id, content) + sink, and route on the sink's open-state for start-vs-delta. Keep the sink protocol unchanged.

b) New dispatch (renderer contract is the typed model; the pump parses):

```python
    async def on_wire(self, wire) -> None:
        """Top-level wire event (parsed by the app's pump)."""
        sink = _TopLevelSink(self, self._log_container())
        if isinstance(wire, TextDelta):
            ...open-if-needed, then append...
        elif isinstance(wire, ThinkingDelta):
            ...
        elif isinstance(wire, ToolCall):
            await self._on_tool_call_data(wire.id, wire.name, wire.args, sink)
        elif isinstance(wire, ToolResult):
            await self._on_tool_result_data(wire.id, wire.content, sink)
        else:
            return
        self._finalize_stale_blocks_wire(wire, sink)
```

(`_finalize_stale_blocks_wire` mirrors the current event-based finalize, keyed on wire type.)

c) **Temporary adapters keep the old paths green until Task 4 deletes them:** reimplement `on_events`, `on_subagent_event`, `on_cli_activity` by converting each pydantic-ai event with `event_to_dict` + `STREAM_EVENT_TYPES` remap and delegating to the wire dispatch. After this, `stream_render.py` has **no `pydantic_ai.messages` imports** (the spec requirement) — only `server.stream_events`. `on_subagent_wire(stream_id, wire, usage=None)` also owns the usage fold-in: `usage` arrives as a WIRE DICT (RunUsage dump) — adapt `note_subagent_usage` or its callee to accept it (today it takes a RunUsage object; pick the smallest conversion that keeps the breadcrumb/pane usage line identical).

- [ ] **Step 4: Gates**

New tests green; **all existing renderer-driven app tests stay green** (they exercise the adapters — that's the parity gate). Full: ruff check, ruff format --check, pyright, pytest.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests
git add src/marim_harness/interfaces/tui/stream_render.py tests/
git commit -m "feat(tui): stream renderer consumes wire events"
```

---

### Task 4: App wires EventBus + SessionHost, pump, provisional turn call

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py`
- Modify: `tests/test_app.py` (+ any fixture helpers)

**Interfaces:**
- Consumes: `EventBus`, `SessionHost` (with Task 2's full publish + `run_turn`), Task 1 `parse_wire_event`, Task 3 `on_wire`/`on_subagent_wire`/`on_cli_activity_wire`.
- Produces: `HarnessApp.host`; `app._event_pump()` worker; `app._dispatch_wire(model)`.

- [ ] **Step 1: Map the old `bind_ui` targets**

Read `app.py` ~166-189 (the 22-kwarg `harness.bind_ui(...)` block) and `_run_turn` (~493-544), `on_mount` (~212-278), the Esc/interrupt path, teardown (~330-358). Record, per callback, the TARGET method its lambda/closure invokes (e.g. `on_mode_change=self.session.refresh_status_bar`, `on_notice=self.append_notice`, `on_rename=...`, `on_compact=...`, workflow → `stream.claim_workflow_card`/`finish_workflow_card`/pane-log, `on_ttft=self.stream.on_ttft`). The pump handlers must call these SAME targets. STOP/BLOCKED on surprises.

- [ ] **Step 2: Write failing tests**

- `test_app_builds_host_and_pump`: construct the app; `app.host` is a `SessionHost`; `app.host.harness is app.harness`; `app.host._claim is None` (claim invariant).
- `test_pump_renders_scripted_turn_via_wire`: pilot-run the app with a scripted `TestModel`; submit a prompt; assert the assistant text lands in the transcript. (Existing scripted-turn tests in `test_app.py` already assert this — they MUST stay green unchanged; they are the parity gate. This new test additionally asserts the render came via the bus: e.g. spy/assert `harness.deps.ui["on_subagent_event"]` belongs to the host, or simply that `app.harness.deps.ui` callbacks are the host's methods.)
- `test_publish_tasks_changed_reaches_activity_ui`: publish `tasks.changed` on `app.host.bus` directly; assert the tasks UI updates (pick the smallest observable target from the step-1 map). Wait for a real condition (widget state), never `pilot.pause()` alone.

Run → FAIL.

- [ ] **Step 3: Implement**

a) `HarnessApp.__init__`: after the harness exists, create `self._bus = EventBus()` and `self.host = SessionHost(harness, self._bus)` (claim defaults to None — never pass one). DELETE the entire `bind_ui` block (~22 kwargs). Keep everything else in `__init__`.

b) Pump — start in `on_mount` alongside existing startup, via Textual's `self.run_worker(self._event_pump(), exclusive=False, group="event-pump")`:

```python
    async def _event_pump(self) -> None:
        sub = self.host.bus.attach()
        try:
            while True:
                event = await sub.next_event()
                # bus Event carries `type` and `data` separately; the wire dict is their merge
                wire = parse_wire_event({"type": event.type, **event.data})
                if wire is None:
                    continue
                self._dispatch_wire(wire)
        finally:
            await sub.aclose()  # verify Subscription teardown API in bus.py; use what exists
```

c) `_dispatch_wire(self, wire)` routes every Task 1 model to the step-1 targets:
- `TextDelta/ThinkingDelta/ToolCall/ToolResult` → `await self.stream.on_wire(wire)` (pump must `await` it — the dispatch is async).
- `SubagentEvent` → parse nested `wire.event` with `parse_wire_event`; if known, `await self.stream.on_subagent_wire(wire.stream_id, nested, wire.usage)`; usage dict → rebuild per Task 3's expected shape (see its `usage` handling).
- `SubagentCliActivity` → `await self.stream.on_cli_activity_wire([parse each; skip Nones])`.
- `SubagentNotice/Model/Thinking/Usage` → `stream.on_subagent_notice/model/thinking/usage` wire shapes.
- `WorkflowSpawned/Started/Logged/Finished/SpawnFinished` → the same stream/workflow-card methods the old `on_workflow_*` closures called.
- `SessionTtft` → `self.stream.on_ttft(wire.seconds)`; `SessionModeChanged` → the old `on_mode_change` target; `SessionNotice` → `self.append_notice(wire.message)`; `SessionRenamed`/`TasksChanged`/`JobsChanged`/`CompactionStarted`/`CompactionFinished` → the old targets.
- `AskPending`/`AskResolved` → Task 5's handlers (leave a `pass`/TODO-free stub that Task 5 replaces — no, do NOT stub: Task 5 adds the branches; here, ignore unknown-to-3a kinds silently).
- `TurnStarted/TurnFinished/TurnError/SessionStatus/StreamGap` → no-op in 3a (3b owns completion/status; gap is remote-only). Leave a comment naming the phase.

Task 3 fix-round note: `SubagentUsage.usage` on the wire is a dict (a `RunUsage` dump), not a live `RunUsage` — the pump must convert it with `usage.usage_from_dump` before handing it to `stream.on_subagent_usage`/`note_subagent_usage`, exactly like `on_subagent_wire` already does for the `usage` folded onto a subagent stream event; a raw dict reaches `.total_tokens` on the flush tick and `AttributeError`s. Separately, since the pump has no `on_events` call at all (that adapter is Task 4's own thing to delete), whichever handler marks a run/turn boundary (`TurnStarted` and/or `TurnFinished`) must also clear `self.stream.text_open` — mirroring `on_events`' own reset — or the cross-turn text-open leak fixed in Task 3's fix round reopens on the pure-wire pump.

d) Turn call: in `_run_turn` (~493-544), replace

```python
outcome = await self.harness.run_turn(
    text, event_stream_handler=self.stream.on_events, attachments=attachments
)
```

with

```python
outcome = await self.host.run_turn(text, attachments=attachments)
```

Keep ALL surrounding busy-flag/timer/except/finally logic byte-identical (3b inverts completion; not now). The Esc/interrupt path keeps its current cancel mechanism — cancellation propagates through `host.run_turn` into the host's `CancelledError` arm (publishes `interrupted`, cancels pending asks). Verify the interrupt path still settles `settle_pending` and busy flags exactly as before.

e) Teardown unchanged (`cancel_autoname` → persist → `session_end` → `harness.aclose()` in app; `release_claim` in `default_cmd`'s finally). The pump worker dies with the app; the host's `_worker_loop` task is abandoned exactly as the daemon-less TUI abandons nothing else — VERIFY: does anything await `host.aclose()`? NO — do not add it (teardown invariant). But the host's queue worker task must not outlive app shutdown noisily: confirm Textual's app-exit cancels run_workers and that `SessionHost.__init__`'s `loop.create_task(self._worker_loop())` gets garbage-collected silently; if it logs warnings, cancel it in the app's existing teardown via `host.interrupt()` + worker cancel — smallest change that keeps logs clean, documented in a comment.

- [ ] **Step 4: Gates**

All of `tests/test_app.py` (+ ask/tool/workflow app tests) green via the pump — that IS the parity proof. Full gates: ruff check, ruff format --check, pyright, pytest.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests
git add src/marim_harness/interfaces/tui/app.py tests/
git commit -m "feat(tui): render from an in-process SessionHost via bus pump"
```

---

### Task 5: Asks via `ask.pending`/`ask.resolved`

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (pump ask handlers)
- Modify: `src/marim_harness/interfaces/tui/interactions/base.py` (non-blocking mount)
- Modify: `tests/test_approval.py`, `tests/test_ask_user_render.py`, `tests/test_app_present_plan.py`

**Interfaces:**
- Consumes: Task 2's parked asks (`ask.pending` kinds `approval`/`question`/`plan`), `host.answer_ask`, Task 1 `AskPending`/`AskResolved`.
- Produces: event-driven panel lifecycle; answer payload contract — approval `{"approve": bool}`, question `{"answers": dict}` or `{"cancel": True}`, plan `{"choice": str, "feedback": str|None}` or `{"cancel": True}`.

- [ ] **Step 1: Read the current ask flows on disk**

Read `app.py`'s `request_approval`/`ask_user`/`present_plan` wiring (~807-835, the `run_panel(...)` calls), `interactions/base.py::run_panel` (~98-132: mount + await `panel.result` + finally remove/focus-restore), and the three ask test files. Note exactly: panel constructors, desktop-notify calls at panel open, Esc/cancel results, and what `run_panel`'s `finally` restores. STOP/BLOCKED on surprises.

- [ ] **Step 2: Write failing tests**

- Round-trip per kind via a REAL trigger where the existing tests have one (scripted model calling a gated tool → approval; `ask_user` tool; plan-presenting model) — adapt the existing tests' timing: after triggering, WAIT for the panel to appear (poll query with a deadline; never bare `pilot.pause()`), interact, then wait for dismissal.
- `test_remote_answer_dismisses_local_panel` (the cross-client seam): pilot-run the app; trigger an approval panel; then `app.host.answer_ask(ask_id, {"approve": True})` directly (simulating another client); assert the panel disappears and the turn proceeds — the panel must dismiss off `ask.resolved`, not off local interaction.
- `test_plan_answer_payload`: drive `host.deps.ui` `on_present_plan` (or the scripted plan model); answer via the panel; assert `host` received `{"choice": ..., "feedback": ...}` (spy `answer_ask` or check the `PlanDecision` the harness saw).

Run → FAIL.

- [ ] **Step 3: Implement**

a) `interactions/base.py`: extract the MOUNT half of `run_panel` into `mount_panel(app, panel) -> panel` (same placement logic, no await, no removal). Keep `run_panel` implemented via `mount_panel` + await for any remaining caller (grep first; if none remain after this task, deleting it is fine — note it in the report).

b) `app.py` pump handlers + state `self._ask_panels: dict[str, InteractionPanel]`:

```python
    def _on_ask_pending(self, wire: AskPending) -> None:
        panel = self._panel_for_ask(wire)  # kind → ApprovalPanel/AskUserPanel/PlanCard,
        # constructors + desktop-notify copied VERBATIM from the old wiring
        self._ask_panels[wire.id] = panel
        mount_panel(self, panel)
        self.run_worker(self._answer_ask(wire.id, panel), group="asks")

    async def _answer_ask(self, ask_id: str, panel) -> None:
        result = await panel.result
        self.host.answer_ask(ask_id, self._ask_payload(panel, result))

    def _on_ask_resolved(self, wire: AskResolved) -> None:
        panel = self._ask_panels.pop(wire.id, None)
        if panel is not None:
            ...remove + focus restore, mirroring run_panel's finally...
```

`_ask_payload` maps per kind: `ApprovalPanel` bool → `{"approve": result}`; `AskUserPanel` `dict|None` → `None → {"cancel": True}` else `{"answers": result}`; `PlanCard` result → `{"choice": ..., "feedback": ...}` (Esc/cancel result → `{"cancel": True}` — must line up with the Task 2 cancel mirror).

c) DELETE the old `request_approval`/`ask_user`/`present_plan` wiring from `app.py` (those callbacks now live only on the host).

d) Interrupt parity: cancelling a turn → host `_cancel_pending("interrupted")` resolves parked futures → `ask.resolved` fires → panels dismiss through `_on_ask_resolved`. Verify with the existing interrupt tests (adapt timing only).

- [ ] **Step 4: Gates**

All three ask test files green (adapted), full gates: ruff check, ruff format --check, pyright, pytest.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src tests
git add src/marim_harness/interfaces/tui/ tests/
git commit -m "feat(tui): approvals/questions/plans via ask.pending + ask.resolved"
```

---

## Verification (after all tasks)

- [ ] Full gates, in order: `uv run ruff check src tests` → `uv run ruff format --check src tests` → `uv run pyright` → `uv run pytest` → `uv run --python 3.10 pytest` (CI 3.10 leg parity).
- [ ] Spec sweep against `docs/superpowers/specs/2026-09-01-tui-events-3a-design.md`: (a) sole bind_ui consumer — grep `bind_ui` in `interfaces/` finds nothing; (b) `stream_render.py` has no `pydantic_ai.messages` import; (c) claims — in-process host claim stays None end-to-end; (d) teardown unchanged — no `host.aclose()` on the TUI exit path; (e) no compat flag.
- [ ] Update `docs/reference/serve-api.md` (or the TUI/serve docs the parent spec names) if the new wire types belong in the published vocabulary table — one docs commit.
- [ ] Manual smoke (FREE model only: `MARIM_PROVIDER=zen MARIM_MODEL=mimo-v2.5-free`): TUI turn rendering text + one gated-tool approval + Esc interrupt; compare visually against pre-branch behavior; confirm the `.json.claim` sidecar lifecycle is unchanged (claim while running, released on exit).
- [ ] Whole-branch review (claude-deep) + fix wave before PR.

## SDD execution notes

- One fresh implementer subagent per task; two-stage review between tasks (spec-compliance then quality). Never batch tasks.
- Model policy: Task 1 claude-fast; Tasks 2/4/5 claude-general; Task 3 claude-deep (renderer state machine). CLI agents have a 600s ceiling — if one times out late, inspect `git status`/diff and finish inline or with a continuation agent (this happened repeatedly on claim-hygiene).
- Quote-drift rule: every "current code" anchor above was checked at `b9c5dbb1` on 2026-09-01; mismatch on disk ⇒ BLOCKED report, no improvising.
- Branch: `feat/tui-events-3a` (already cut). Tasks are ordered; do not merge Task 3 alone.
