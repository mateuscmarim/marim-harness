# Claude Code CLI Sub-agent Demux Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a `claude` CLI process (either a `backend: claude-cli` sub-agent spawn or the claude-cli main-loop provider) spawns its *own* sub-agents via Claude Code's Agent/Task tool, render each one as a first-class card in marim's sub-agents screen — identical to a native `spawn_agent` — with live transcript, usage, model, tree nesting, and completion status.

**Architecture:** Claude Code stream-json tags every sub-agent-originated line with a top-level `parent_tool_use_id` pointing at the spawning `tool_use` id, and emits `system/task_started` + `system/task_notification` lifecycle events for async spawns. A new pure demultiplexer (`CliSubagentDemux`) converts that interleaved stream into the exact event shapes the TUI already consumes: a synthesized `spawn_agent` `FunctionToolCallEvent` (which `_TopLevelSink`/`_SubAgentSink.intercept_tool` already turn into a live card keyed by tool_call_id), per-child translated events routed by stream id through the existing `on_subagent_event` callback, and a synthesized `spawn_agent` `ToolReturnPart` on completion. Both consumers (the sub-agent CLI backend runner and the main-loop model) share the demux; **zero changes to the renderer or the sub-agents screen are needed**.

**Tech Stack:** Python 3.10+, pydantic-ai message/event types, pytest (anyio), existing fake-CLI-script test pattern.

## Global Constraints

- `requires-python >= 3.10` — no 3.11+-only syntax.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; import sorting enforced.
- Run `uv run ruff check src tests` → `uv run pyright` → `uv run pytest` (that order) before claiming a task done. Use `uv run pytest --no-cov <file>` for fast single-file runs.
- All test commands via `uv` — never bare `python`/`pytest`.
- Tool/module docstrings are product surface — keep the long "why" comment style of the codebase.
- Preserve the existing invariant: a persisted transcript should not end with a `ToolCallPart` lacking its `ToolReturnPart` where avoidable.

## Verified stream facts (probe against claude CLI 2.1.198 — do not re-derive)

- The spawn tool is named **`Agent`** (older CLIs used `Task`); its `tool_use.input` is `{description, subagent_type, prompt}`.
- Child lines are `assistant`/`user` objects with **top-level `parent_tool_use_id`** = the spawn's tool_use id. They also carry top-level `subagent_type`/`task_description` (unused here; the tool_use input suffices).
- Async lifecycle: `system/task_started {task_id, tool_use_id, description, subagent_type, prompt}` → immediate `tool_result` for the spawn containing **launch metadata, not the report** → `system/task_updated {patch}` → `system/task_notification {tool_use_id, status, summary, usage:{total_tokens, tool_uses, duration_ms}}`.
- Legacy sync shape (older CLIs): no `task_started`; the spawn's `tool_result` **is** the final report.
- One `claude -p` process can emit **multiple `result` events** (an async spawn's notification re-invokes the main agent). Per-result `usage` token buckets are **per-segment** (sum them); `total_cost_usd` is **cumulative** (take the last). The last `result.result` is the final report text.
- `message.usage` repeats identically on every stream event of the same API message — dedupe by `message.id`.
- Today's bugs this plan also fixes: `consume_cli_stream` **returns at the first `result`**, which closes the generator and `spawn_cli_objects`' `finally` **kills the CLI while its async sub-agent still runs**; child text blocks leak into the main response text.

## File Structure

- **Modify** `src/marim_harness/subagents/cli_backend.py` — translator thinking blocks + `record_call`/`record_return`; `sum_result_usages`; `CliResult.child_transcripts`; `ClaudeCliRunner` demux glue + multi-result.
- **Create** `src/marim_harness/subagents/cli_demux.py` — `RoutedEvent`, `CliSubagentDemux` (pure; imports *from* `cli_backend`; `cli_backend` imports it lazily inside `run()` to avoid the cycle).
- **Modify** `src/marim_harness/subagents/runner.py` — persist child transcripts in `_execute_cli_spawn`.
- **Modify** `src/marim_harness/config/claude_cli_model.py` — headless child filtering, multi-result survival, `Agent`/`Task` activity line, `on_subagent`/`on_subagent_model` side-channels + demux tee in the streamed path.
- **Modify** `src/marim_harness/runtime/harness.py` — `_wire_cli_model` binds the two new model callbacks.
- **Tests:** `tests/test_subagents_cli.py` (translator/helper/runner), **create** `tests/test_cli_demux.py`, `tests/test_subagent_cli_spawn.py` (persistence), `tests/test_claude_cli_model.py` (model paths), `tests/test_bootstrap.py` (wiring).

---

### Task 1: `CliStreamTranslator` — thinking blocks + transcript record helpers

**Files:**
- Modify: `src/marim_harness/subagents/cli_backend.py` (imports at top; `CliStreamTranslator._assistant`; new methods after `transcript()`)
- Test: `tests/test_subagents_cli.py`

**Interfaces:**
- Produces: `CliStreamTranslator.record_call(part: ToolCallPart) -> None` and `record_return(part: ToolReturnPart) -> None` (used by the demux and Task 4's runner glue); thinking blocks now translate to `PartStartEvent(ThinkingPart)` + `PartDeltaEvent(ThinkingPartDelta)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagents_cli.py` (extend the existing import block with `PartStartEvent` already there; add `ThinkingPart, ThinkingPartDelta` to the `pydantic_ai.messages` import):

```python
def test_translate_thinking_block_emits_thinking_events():
    t = CliStreamTranslator()
    events = t.translate({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "pondering...", "signature": "sig"},
    ]}})
    assert isinstance(events[0], PartStartEvent)
    assert isinstance(events[0].part, ThinkingPart)
    assert isinstance(events[1], PartDeltaEvent)
    assert isinstance(events[1].delta, ThinkingPartDelta)
    assert events[1].delta.content_delta == "pondering..."
    # the transcript carries the thought too
    parts = t.transcript()[0].parts
    assert isinstance(parts[0], ThinkingPart) and parts[0].content == "pondering..."


def test_record_call_and_return_append_transcript_pair():
    t = CliStreamTranslator()
    t.record_call(ToolCallPart(tool_name="spawn_agent", args={"type": "Explore"},
                               tool_call_id="t1"))
    from datetime import datetime, timezone
    t.record_return(ToolReturnPart(
        tool_name="spawn_agent", content="4", tool_call_id="t1",
        timestamp=datetime.now(tz=timezone.utc), outcome="success",
    ))
    msgs = t.transcript()
    assert isinstance(msgs[0], ModelResponse)
    assert msgs[0].parts[0].tool_name == "spawn_agent"
    assert isinstance(msgs[1], ModelRequest)
    assert msgs[1].parts[0].content == "4"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -k "thinking or record_call" -v`
Expected: FAIL (`ThinkingPart` import error first — add it to the test import, then `AttributeError: record_call`; thinking test fails with `events == []`... actually `IndexError`).

- [ ] **Step 3: Implement**

In `src/marim_harness/subagents/cli_backend.py`, extend the `pydantic_ai.messages` import with `ThinkingPart, ThinkingPartDelta`. In `CliStreamTranslator._assistant`, add a branch after the `text` branch:

```python
            elif btype == "thinking":
                idx = self._index
                self._index += 1
                events.append(PartStartEvent(index=idx, part=ThinkingPart(content="")))
                events.append(PartDeltaEvent(
                    index=idx,
                    delta=ThinkingPartDelta(content_delta=block.get("thinking", "")),
                ))
                resp_parts.append(ThinkingPart(content=block.get("thinking", "")))
```

After `transcript()`, add:

```python
    def record_call(self, part: ToolCallPart) -> None:
        """Append a synthesized tool call (e.g. the demux's spawn_agent for a
        Claude-side sub-agent) to the transcript, and remember its name so a
        later synthesized return — or a raw tool_result hitting translate() —
        labels itself correctly."""
        self._call_names[part.tool_call_id] = part.tool_name
        self._messages.append(ModelResponse(parts=[part]))

    def record_return(self, part: ToolReturnPart) -> None:
        """Append a synthesized tool return, closing a record_call so a
        persisted sidecar never carries an unanswered call."""
        self._messages.append(ModelRequest(parts=[part]))
```

Also update the `CliStreamTranslator` class docstring: mention that thinking blocks render as collapsed thoughts and that `record_call`/`record_return` exist for the demux's synthesized spawn parts.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -v`
Expected: ALL PASS (existing translate tests must stay green — the thinking branch is additive).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/subagents/cli_backend.py tests/test_subagents_cli.py
git commit -m "feat(cli-backend): translate thinking blocks; add transcript record helpers"
```

---

### Task 2: `sum_result_usages` — fold multiple CLI `result` events

**Files:**
- Modify: `src/marim_harness/subagents/cli_backend.py` (new function directly under `synth_usage`)
- Test: `tests/test_subagents_cli.py`

**Interfaces:**
- Produces: `sum_result_usages(results: list[dict]) -> tuple[dict, int, float | None]` returning `(summed_usage, total_num_turns, last_cumulative_cost)` — feeds `synth_usage(*sum_result_usages(results))` (Task 4) and `request_usage_from_cli(summed, cost)` (Task 6).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagents_cli.py` (add `sum_result_usages` to the `cli_backend` import):

```python
def test_sum_result_usages_sums_tokens_and_keeps_last_cumulative_cost():
    r1 = {"num_turns": 2, "total_cost_usd": 0.04,
          "usage": {"input_tokens": 18, "output_tokens": 1083,
                    "cache_read_input_tokens": 44348,
                    "cache_creation_input_tokens": 10455,
                    "cache_creation": {"ephemeral_1h_input_tokens": 10455}}}
    r2 = {"num_turns": 1, "total_cost_usd": 0.05,
          "usage": {"input_tokens": 10, "output_tokens": 48,
                    "cache_read_input_tokens": 28039,
                    "cache_creation_input_tokens": 1942}}
    summed, turns, cost = sum_result_usages([r1, r2])
    assert summed["input_tokens"] == 28 and summed["output_tokens"] == 1131
    assert summed["cache_read_input_tokens"] == 44348 + 28039
    assert "cache_creation" not in summed  # nested dicts skipped
    assert turns == 3
    assert cost == 0.05  # total_cost_usd is cumulative — last wins


def test_sum_result_usages_tolerates_missing_fields():
    summed, turns, cost = sum_result_usages([{"usage": None}, {}])
    assert summed == {} and turns == 0 and cost is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -k sum_result -v`
Expected: FAIL with `ImportError: cannot import name 'sum_result_usages'`.

- [ ] **Step 3: Implement**

In `cli_backend.py`, directly below `synth_usage`:

```python
def sum_result_usages(results: list[dict]) -> tuple[dict, int, float | None]:
    """Fold one CLI run's ``result`` events into ``(usage, num_turns, cost)``
    ready for ``synth_usage`` / ``request_usage_from_cli``.

    One ``claude -p`` process can emit SEVERAL result events: an async
    sub-agent's completion notification re-invokes the main agent, which ends
    in another result. Token buckets are per-segment, so they are summed;
    ``total_cost_usd`` is cumulative across the whole process, so the LAST
    value is the run's cost (both verified against a live 2.1.198 stream).
    Nested non-numeric usage values (``cache_creation``, ``server_tool_use``)
    are skipped."""
    summed: dict = {}
    turns = 0
    cost: float | None = None
    for r in results:
        for k, v in (r.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                summed[k] = summed.get(k, 0) + v
        turns += int(r.get("num_turns", 0) or 0)
        if r.get("total_cost_usd") is not None:
            cost = float(r["total_cost_usd"])
    return summed, turns, cost
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -k sum_result -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/subagents/cli_backend.py tests/test_subagents_cli.py
git commit -m "feat(cli-backend): fold multi-result CLI usage (sum tokens, last cumulative cost)"
```

---

### Task 3: `CliSubagentDemux` — the pure routing core

**Files:**
- Create: `src/marim_harness/subagents/cli_demux.py`
- Test: `tests/test_cli_demux.py` (new)

**Interfaces:**
- Consumes: `CliStreamTranslator` (+ its Task 1 `record_call`/`record_return`), `synth_usage`, `_flatten_tool_result` from `cli_backend`.
- Produces:
  - `RoutedEvent` dataclass: `stream_id: str | None` (None = the caller's own/main stream; else the spawn tool_use id == the card's stream id), `event: object` (a pydantic-ai stream event), `usage: RunUsage | None` (child accumulated snapshot), `model: str | None` (set once per child).
  - `CliSubagentDemux.route(obj: dict) -> tuple[list[RoutedEvent], dict | None]` — `(events, passthrough)`; passthrough is the original object (nothing applied), a stripped copy (mixed message), or `None` (fully claimed).
  - `CliSubagentDemux.child_transcripts() -> dict[str, list]`.
  - `SPAWN_TOOL_NAMES = frozenset({"Agent", "Task"})`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_demux.py`:

```python
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
)

from marim_harness.subagents.cli_demux import SPAWN_TOOL_NAMES, CliSubagentDemux


def _spawn_obj(tid="t1", name="Agent", stype="Explore", desc="find it", prompt="do it",
               extra_blocks=(), parent=None):
    obj = {"type": "assistant", "message": {"model": "claude-haiku-4-5", "id": "msg_p1",
           "content": [
               {"type": "tool_use", "id": tid, "name": name,
                "input": {"description": desc, "subagent_type": stype, "prompt": prompt}},
               *extra_blocks,
           ]}}
    if parent:
        obj["parent_tool_use_id"] = parent
    return obj


def _child_text(parent="t1", text="4", mid="msg_c1", usage=None, model="claude-haiku-4-5"):
    msg = {"model": model, "id": mid,
           "content": [{"type": "text", "text": text}]}
    if usage is not None:
        msg["usage"] = usage
    return {"type": "assistant", "parent_tool_use_id": parent, "message": msg}


def test_unrelated_objects_pass_through_unchanged():
    d = CliSubagentDemux()
    obj = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
    events, passthrough = d.route(obj)
    assert events == [] and passthrough is obj
    events, passthrough = d.route({"type": "system", "subtype": "init"})
    assert events == [] and passthrough == {"type": "system", "subtype": "init"}
    events, passthrough = d.route({"type": "result", "result": "done"})
    assert events == [] and passthrough == {"type": "result", "result": "done"}


def test_spawn_tool_use_becomes_spawn_agent_call():
    d = CliSubagentDemux()
    events, passthrough = d.route(_spawn_obj())
    assert passthrough is None  # the spawn was the only block
    (r,) = events
    assert r.stream_id is None  # routed to the containing (main) stream
    assert isinstance(r.event, FunctionToolCallEvent)
    part = r.event.part
    assert part.tool_name == "spawn_agent" and part.tool_call_id == "t1"
    assert part.args_as_dict() == {
        "type": "Explore", "task": "do it", "description": "find it",
    }


def test_legacy_task_tool_name_also_claimed():
    assert "Task" in SPAWN_TOOL_NAMES
    d = CliSubagentDemux()
    events, passthrough = d.route(_spawn_obj(name="Task"))
    assert passthrough is None and events[0].event.part.tool_name == "spawn_agent"


def test_mixed_assistant_message_keeps_other_blocks():
    d = CliSubagentDemux()
    obj = _spawn_obj(extra_blocks=({"type": "text", "text": "spawning now"},))
    events, passthrough = d.route(obj)
    assert len(events) == 1
    assert passthrough is not None and passthrough is not obj  # a stripped copy
    kept = passthrough["message"]["content"]
    assert kept == [{"type": "text", "text": "spawning now"}]
    # original object was not mutated
    assert len(obj["message"]["content"]) == 2


def test_child_messages_route_to_child_stream():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, passthrough = d.route(_child_text())
    assert passthrough is None
    assert [r.stream_id for r in events] == ["t1", "t1"]
    assert isinstance(events[0].event, PartStartEvent)
    assert isinstance(events[1].event, PartDeltaEvent)
    assert isinstance(events[1].event.delta, TextPartDelta)
    assert events[1].event.delta.content_delta == "4"


def test_child_usage_accumulates_once_per_message_id():
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_creation_input_tokens": 10066}
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    # stream-json repeats the same message.usage on both events of one message
    events, _ = d.route(_child_text(mid="msg_c1", usage=usage))
    assert events[0].usage.output_tokens == 5
    assert events[0].usage.input_tokens == 10 + 10066  # cache-inclusive fold
    events, _ = d.route(_child_text(mid="msg_c1", usage=usage))
    assert events[0].usage.output_tokens == 5  # same message id — not re-counted
    events, _ = d.route(_child_text(mid="msg_c2", usage=usage))
    assert events[0].usage.output_tokens == 10  # a new message id accumulates


def test_child_model_surfaced_once():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, _ = d.route(_child_text(mid="m1"))
    assert events[0].model == "claude-haiku-4-5"
    assert events[1].model is None  # only the first event of the stream
    events, _ = d.route(_child_text(mid="m2"))
    assert events[0].model is None  # already sent


def test_async_launch_tool_result_is_suppressed():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, passthrough = d.route(
        {"type": "system", "subtype": "task_started", "tool_use_id": "t1"})
    assert events == [] and passthrough is None
    events, passthrough = d.route({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "Async agent launched successfully..."},
    ]}})
    assert events == [] and passthrough is None  # launch metadata: fully swallowed


def test_task_notification_finishes_spawn():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    d.route({"type": "system", "subtype": "task_started", "tool_use_id": "t1"})
    events, passthrough = d.route(
        {"type": "system", "subtype": "task_notification", "tool_use_id": "t1",
         "status": "completed", "summary": "4",
         "usage": {"total_tokens": 10086, "tool_uses": 0, "duration_ms": 2073}})
    assert passthrough is None
    (r,) = events
    assert r.stream_id is None and isinstance(r.event, FunctionToolResultEvent)
    assert r.event.part.tool_name == "spawn_agent"
    assert r.event.part.tool_call_id == "t1"
    assert r.event.part.content == "4" and r.event.part.outcome == "success"
    # a duplicate notification does not double-finish
    events, _ = d.route(
        {"type": "system", "subtype": "task_notification", "tool_use_id": "t1",
         "status": "completed", "summary": "4"})
    assert events == []


def test_task_notification_failed_status_marks_failed():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, _ = d.route(
        {"type": "system", "subtype": "task_notification", "tool_use_id": "t1",
         "status": "failed", "summary": ""})
    assert events[0].event.part.outcome == "failed"
    assert "failed" in events[0].event.part.content


def test_notification_for_unknown_spawn_is_dropped():
    d = CliSubagentDemux()
    events, passthrough = d.route(
        {"type": "system", "subtype": "task_notification", "tool_use_id": "nope",
         "status": "completed", "summary": "x"})
    assert events == [] and passthrough is None


def test_sync_tool_result_finishes_spawn():
    # Legacy CLIs: no task_started; the spawn's tool_result IS the report.
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    events, passthrough = d.route({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "the report"},
    ]}})
    assert passthrough is None
    (r,) = events
    assert isinstance(r.event, FunctionToolResultEvent)
    assert r.event.part.content == "the report"
    assert r.event.part.outcome == "success"


def test_unrelated_tool_result_passes_through():
    d = CliSubagentDemux()
    obj = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "other", "content": "x"},
    ]}}
    events, passthrough = d.route(obj)
    assert events == [] and passthrough is obj


def test_nested_spawn_routes_to_child_container():
    d = CliSubagentDemux()
    d.route(_spawn_obj(tid="t1"))
    # the child t1 itself spawns g1
    events, passthrough = d.route(_spawn_obj(tid="g1", parent="t1"))
    assert passthrough is None
    (r,) = events
    assert r.stream_id == "t1"  # the spawn call renders inside t1's pane
    assert r.event.part.tool_call_id == "g1"
    # the grandchild's own messages route to g1
    events, _ = d.route(_child_text(parent="g1", mid="msg_g"))
    assert events[0].stream_id == "g1"
    # t1's sidecar transcript carries the nested spawn call
    calls = [p for m in d.child_transcripts()["t1"] for p in m.parts]
    assert any(getattr(p, "tool_name", "") == "spawn_agent" for p in calls)


def test_child_transcripts_capture_messages():
    d = CliSubagentDemux()
    d.route(_spawn_obj())
    d.route(_child_text())
    transcripts = d.child_transcripts()
    assert "t1" in transcripts and len(transcripts["t1"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_cli_demux.py -v`
Expected: FAIL with `ModuleNotFoundError: marim_harness.subagents.cli_demux`.

- [ ] **Step 3: Implement the module**

Create `src/marim_harness/subagents/cli_demux.py`:

```python
"""Demultiplex Claude Code stream-json into per-sub-agent event streams.

A ``claude`` process (whether a ``backend: claude-cli`` spawn or the claude-cli
main-loop provider) can spawn its own sub-agents with Claude Code's Agent tool
(``Task`` on older CLIs). Its stream-json interleaves the children's traffic
with the parent's, tagging every child line with a top-level
``parent_tool_use_id``. This module turns that interleaved stream into the
exact shapes marim's TUI already renders for native spawns, so Claude-side
sub-agents get first-class cards in the sub-agents screen:

- an Agent/Task ``tool_use`` becomes a synthesized ``spawn_agent``
  ``FunctionToolCallEvent`` — the renderer's sinks claim those and build a live
  card keyed by the tool_call_id, which is exactly the id children are tagged
  with, so no renderer change is needed;
- child messages are translated per child (one ``CliStreamTranslator`` each)
  and routed to their card's stream, with a live accumulated ``RunUsage``
  (deduped by ``message.id`` — stream-json repeats the same usage on every
  event of one API message) and the child's reported model (surfaced once);
- completion is either a ``task_notification`` system event (async spawns —
  the spawn's immediate ``tool_result`` is launch metadata and is suppressed)
  or the spawn's real ``tool_result`` (legacy sync Task); both become a
  ``spawn_agent`` ``ToolReturnPart`` that settles the card.

Consumers call :meth:`CliSubagentDemux.route` per parsed object and get back
``(events, passthrough)``: the routed events to deliver, plus the object they
should still feed their own main-stream pipeline — the original when nothing
here applied, a stripped copy when spawn blocks were claimed out of a mixed
message, or ``None`` when fully claimed. Pure translation, no I/O; imported
lazily by its consumers (``cli_backend`` imports this module inside ``run()``
because this module imports from ``cli_backend``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RunUsage

from .cli_backend import CliStreamTranslator, _flatten_tool_result, synth_usage

# Claude Code's sub-agent spawn tool: "Agent" since CLI 2.1.x, "Task" before.
SPAWN_TOOL_NAMES = frozenset({"Agent", "Task"})


@dataclass
class RoutedEvent:
    """One translated event plus its destination stream.

    ``stream_id`` None means the *containing* stream (the caller's own main
    stream); otherwise it is the spawn's tool_use id — which is also the card's
    stream id. ``usage`` is the child stream's accumulated RunUsage snapshot
    (child events only) so the card can price live; ``model`` is set on the
    first event after the child reports which model it runs on."""

    stream_id: str | None
    event: object
    usage: RunUsage | None = None
    model: str | None = None


@dataclass
class _Spawn:
    """Lifecycle state for one Agent/Task spawn seen in the stream."""

    container: str | None  # stream its tool_use appeared in (None = main)
    async_started: bool = False  # task_started seen → its tool_result is launch noise
    finished: bool = False


class CliSubagentDemux:
    """Stateful router for one CLI process's stream (see module docstring)."""

    def __init__(self) -> None:
        self._spawns: dict[str, _Spawn] = {}
        self._translators: dict[str, CliStreamTranslator] = {}
        self._usage: dict[str, RunUsage] = {}
        self._usage_msgs: dict[str, set[str]] = {}
        self._model_sent: set[str] = set()

    def route(self, obj: dict) -> tuple[list[RoutedEvent], dict | None]:
        kind = obj.get("type")
        if kind == "system":
            return self._system(obj)
        parent = obj.get("parent_tool_use_id")
        if parent and kind in ("assistant", "user"):
            return self._child(obj, str(parent)), None
        events: list[RoutedEvent] = []
        if kind == "assistant":
            return events, self._claim_spawn_calls(obj, None, events)
        if kind == "user":
            return events, self._claim_spawn_results(obj, None, events)
        return [], obj

    # -- system lifecycle events -------------------------------------------

    def _system(self, obj: dict) -> tuple[list[RoutedEvent], dict | None]:
        subtype = obj.get("subtype")
        if subtype == "task_started":
            tid = str(obj.get("tool_use_id") or "")
            if tid:
                self._spawns.setdefault(tid, _Spawn(container=None)).async_started = True
            return [], None
        if subtype == "task_notification":
            tid = str(obj.get("tool_use_id") or "")
            status = str(obj.get("status") or "completed")
            content = str(obj.get("summary") or "") or f"(sub-agent {status})"
            return self._finish(tid, content, failed=status != "completed"), None
        if subtype == "task_updated":
            return [], None  # progress patches; the notification carries the report
        return [], obj

    def _finish(self, tid: str, content: str, *, failed: bool) -> list[RoutedEvent]:
        spawn = self._spawns.get(tid)
        if spawn is None or spawn.finished:
            return []
        spawn.finished = True
        part = ToolReturnPart(
            tool_name="spawn_agent",
            content=content,
            tool_call_id=tid,
            timestamp=datetime.now(tz=timezone.utc),
            outcome="failed" if failed else "success",
        )
        if spawn.container is not None:
            self._translator(spawn.container).record_return(part)
        return [self._routed(spawn.container, FunctionToolResultEvent(part=part))]

    # -- child streams -------------------------------------------------------

    def _child(self, obj: dict, sid: str) -> list[RoutedEvent]:
        # Defensive: a child line for a spawn whose tool_use we never saw still
        # gets a stream (its events will just find no card and no-op in the UI).
        self._spawns.setdefault(sid, _Spawn(container=None))
        events: list[RoutedEvent] = []
        model: str | None = None
        remainder: dict | None
        if obj.get("type") == "assistant":
            self._accumulate_usage(sid, obj)
            model = self._peek_model(sid, obj)
            remainder = self._claim_spawn_calls(obj, sid, events)
        else:
            remainder = self._claim_spawn_results(obj, sid, events)
        if remainder is not None:
            usage = self._usage.get(sid)
            for ev in self._translator(sid).translate(remainder):
                events.append(RoutedEvent(sid, ev, usage=usage, model=model))
                if model:
                    self._model_sent.add(sid)
                model = None
        return events

    # -- spawn claim helpers (shared by main and child streams) --------------

    def _claim_spawn_calls(
        self, obj: dict, container: str | None, out: list[RoutedEvent]
    ) -> dict | None:
        """Claim Agent/Task tool_use blocks out of an assistant message:
        register the spawn, synthesize its spawn_agent call into ``out``, and
        return the message with those blocks removed (the original when none
        matched; None when nothing else remains). Never mutates ``obj``."""
        msg = obj.get("message") or {}
        blocks = msg.get("content") or []
        kept = []
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in SPAWN_TOOL_NAMES
            ):
                out.extend(self._spawn_call(block, container))
            else:
                kept.append(block)
        if len(kept) == len(blocks):
            return obj
        if not kept:
            return None
        return {**obj, "message": {**msg, "content": kept}}

    def _spawn_call(self, block: dict, container: str | None) -> list[RoutedEvent]:
        tid = str(block.get("id") or "")
        if not tid:
            return []
        inp = block.get("input") or {}
        spawn = self._spawns.setdefault(tid, _Spawn(container=container))
        spawn.container = container
        args = {
            "type": str(inp.get("subagent_type") or "agent"),
            "task": str(inp.get("prompt") or inp.get("description") or ""),
            "description": str(inp.get("description") or ""),
        }
        part = ToolCallPart(tool_name="spawn_agent", args=args, tool_call_id=tid)
        if container is not None:
            self._translator(container).record_call(part)
        return [self._routed(container, FunctionToolCallEvent(part=part))]

    def _claim_spawn_results(
        self, obj: dict, container: str | None, out: list[RoutedEvent]
    ) -> dict | None:
        """Claim tool_result blocks answering a known spawn. An async spawn's
        immediate tool_result is launch metadata — swallowed (the
        task_notification settles the card). A sync (legacy Task) spawn's
        tool_result IS the report — converted to the spawn_agent return."""
        msg = obj.get("message") or {}
        blocks = msg.get("content") or []
        kept = []
        for block in blocks:
            tid = (
                str(block.get("tool_use_id") or "")
                if isinstance(block, dict) and block.get("type") == "tool_result"
                else ""
            )
            spawn = self._spawns.get(tid) if tid else None
            if spawn is None:
                kept.append(block)
                continue
            if spawn.async_started:
                continue  # launch metadata — drop
            out.extend(self._finish(
                tid,
                _flatten_tool_result(block.get("content")),
                failed=bool(block.get("is_error")),
            ))
        if len(kept) == len(blocks):
            return obj
        if not kept:
            return None
        return {**obj, "message": {**msg, "content": kept}}

    # -- per-child bookkeeping ------------------------------------------------

    def _accumulate_usage(self, sid: str, obj: dict) -> None:
        """Fold one assistant message's usage into the child's running total.
        stream-json repeats the same ``message.usage`` on every event of one
        API message, so dedupe by message id; ``synth_usage`` folds the cache
        buckets into ``input_tokens`` per the harness convention."""
        msg = obj.get("message") or {}
        u, mid = msg.get("usage"), str(msg.get("id") or "")
        if not u or not mid or mid in self._usage_msgs.setdefault(sid, set()):
            return
        self._usage_msgs[sid].add(mid)
        add = synth_usage(u, 1)
        total = self._usage.get(sid, RunUsage())
        self._usage[sid] = RunUsage(
            input_tokens=total.input_tokens + add.input_tokens,
            output_tokens=total.output_tokens + add.output_tokens,
            cache_read_tokens=total.cache_read_tokens + add.cache_read_tokens,
            cache_write_tokens=total.cache_write_tokens + add.cache_write_tokens,
            requests=total.requests + add.requests,
        )

    def _peek_model(self, sid: str, obj: dict) -> str | None:
        if sid in self._model_sent:
            return None
        model = (obj.get("message") or {}).get("model")
        return str(model) if model else None

    def _translator(self, sid: str) -> CliStreamTranslator:
        return self._translators.setdefault(sid, CliStreamTranslator())

    def _routed(self, sid: str | None, event: object) -> RoutedEvent:
        usage = self._usage.get(sid) if sid is not None else None
        return RoutedEvent(sid, event, usage=usage)

    def child_transcripts(self) -> dict[str, list]:
        """Each child stream's transcript (pydantic-ai messages) keyed by its
        stream id, for sidecar persistence — so the sub-agents screen can
        replay a Claude-side child after a session resume. Empty streams are
        omitted."""
        return {
            sid: t.transcript()
            for sid, t in self._translators.items()
            if t.transcript()
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_cli_demux.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Lint + typecheck the new module**

Run: `uv run ruff check src/marim_harness/subagents/cli_demux.py tests/test_cli_demux.py && uv run pyright`
Expected: clean. (`_flatten_tool_result` is a same-package private import — allowed.)

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/subagents/cli_demux.py tests/test_cli_demux.py
git commit -m "feat(subagents): CliSubagentDemux — route Claude-side sub-agent streams"
```

---

### Task 4: `ClaudeCliRunner` — demux glue, multi-result, child transcripts

**Files:**
- Modify: `src/marim_harness/subagents/cli_backend.py` (`CliResult`, `ClaudeCliRunner.run`, new `_deliver`)
- Test: `tests/test_subagents_cli.py`

**Interfaces:**
- Consumes: `CliSubagentDemux.route` / `.child_transcripts()` (Task 3), `record_call`/`record_return` (Task 1), `sum_result_usages` (Task 2).
- Produces: `CliResult.child_transcripts: dict[str, list]` (Task 5 persists it). `on_event` now also fires with child stream ids and a non-None usage for child events.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagents_cli.py`. First add a second fake-CLI script next to `_FAKE_CLI` that mirrors the verified async-agent stream shape:

```python
_FAKE_CLI_AGENT = '''#!{python}
import json, sys
lines = [
    {{"type": "system", "subtype": "init", "model": "claude-opus-4-8"}},
    {{"type": "assistant", "message": {{"model": "claude-opus-4-8", "id": "msg_p1",
        "content": [
        {{"type": "tool_use", "id": "tsub", "name": "Agent",
          "input": {{"description": "Answer 2+2", "subagent_type": "Explore",
                     "prompt": "What is 2+2?"}}}},
    ]}}}},
    {{"type": "system", "subtype": "task_started", "task_id": "af41",
      "tool_use_id": "tsub", "description": "Answer 2+2",
      "subagent_type": "Explore", "prompt": "What is 2+2?"}},
    {{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": "tsub",
          "content": [{{"type": "text", "text": "Async agent launched..."}}]}},
    ]}}}},
    {{"type": "assistant", "parent_tool_use_id": "tsub",
      "message": {{"model": "claude-haiku-4-5", "id": "msg_c1",
        "usage": {{"input_tokens": 10, "output_tokens": 5,
                   "cache_creation_input_tokens": 10066}},
        "content": [{{"type": "text", "text": "4"}}]}}}},
    {{"type": "system", "subtype": "task_updated", "task_id": "af41",
      "patch": {{"status": "completed"}}}},
    {{"type": "system", "subtype": "task_notification", "task_id": "af41",
      "tool_use_id": "tsub", "status": "completed", "summary": "4",
      "usage": {{"total_tokens": 10086, "tool_uses": 0, "duration_ms": 2073}}}},
    {{"type": "result", "subtype": "success",
      "result": "Agent spawned. Waiting for it to complete...", "num_turns": 2,
      "total_cost_usd": 0.04,
      "usage": {{"input_tokens": 18, "output_tokens": 1083}}}},
    {{"type": "assistant", "message": {{"model": "claude-opus-4-8", "id": "msg_p2",
        "content": [{{"type": "text", "text": "Four."}}]}}}},
    {{"type": "result", "subtype": "success", "result": "Four.", "num_turns": 1,
      "total_cost_usd": 0.05,
      "usage": {{"input_tokens": 10, "output_tokens": 48}}}},
]
for o in lines:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _make_fake_cli_agent(tmp_path) -> str:
    p = tmp_path / "fake_claude_agent.py"
    p.write_text(_FAKE_CLI_AGENT.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_runner_demuxes_claude_side_subagents(tmp_path):
    binary = _make_fake_cli_agent(tmp_path)
    events: list[tuple[str, object, object]] = []
    models: list[tuple[str, str]] = []

    async def on_event(stream_id, event, usage):
        events.append((stream_id, event, usage))

    async def on_model(stream_id, model):
        models.append((stream_id, model))

    runner = ClaudeCliRunner(on_event, None, on_model)
    result = await runner.run(
        binary=binary, prompt="t", system_prompt="s", cwd=str(tmp_path),
        allow_gated=False, allowed_tools=[], model=None, stream_id="parent",
    )
    # Final report is the LAST result event's text; usage sums both segments
    # and keeps the last (cumulative) cost.
    assert result.output == "Four."
    assert result.usage.output_tokens == 1083 + 48
    from marim_harness.usage import COST_DETAIL_KEY
    assert result.usage.details[COST_DETAIL_KEY] == 50_000  # $0.05 in micro-USD

    # The spawn surfaced on the PARENT stream as a spawn_agent call…
    spawn_calls = [
        (sid, ev) for sid, ev, _ in events
        if isinstance(ev, FunctionToolCallEvent) and ev.part.tool_name == "spawn_agent"
    ]
    assert spawn_calls and spawn_calls[0][0] == "parent"
    assert spawn_calls[0][1].part.tool_call_id == "tsub"
    # …child text streamed on the CHILD stream, carrying live usage…
    child_events = [(sid, ev, u) for sid, ev, u in events if sid == "tsub"]
    assert child_events
    assert any(u is not None and u.output_tokens == 5 for _, _, u in child_events)
    # …and the notification settled the card with the summary.
    finishes = [
        ev for sid, ev, _ in events
        if sid == "parent" and isinstance(ev, FunctionToolResultEvent)
        and ev.part.tool_name == "spawn_agent"
    ]
    assert finishes and finishes[0].part.content == "4"

    # The child's model reached the card; the run's own model still surfaced.
    assert ("tsub", "claude-haiku-4-5") in models
    assert any(m == "claude-opus-4-8" for _, m in models)

    # Parent transcript pairs the synthesized spawn call with its return;
    # the child transcript is captured for sidecar persistence.
    parent_parts = [p for m in result.transcript for p in m.parts]
    assert any(getattr(p, "tool_name", "") == "spawn_agent"
               and isinstance(p, ToolCallPart) for p in parent_parts)
    assert any(getattr(p, "tool_name", "") == "spawn_agent"
               and isinstance(p, ToolReturnPart) for p in parent_parts)
    assert "tsub" in result.child_transcripts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py -k demuxes -v`
Expected: FAIL (`result.output` is `"Agent spawned. Waiting..."`? No — last result already wins today; the first hard failure is `result.child_transcripts` AttributeError and no `spawn_agent` events; usage assert fails at `output_tokens == 1131` since only the last result is counted today).

- [ ] **Step 3: Implement**

In `cli_backend.py`:

1. `CliResult` gains a field:

```python
@dataclass
class CliResult:
    """A finished CLI spawn, shaped like the bits of a Pydantic AI run result the
    spawn lifecycle consumes: the final report text, the run's usage, and the
    transcripts — the parent's, plus one per Claude-side child sub-agent (keyed
    by the child's stream id) for sidecar persistence."""

    output: str
    usage: RunUsage
    transcript: list = field(default_factory=list)
    child_transcripts: dict = field(default_factory=dict)
```

2. Rework the body of `ClaudeCliRunner.run` between `translator = CliStreamTranslator()` and the `return`:

```python
            from .cli_demux import CliSubagentDemux  # lazy: cli_demux imports us

            translator = CliStreamTranslator()
            demux = CliSubagentDemux()
            output = ""
            results: list[dict] = []
            model_sent = False
            assert proc.stdout is not None
            async for raw in _iter_ndjson_lines(proc.stdout):
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # non-JSON noise on stdout — skip
                # Claude-side sub-agent traffic (Agent/Task spawns, their child
                # streams, task lifecycle events) is demuxed into per-card
                # streams; whatever remains is this spawn's own main stream.
                routed, remainder = demux.route(obj)
                for r in routed:
                    await self._deliver(r, translator, stream_id)
                if remainder is None:
                    continue
                obj = remainder
                if not model_sent:
                    # (existing model probe block — unchanged)
                    ...
                if obj.get("type") == "result":
                    # One -p process can emit several results (an async
                    # sub-agent's completion notification re-invokes the main
                    # agent). The LAST result's text is the final report;
                    # usage folds across all of them (sum_result_usages).
                    results.append(obj)
                    output = obj.get("result", "") or ""
                    continue
                for event in translator.translate(obj):
                    if self._on_event is not None and stream_id:
                        await self._on_event(stream_id, event, None)
            stderr_bytes = await stderr_task if stderr_task is not None else b""
            stderr_task = None  # consumed — don't cancel it in finally
            code = await proc.wait()
            if not results:
                detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {code}"
                raise CliRunError(f"claude produced no result ({detail})")
            return CliResult(
                output=output,
                usage=synth_usage(*sum_result_usages(results)),
                transcript=translator.transcript(),
                child_transcripts=demux.child_transcripts(),
            )
```

(The `usage = RunUsage()` / `result_seen` locals are removed; keep the existing `model_sent` probe block verbatim.)

3. Add `_deliver` to `ClaudeCliRunner` (after `run`); add `from typing import TYPE_CHECKING` guard importing `RoutedEvent` for the annotation:

```python
    async def _deliver(
        self, routed: "RoutedEvent", translator: CliStreamTranslator, stream_id: str
    ) -> None:
        """Forward one demux-routed event. Main-routed events (the synthesized
        spawn_agent call/return for a Claude-side spawn) go to this spawn's own
        stream and are recorded into the parent transcript, so the persisted
        sidecar replays the nested card. Child-routed events go to the child's
        stream with its live usage and (once) its reported model."""
        if routed.stream_id is None:
            part = getattr(routed.event, "part", None)
            if isinstance(part, ToolCallPart):
                translator.record_call(part)
            elif isinstance(part, ToolReturnPart):
                translator.record_return(part)
            if self._on_event is not None and stream_id:
                await self._on_event(stream_id, routed.event, None)
            return
        if routed.model and self._on_model is not None:
            await self._on_model(routed.stream_id, routed.model)
        if self._on_event is not None:
            await self._on_event(routed.stream_id, routed.event, routed.usage)
```

With the `TYPE_CHECKING` import at the top of the module:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli_demux import RoutedEvent
```

- [ ] **Step 4: Run tests to verify they pass (whole file — the existing runner tests must stay green)**

Run: `uv run pytest --no-cov tests/test_subagents_cli.py tests/test_subagent_cli_spawn.py -v`
Expected: ALL PASS. (`test_runner_raises_when_no_result` still passes — `results` empty → `CliRunError`.)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/subagents/cli_backend.py tests/test_subagents_cli.py
git commit -m "feat(cli-backend): demux Claude-side sub-agents into per-card streams"
```

---

### Task 5: Persist child transcripts (runner lifecycle)

**Files:**
- Modify: `src/marim_harness/subagents/runner.py:863` (in `_execute_cli_spawn`, after `self._save_transcript(stream_id, result.transcript)`)
- Test: `tests/test_subagent_cli_spawn.py`

**Interfaces:**
- Consumes: `CliResult.child_transcripts` (Task 4).
- Produces: each Claude-side child's sidecar saved under its own stream id, so `SubAgentsViewer._load_transcript` replays it on resume with no changes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagent_cli_spawn.py` (reuses `_write_cli_agent`, `_dummy_model`, `_make_deps`, `_make_harness` already imported there):

```python
_FAKE_CLI_CHILD = '''#!{python}
import json, sys
for o in [
    {{"type": "assistant", "message": {{"id": "m1", "content": [
        {{"type": "tool_use", "id": "tsub", "name": "Agent",
          "input": {{"description": "d", "subagent_type": "Explore", "prompt": "p"}}}},
    ]}}}},
    {{"type": "system", "subtype": "task_started", "tool_use_id": "tsub"}},
    {{"type": "assistant", "parent_tool_use_id": "tsub",
      "message": {{"id": "m2", "content": [{{"type": "text", "text": "4"}}]}}}},
    {{"type": "system", "subtype": "task_notification", "tool_use_id": "tsub",
      "status": "completed", "summary": "4"}},
    {{"type": "result", "subtype": "success", "result": "Done", "num_turns": 1,
      "usage": {{"input_tokens": 1, "output_tokens": 1}}}},
]:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _fake_cli_child(tmp_path: Path) -> str:
    p = tmp_path / "fake_claude_child.py"
    p.write_text(_FAKE_CLI_CHILD.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_cli_backend_persists_child_transcripts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli_child(tmp_path))
    _write_cli_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents

    saved: list[str] = []
    real_save = runner._save_transcript
    monkeypatch.setattr(
        runner, "_save_transcript",
        lambda sid, msgs: (saved.append(sid), real_save(sid, msgs)),
    )
    out = await runner.run("cli-worker", "do the thing", stream_id="s1")
    assert "Done" in out
    assert "s1" in saved and "tsub" in saved  # parent sidecar AND the child's
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_subagent_cli_spawn.py -k persists_child -v`
Expected: FAIL — `"tsub" in saved` is False.

- [ ] **Step 3: Implement**

In `runner.py`'s `_execute_cli_spawn`, right after `self._save_transcript(stream_id, result.transcript)`:

```python
        # Claude-side sub-agents (the CLI's own Agent/Task spawns) each get a
        # sidecar under their stream id — the same id their live card streamed
        # under — so the sub-agents screen can replay them after a resume.
        for child_id, msgs in result.child_transcripts.items():
            self._save_transcript(child_id, msgs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_subagent_cli_spawn.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/subagents/runner.py tests/test_subagent_cli_spawn.py
git commit -m "feat(subagents): persist Claude-side child transcripts as sidecars"
```

---

### Task 6: `consume_cli_stream` — headless child filtering + multi-result survival

**Files:**
- Modify: `src/marim_harness/config/claude_cli_model.py` (`consume_cli_stream`, `_ACTIVITY_ARG`, `ClaudeCliStreamedResponse._get_event_iterator` finalization)
- Test: `tests/test_claude_cli_model.py`

**Interfaces:**
- Consumes: `sum_result_usages` (Task 2).
- Produces: `consume_cli_stream` now (a) skips objects tagged `parent_tool_use_id` and `task_*` system events, (b) does NOT return at the first `result` — it yields a `DoneChunk` per result (usage folded so far, cumulative cost) and keeps reading to EOF, so the CLI process is no longer killed while an async sub-agent runs; last DoneChunk wins in both consumers.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_cli_model.py` (the file already has `_collect`/`_fake_objs` helpers — reuse them):

```python
@pytest.mark.anyio
async def test_consume_skips_subagent_child_traffic():
    chunks = await _collect([
        {"type": "assistant", "parent_tool_use_id": "t1",
         "message": {"content": [{"type": "text", "text": "CHILD TEXT"}]}},
        {"type": "system", "subtype": "task_started", "tool_use_id": "t1"},
        {"type": "system", "subtype": "task_notification", "tool_use_id": "t1",
         "status": "completed", "summary": "4"},
        {"type": "result", "subtype": "success", "result": "ok", "num_turns": 1,
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    texts = [c.delta for c in chunks if isinstance(c, TextChunk)]
    assert texts == []  # a child's text never leaks into the main response


@pytest.mark.anyio
async def test_consume_survives_multiple_results_and_folds_usage():
    chunks = await _collect([
        {"type": "result", "subtype": "success", "result": "waiting",
         "num_turns": 2, "total_cost_usd": 0.04,
         "usage": {"input_tokens": 18, "output_tokens": 1083}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Four."}]}},
        {"type": "result", "subtype": "success", "result": "Four.",
         "num_turns": 1, "total_cost_usd": 0.05,
         "usage": {"input_tokens": 10, "output_tokens": 48}},
    ])
    # text AFTER the first result still streams (the generator no longer
    # returns early, which used to kill the CLI mid-async-sub-agent)
    assert any(isinstance(c, TextChunk) and c.delta == "Four." for c in chunks)
    dones = [c for c in chunks if isinstance(c, DoneChunk)]
    assert len(dones) == 2 and all(d.complete for d in dones)
    assert dones[-1].usage.output_tokens == 1083 + 48
    from marim_harness.usage import COST_DETAIL_KEY
    assert dones[-1].usage.details[COST_DETAIL_KEY] == 50_000


def test_activity_line_names_agent_spawns():
    assert format_activity_line(
        "Agent", {"description": "Answer 2+2", "subagent_type": "Explore"}
    ) == "▸ Agent Answer 2+2"
```

(Extend the module's existing imports with whatever of `TextChunk, DoneChunk, format_activity_line` isn't imported yet.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_claude_cli_model.py -k "skips_subagent or survives_multiple or names_agent" -v`
Expected: FAIL — child text leaks; only one DoneChunk with unsummed usage; activity line lacks the description.

- [ ] **Step 3: Implement**

In `claude_cli_model.py`:

1. `_ACTIVITY_ARG` — add spawn tools:

```python
    "Agent": "description",
    "Task": "description",
```

2. Rewrite `consume_cli_stream`'s loop (docstring: note the child filter and the multi-result behavior — the old early `return` closed the generator, whose `finally` in `spawn_cli_objects` killed the CLI while an async sub-agent was still running):

```python
    from ..subagents.cli_backend import sum_result_usages

    session_id: str | None = None
    results: list[dict] = []
    async for obj in objs:
        if obj.get("parent_tool_use_id"):
            # Sub-agent-internal traffic. With a UI the demux tee (see
            # ClaudeCliStreamedResponse) consumes it before we ever see it;
            # headless it is dropped so a child's prose never pollutes the
            # main response text.
            continue
        kind = obj.get("type")
        if kind == "system":
            if obj.get("subtype") in ("task_started", "task_updated", "task_notification"):
                continue  # sub-agent lifecycle noise (the demux path renders it)
            session_id = session_id or obj.get("session_id")
        elif kind == "assistant":
            # (existing text/tool_use block loop — unchanged)
            ...
        elif kind == "user":
            # (existing tool_result block loop — unchanged)
            ...
        elif kind == "result":
            session_id = session_id or obj.get("session_id")
            results.append(obj)
            summed, _turns, cost = sum_result_usages(results)
            # Do NOT return: an async sub-agent's completion re-invokes the
            # main agent, so more turns (and another result) may follow.
            # Consumers keep the LAST DoneChunk.
            yield DoneChunk(
                session_id=session_id,
                usage=request_usage_from_cli(summed, cost),
                complete=True,
            )
    if not results:
        yield DoneChunk(session_id=session_id, usage=RequestUsage(), complete=False)
```

3. In `ClaudeCliStreamedResponse._get_event_iterator`, defer finalization so a mid-stream `DoneChunk` can't mark the stream finished while events still follow — replace the `DoneChunk` branch and the post-loop check with:

```python
            elif isinstance(chunk, DoneChunk):
                done = chunk  # last one wins (multi-result runs)
        if done is None or not done.complete:
            raise CliModelError("claude produced no result (crash or bad output).")
        self._usage = done.usage
        if done.session_id and self._set_session is not None:
            self._set_session(done.session_id)
        self._finished = True
```

(`request()` already `continue`s on `DoneChunk`, so last-wins falls out for free there.)

- [ ] **Step 4: Run the whole model test file**

Run: `uv run pytest --no-cov tests/test_claude_cli_model.py -v`
Expected: ALL PASS — including `test_consume_marks_incomplete_when_no_result` (no results → the trailing incomplete DoneChunk) and `test_request_stream_pushes_tool_cards_and_keeps_response_text_only`.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/claude_cli_model.py tests/test_claude_cli_model.py
git commit -m "fix(claude-cli): survive multi-result streams; keep sub-agent traffic out of main text"
```

---

### Task 7: Main-loop UI wiring — demux tee + harness binding

**Files:**
- Modify: `src/marim_harness/config/claude_cli_model.py` (`ClaudeCliModel.__init__`, `request_stream`, `ClaudeCliStreamedResponse`)
- Modify: `src/marim_harness/runtime/harness.py:473-482` (`_wire_cli_model`)
- Test: `tests/test_claude_cli_model.py`, `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: `CliSubagentDemux` (Task 3), `Deps.ui.on_subagent_event` / `on_subagent_model` (existing UIHooks, already wired by the TUI's `bind_ui` call).
- Produces: `ClaudeCliModel.on_subagent: Callable[[str, object, object], Awaitable[None]] | None` and `ClaudeCliModel.on_subagent_model: Callable[[str, str], Awaitable[None]] | None`, late-bound by `Harness._wire_cli_model` exactly like `on_activity`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_claude_cli_model.py`:

```python
@pytest.mark.anyio
async def test_request_stream_routes_claude_subagents_to_side_channels():
    from marim_harness.config.claude_cli_model import ClaudeCliModel

    model = ClaudeCliModel("claude-opus-4-8")
    activity: list = []
    sub_events: list[tuple[str, object, object]] = []
    sub_models: list[tuple[str, str]] = []

    async def on_activity(events):
        activity.extend(events)

    async def on_subagent(sid, event, usage):
        sub_events.append((sid, event, usage))

    async def on_subagent_model(sid, m):
        sub_models.append((sid, m))

    model.on_activity = on_activity
    model.on_subagent = on_subagent
    model.on_subagent_model = on_subagent_model
    model.spawn = _fake_objs([
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "tsub", "name": "Agent",
             "input": {"description": "d", "subagent_type": "Explore",
                       "prompt": "p"}},
        ]}},
        {"type": "system", "subtype": "task_started", "tool_use_id": "tsub"},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tsub",
             "content": "Async agent launched..."}]}},
        {"type": "assistant", "parent_tool_use_id": "tsub",
         "message": {"id": "m2", "model": "claude-haiku-4-5",
                     "usage": {"input_tokens": 3, "output_tokens": 2},
                     "content": [{"type": "text", "text": "4"}]}},
        {"type": "system", "subtype": "task_notification", "tool_use_id": "tsub",
         "status": "completed", "summary": "4"},
        {"type": "assistant", "message": {"id": "m3", "content": [
            {"type": "text", "text": "Four."}]}},
        {"type": "result", "subtype": "success", "result": "Four.", "num_turns": 1,
         "session_id": "sess-1", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ])
    text = await _stream_text(model)  # see note below

    # spawn call + spawn return went to the MAIN transcript side-channel
    spawn_names = [
        e.part.tool_name for e in activity if hasattr(e, "part")
    ]
    assert spawn_names.count("spawn_agent") == 2
    # child events went to the sub-agent channel, tagged with usage + model
    assert sub_events and all(sid == "tsub" for sid, _, _ in sub_events)
    assert any(u is not None and u.output_tokens == 2 for _, _, u in sub_events)
    assert ("tsub", "claude-haiku-4-5") in sub_models
    # the main-stream prose survives; the child's "4" never entered the text
    assert text == "Four."
```

For `_stream_text`, follow the exact pattern the existing `test_request_stream_pushes_tool_cards_and_keeps_response_text_only` uses to drive `model.request_stream(...)` and collect the response text — copy its invocation (a `ModelRequestParameters()` + iterating the stream context) into a small local helper if one doesn't already exist in the file.

And extend `tests/test_bootstrap.py`, in the existing wiring test around lines 337-344 (which asserts `harness.current_model.on_activity is _on_cli_activity`), add the two parallel assertions:

```python
    assert harness.current_model.on_subagent is None          # before bind_ui
    ...
    assert harness.current_model.on_subagent is _on_subagent_event   # after
    assert harness.current_model.on_subagent_model is _on_subagent_model
```

using callbacks passed to `bind_ui(on_subagent_event=..., on_subagent_model=...)` the same way that test passes `on_cli_activity=_on_cli_activity`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_claude_cli_model.py -k routes_claude_subagents -v`
Expected: FAIL with `AttributeError: 'ClaudeCliModel' object has no attribute 'on_subagent'`.

- [ ] **Step 3: Implement**

In `claude_cli_model.py`:

1. `ClaudeCliModel.__init__` — two new late-bound side-channels next to `on_activity` (mirror its comment style):

```python
        # Late-bound by bind_ui (TUI only): routes a Claude-side sub-agent's
        # translated events to the sub-agents screen (on_subagent ≙
        # Deps.ui.on_subagent_event) and relabels its card with the model the
        # child reports (on_subagent_model). None headless — the stream filter
        # in consume_cli_stream then simply drops child traffic.
        self.on_subagent: Callable[[str, object, object], Awaitable[None]] | None = None
        self.on_subagent_model: Callable[[str, str], Awaitable[None]] | None = None
```

2. `request_stream` passes them into the dataclass:

```python
            _on_subagent=self.on_subagent,
            _on_subagent_model=self.on_subagent_model,
```

3. `ClaudeCliStreamedResponse` gains the fields and a demux tee:

```python
    _on_subagent: Callable[[str, object, object], Awaitable[None]] | None = None
    _on_subagent_model: Callable[[str, str], Awaitable[None]] | None = None
```

```python
    async def _demuxed_objs(self) -> AsyncIterator[dict]:
        """Tee the raw stream through a CliSubagentDemux: Claude-side sub-agent
        traffic is delivered out-of-band (the synthesized spawn_agent call/
        return via _on_activity — the top-level sink claims those and builds
        the live card — and child events via _on_subagent, keyed by the spawn's
        tool_use id); everything else flows on to the chunk pipeline."""
        from ..subagents.cli_demux import CliSubagentDemux

        demux = CliSubagentDemux()
        assert self._objs is not None
        async for obj in self._objs:
            routed, remainder = demux.route(obj)
            for r in routed:
                if r.stream_id is None:
                    if self._on_activity is not None:
                        await self._on_activity([r.event])
                elif self._on_subagent is not None:
                    if r.model and self._on_subagent_model is not None:
                        await self._on_subagent_model(r.stream_id, r.model)
                    await self._on_subagent(r.stream_id, r.event, r.usage)
            if remainder is not None:
                yield remainder
```

4. In `_get_event_iterator`, choose the source once at the top (the demux is active only when the sub-agent channel is wired; headless keeps the cheap filter-only path):

```python
        objs = self._demuxed_objs() if self._on_subagent is not None else self._objs
        ...
        async for chunk in consume_cli_stream(objs):
```

In `harness.py`'s `_wire_cli_model`, after `model.on_activity = ...`:

```python
            model.on_subagent = self.deps.ui.on_subagent_event
            model.on_subagent_model = self.deps.ui.on_subagent_model
```

Update `_wire_cli_model`'s docstring: "…the TUI tool-card side-channel, and the sub-agents-screen side-channels for Claude's own Agent/Task spawns."

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_claude_cli_model.py tests/test_bootstrap.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/config/claude_cli_model.py src/marim_harness/runtime/harness.py \
        tests/test_claude_cli_model.py tests/test_bootstrap.py
git commit -m "feat(claude-cli): stream Claude's own sub-agents into the sub-agents screen"
```

---

### Task 8: Renderer round-trip test, docs, full verification

**Files:**
- Test: `tests/test_subagents_screen.py`
- Modify: `CLAUDE.md` (claude-cli provider sentence), `src/marim_harness/subagents/cli_backend.py` (module docstring), `src/marim_harness/config/claude_cli_model.py` (module docstring)

**Interfaces:**
- Consumes: everything above. No new interfaces.

- [ ] **Step 1: Write the renderer round-trip test**

Append to `tests/test_subagents_screen.py`, using the file's `_app(tmp_path)` fixture. This proves the synthesized events drive the *real* renderer end to end — card creation via the activity channel, child streaming via the sub-agent channel, settle via the synthesized return:

```python
@pytest.mark.anyio
async def test_claude_cli_spawn_events_drive_a_native_card(tmp_path):
    from datetime import datetime, timezone

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        PartDeltaEvent,
        PartStartEvent,
        TextPart,
        TextPartDelta,
        ToolCallPart,
        ToolReturnPart,
    )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        call = FunctionToolCallEvent(part=ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "Explore", "task": "What is 2+2?", "description": "math"},
            tool_call_id="tsub",
        ))
        await app.stream.on_cli_activity([call])
        await pilot.pause()
        assert len(app.stream.subagents) == 1
        card = app.stream.subagents[0]
        assert card.stream_id == "tsub" and card.agent_type == "Explore"

        await app.stream.on_subagent_event(
            "tsub", PartStartEvent(index=0, part=TextPart(content="")))
        await app.stream.on_subagent_event(
            "tsub", PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="4")))
        await pilot.pause()

        ret = FunctionToolResultEvent(part=ToolReturnPart(
            tool_name="spawn_agent", content="4", tool_call_id="tsub",
            timestamp=datetime.now(tz=timezone.utc), outcome="success",
        ))
        await app.stream.on_cli_activity([ret])
        await pilot.pause()
        assert card.status == "done"
```

Run: `uv run pytest --no-cov tests/test_subagents_screen.py -k claude_cli_spawn -v`
Expected: PASS with **no renderer changes** — if it fails, the demux output shape is wrong, not the renderer; fix the demux.

- [ ] **Step 2: Update docs**

In `CLAUDE.md`, extend the claude-cli provider sentence (the one ending "so marim's own tools/approval/LSP/MCP do not apply in that provider.") with:

```
Claude's own Agent/Task sub-agents, however, are demuxed out of the stream
(`subagents/cli_demux.py`) and rendered as first-class cards in the sub-agents
screen, for both the main-loop provider and `backend: claude-cli` spawns.
```

Update the `cli_backend.py` module docstring's helper list to mention the demux hand-off, and the `claude_cli_model.py` module docstring to mention the sub-agent side-channels.

- [ ] **Step 3: Full CI order locally**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all three clean. Fix anything that surfaces before committing.

- [ ] **Step 4: Manual smoke (optional but recommended, needs a `claude` login)**

Run marim with `MARIM_PROVIDER=claude-cli`, ask: *"Use your Agent tool to spawn an Explore subagent that answers: what is 2+2."* Expect: a card appears in the main log and Ctrl+X screen, streams the child's thinking/text, and settles with "4" when the notification lands; the turn's final text arrives after.

- [ ] **Step 5: Commit**

```bash
git add tests/test_subagents_screen.py CLAUDE.md \
        src/marim_harness/subagents/cli_backend.py \
        src/marim_harness/config/claude_cli_model.py
git commit -m "test(tui): claude-cli spawn events drive a native sub-agent card; docs"
```

---

## Known, accepted limitations (do not "fix" in this plan)

- **Resume, main-loop path:** the claude-cli provider's history is text-only, so after a session resume the main transcript's cards (tool cards *and* these spawn cards) don't rebuild — pre-existing behavior of the display-only side-channel. The `backend: claude-cli` spawn path DOES replay children (Task 5 sidecars).
- **Cost on child cards** may show as tokens-only when `resolve_cost` can't price the CLI-reported model id — same as the existing CLI spawn card behavior.
- **Approvals:** Claude-side children run under Claude's own permission mode; marim's approval flow doesn't apply (already true for everything claude-cli).
- **A killed CLI mid-spawn** can leave a child sidecar whose last message is an unanswered synthesized `spawn_agent` call — sidecars are replay-only (never sent to a provider), so this is cosmetic.
