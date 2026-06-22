# marim Missing Lifecycle Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fire `PostToolUseFailure`, `Notification`, and `TaskCompleted` lifecycle events so agentmemory's three dormant capture scripts record tool failures, attention moments, and task completions.

**Architecture:** Additive extension of the existing `hooks/` engine. Add three event constants, three `TurnHooks` dispatch methods, a `Deps.turn_hooks` back-reference so tools can fire hooks, and four fire points (tool-result failure branch, ask-mode approval, `ask_user` tool, `update_tasks` tool). Then deploy the on-disk config (copy scripts into `~/.config/marim/agentmemory-hooks/`, repoint `hooks.json`).

**Tech Stack:** Python 3.14, pydantic-ai 1.107, pytest + anyio. Hook scripts are agentmemory's bundled Node `.mjs` (unmodified).

## Global Constraints

- All three new events are **observe-only**: not added to `INJECTING_EVENTS`; `TurnHooks` methods return `None`.
- Every tool-layer fire is guarded `if <turn_hooks> is not None` — a no-op when hooks are unconfigured.
- Failure fires `PostToolUseFailure` *instead of* `PostToolUse` (not both).
- Approval `Notification` fires only for a `DeferredToolRequests` round in `Mode.ask`.
- Canonical tool-result attribute is `event.part` (`result` is a deprecated alias).
- Tests are TDD and mirror `tests/test_agent_hooks.py` / `tests/test_tasks_tool.py` patterns.
- Commit after every green task.

---

### Task 1: Event name constants

**Files:**
- Modify: `src/marim_harness/hooks/events.py`
- Test: `tests/test_hooks_events.py`

**Interfaces:**
- Produces: `hook_events.POST_TOOL_USE_FAILURE == "PostToolUseFailure"`, `hook_events.NOTIFICATION == "Notification"`, `hook_events.TASK_COMPLETED == "TaskCompleted"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hooks_events.py`:

```python
def test_new_event_constants_match_claude_strings():
    from marim_harness.hooks import events
    assert events.POST_TOOL_USE_FAILURE == "PostToolUseFailure"
    assert events.NOTIFICATION == "Notification"
    assert events.TASK_COMPLETED == "TaskCompleted"


def test_new_events_are_not_injecting():
    from marim_harness.hooks import events
    assert events.POST_TOOL_USE_FAILURE not in events.INJECTING_EVENTS
    assert events.NOTIFICATION not in events.INJECTING_EVENTS
    assert events.TASK_COMPLETED not in events.INJECTING_EVENTS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hooks_events.py -v`
Expected: FAIL with `AttributeError: module 'marim_harness.hooks.events' has no attribute 'POST_TOOL_USE_FAILURE'`

- [ ] **Step 3: Add the constants**

In `src/marim_harness/hooks/events.py`, after `SESSION_END = "SessionEnd"` and before the `INJECTING_EVENTS` definition:

```python
POST_TOOL_USE_FAILURE = "PostToolUseFailure"
NOTIFICATION = "Notification"
TASK_COMPLETED = "TaskCompleted"
```

Leave `INJECTING_EVENTS = frozenset({SESSION_START, USER_PROMPT_SUBMIT})` unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_hooks_events.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/hooks/events.py tests/test_hooks_events.py
git commit -m "feat(hooks): add PostToolUseFailure/Notification/TaskCompleted event names"
```

---

### Task 2: `Deps.turn_hooks` bridge + Harness wiring

**Files:**
- Modify: `src/marim_harness/deps.py`
- Modify: `src/marim_harness/agent.py:276` (right after `self.hooks = TurnHooks(...)`)
- Test: `tests/test_agent_hooks.py`

**Interfaces:**
- Produces: `Deps.turn_hooks: Optional[TurnHooks] = None`, set by the Harness to the bound `TurnHooks` so tools fire hooks via `ctx.deps.turn_hooks`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_hooks.py`:

```python
@pytest.mark.anyio
async def test_harness_wires_turn_hooks_onto_deps(tmp_path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_edit_then_done_model(), deps)
    assert deps.turn_hooks is harness.hooks
```

Add the import at the top of the file if missing:

```python
from tests.conftest import _edit_then_done_model
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_hooks.py::test_harness_wires_turn_hooks_onto_deps -v`
Expected: FAIL with `AttributeError: 'Deps' object has no attribute 'turn_hooks'`

- [ ] **Step 3: Add the field and wiring**

In `src/marim_harness/deps.py`, extend the `TYPE_CHECKING` block:

```python
if TYPE_CHECKING:
    from .hooks.dispatch import TurnHooks
    from .hooks.runner import HookRunner
    from .lsp.manager import LspManager
    from .notifications import Notifier
```

Add to the `Deps` dataclass (after the `notifier` field):

```python
    # The session-bound hook dispatcher, set by the Harness so tools (ask_user,
    # update_tasks) can fire lifecycle hooks with a full payload. None until the
    # Harness wires it, or when no hooks are configured.
    turn_hooks: "Optional[TurnHooks]" = None
```

In `src/marim_harness/agent.py`, immediately after `self.hooks = TurnHooks(self.deps, self.session)`:

```python
        # Let tools fire lifecycle hooks via ctx.deps with a full payload.
        self.deps.turn_hooks = self.hooks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_hooks.py::test_harness_wires_turn_hooks_onto_deps -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/deps.py src/marim_harness/agent.py tests/test_agent_hooks.py
git commit -m "feat(hooks): expose session-bound TurnHooks on Deps for tools"
```

---

### Task 3: PostToolUseFailure

**Files:**
- Modify: `src/marim_harness/hooks/dispatch.py` (import `RetryPromptPart`; add `post_tool_use_failure`; branch in `tool_event`)
- Modify: `tests/conftest.py` (add a payload-capture helper)
- Test: `tests/test_agent_hooks.py`

**Interfaces:**
- Consumes: `hook_events.POST_TOOL_USE_FAILURE`.
- Produces: `TurnHooks.post_tool_use_failure(tool_name, tool_input, error)`; `tool_event` fires it for a `RetryPromptPart` result.
- Produces (test helper): `conftest._capture_script(tmp_path, name, outfile)` → path to a bash hook that appends its stdin JSON + newline to `outfile`; `conftest._read_hits(outfile)` → `list[dict]`.

- [ ] **Step 1: Add the capture helpers to conftest**

Add to `tests/conftest.py`:

```python
import json as _json_capture
import stat as _stat_capture


def _capture_script(tmp_path, name: str, outfile) -> str:
    """A hook script that appends its stdin (one JSON payload) + a newline to
    *outfile*, so a test can read back every payload the event fired with."""
    p = tmp_path / name
    p.write_text(
        '#!/usr/bin/env bash\ncat >> "%s"\nprintf "\\n" >> "%s"\n' % (outfile, outfile),
        encoding="utf-8",
    )
    p.chmod(p.stat().st_mode | _stat_capture.S_IEXEC | _stat_capture.S_IRWXU)
    return str(p)


def _read_hits(outfile) -> list:
    """Parse the payloads a _capture_script recorded (one JSON object per line)."""
    from pathlib import Path
    text = Path(outfile).read_text(encoding="utf-8") if Path(outfile).exists() else ""
    return [_json_capture.loads(ln) for ln in text.splitlines() if ln.strip()]
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_agent_hooks.py`:

```python
from pydantic_ai.messages import FunctionToolResultEvent, RetryPromptPart, ToolReturnPart
from tests.conftest import _capture_script, _read_hits


@pytest.mark.anyio
async def test_tool_failure_fires_post_tool_use_failure(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "fail.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.POST_TOOL_USE_FAILURE: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    ev = FunctionToolResultEvent(
        part=RetryPromptPart(content="boom", tool_name="edit_file", tool_call_id="tc1")
    )
    await harness.hooks.tool_event(ev, {"tc1": {"path": "a.txt"}})
    hits = _read_hits(out)
    assert len(hits) == 1
    assert hits[0]["hook_event_name"] == "PostToolUseFailure"
    assert hits[0]["tool_name"] == "edit_file"
    assert hits[0]["tool_input"] == {"path": "a.txt"}
    assert "boom" in hits[0]["error"]


@pytest.mark.anyio
async def test_tool_success_fires_post_tool_use_not_failure(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "ok.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner({
            hook_events.POST_TOOL_USE: [{"hooks": [{"type": "command", "command": cmd}]}],
            hook_events.POST_TOOL_USE_FAILURE: [{"hooks": [{"type": "command", "command": cmd}]}],
        }),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    ev = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="read_file", content="ok", tool_call_id="tc2")
    )
    await harness.hooks.tool_event(ev, {"tc2": {"path": "a.txt"}})
    hits = _read_hits(out)
    assert len(hits) == 1
    assert hits[0]["hook_event_name"] == "PostToolUse"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_hooks.py -k "post_tool_use_failure or success_fires_post_tool_use" -v`
Expected: FAIL — `test_tool_failure...` records 0 hits (no failure branch yet); the failure event never fires.

- [ ] **Step 4: Implement the method and branch**

In `src/marim_harness/hooks/dispatch.py`, update the import:

```python
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    RetryPromptPart,
)
```

Add a method to `TurnHooks` (next to `stop`/`subagent_start`):

```python
    async def post_tool_use_failure(self, tool_name: str, tool_input: dict,
                                    error: str) -> None:
        """PostToolUseFailure: a tool call errored or was retried. Observe-only."""
        await self._dispatch(
            hook_events.POST_TOOL_USE_FAILURE,
            tool_name=tool_name,
            tool_input=tool_input,
            error=error,
        )
```

In `tool_event`, replace the `FunctionToolResultEvent` branch body with:

```python
        elif isinstance(event, FunctionToolResultEvent):
            # Look up the stashed input by tool_call_id; fall back gracefully.
            tool_input = ({} if call_inputs is None
                          else call_inputs.get(event.tool_call_id, {}))
            part = event.part
            if isinstance(part, RetryPromptPart):
                # A failed/retried call: fire PostToolUseFailure instead of
                # PostToolUse so the two are distinct (matches Claude Code).
                await self.post_tool_use_failure(
                    tool_name=getattr(part, "tool_name", "") or "",
                    tool_input=tool_input,
                    error=part.model_response(),
                )
            else:
                await self.deps.hooks.dispatch(
                    hook_events.POST_TOOL_USE,
                    self._payload(
                        hook_events.POST_TOOL_USE,
                        tool_name=getattr(event.part, "tool_name", ""),
                        tool_input=tool_input,
                        tool_response=str(getattr(event.part, "content", "")),
                    ),
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_hooks.py -k "post_tool_use_failure or success_fires_post_tool_use" -v`
Expected: PASS (both)

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/hooks/dispatch.py tests/conftest.py tests/test_agent_hooks.py
git commit -m "feat(hooks): fire PostToolUseFailure on failed tool results"
```

---

### Task 4: Notification (approval + ask_user)

**Files:**
- Modify: `src/marim_harness/hooks/dispatch.py` (add `notification`)
- Modify: `src/marim_harness/agent.py` (import `Mode`; fire on ask-mode approval round)
- Modify: `src/marim_harness/tools/provider.py` (fire in `ask_user`)
- Test: `tests/test_agent_hooks.py`, `tests/test_ask_user_tool.py`

**Interfaces:**
- Consumes: `hook_events.NOTIFICATION`, `Deps.turn_hooks`, `permissions.Mode`.
- Produces: `TurnHooks.notification(notification_type, title, message)`.

- [ ] **Step 1: Write the failing dispatch + approval tests**

Add to `tests/test_agent_hooks.py`:

```python
@pytest.mark.anyio
async def test_notification_dispatch_payload(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "n.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.NOTIFICATION: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    await harness.hooks.notification("ask_user", "Question from agent", "pick one")
    hits = _read_hits(out)
    assert hits[0]["hook_event_name"] == "Notification"
    assert hits[0]["notification_type"] == "ask_user"
    assert hits[0]["title"] == "Question from agent"
    assert hits[0]["message"] == "pick one"


@pytest.mark.anyio
async def test_approval_round_fires_notification_in_ask_mode(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "appr.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.ask,
        hooks=HookRunner(
            {hook_events.NOTIFICATION: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )

    async def _approve(call):
        return True

    deps.request_approval = _approve
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    await harness.run_turn("edit it")
    hits = [h for h in _read_hits(out) if h["hook_event_name"] == "Notification"]
    assert any(h["notification_type"] == "approval_needed" for h in hits)
    assert any("edit_file" in h["message"] for h in hits)


@pytest.mark.anyio
async def test_auto_mode_does_not_fire_approval_notification(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "noappr.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.NOTIFICATION: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    await harness.run_turn("edit it")
    hits = [h for h in _read_hits(out)
            if h.get("notification_type") == "approval_needed"]
    assert hits == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_agent_hooks.py -k "notification or approval" -v`
Expected: FAIL — `notification` method missing / approval notification never fired.

- [ ] **Step 3: Add the dispatch method**

In `src/marim_harness/hooks/dispatch.py`, add to `TurnHooks`:

```python
    async def notification(self, notification_type: str, title: str,
                           message: str) -> None:
        """Notification: the agent needs the user's attention (approval / a
        question). Observe-only."""
        await self._dispatch(
            hook_events.NOTIFICATION,
            notification_type=notification_type,
            title=title,
            message=message,
        )
```

- [ ] **Step 4: Fire on the ask-mode approval round**

In `src/marim_harness/agent.py`, change the permissions import:

```python
from .permissions import Mode, resolve_approvals
```

In `run_turn`, inside `if isinstance(result.output, DeferredToolRequests):`, before the `try:` that calls `resolve_approvals`, add:

```python
                if self.deps.mode is Mode.ask and result.output.approvals:
                    names = ", ".join(
                        getattr(c, "tool_name", "") for c in result.output.approvals
                    )
                    await self.hooks.notification(
                        "approval_needed", "Approval needed", names
                    )
```

- [ ] **Step 5: Run the approval tests**

Run: `uv run pytest tests/test_agent_hooks.py -k "notification or approval" -v`
Expected: PASS (all three)

- [ ] **Step 6: Write the failing ask_user test**

Add to `tests/test_ask_user_tool.py`:

```python
def test_ask_user_fires_notification(tmp_path):
    from marim_harness.deps import Deps

    class _Spy:
        def __init__(self):
            self.calls = []

        async def notification(self, notification_type, title, message):
            self.calls.append((notification_type, title, message))

    spy = _Spy()

    async def _answer(questions):
        return {questions[0].header or "q": "yes"}

    deps = Deps(workspace_root=tmp_path, ask_user=_answer)
    deps.turn_hooks = spy
    agent = _agent()
    model, _ = _call_tool(
        "ask_user",
        {"questions": [{"question": "Proceed?", "header": "go",
                        "options": [{"label": "yes"}, {"label": "no"}]}]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert spy.calls and spy.calls[0][0] == "ask_user"
    assert "Proceed?" in spy.calls[0][2]
```

If `_agent` / `_call_tool` are not already in `tests/test_ask_user_tool.py`, copy them from `tests/test_tasks_tool.py` (the `_agent()` builder and the `_call_tool(tool_name, args)` FunctionModel helper).

- [ ] **Step 7: Run to verify it fails**

Run: `uv run pytest tests/test_ask_user_tool.py::test_ask_user_fires_notification -v`
Expected: FAIL — `spy.calls` is empty.

- [ ] **Step 8: Fire in the ask_user tool**

In `src/marim_harness/tools/provider.py`, inside `ask_user`, after the `if ctx.deps.ask_user is None:` guard and before `answers = await ctx.deps.ask_user(coerced)`:

```python
    th = getattr(ctx.deps, "turn_hooks", None)
    if th is not None:
        await th.notification(
            "ask_user", "Question from agent", coerced[0].question
        )
```

- [ ] **Step 9: Run the ask_user test**

Run: `uv run pytest tests/test_ask_user_tool.py::test_ask_user_fires_notification -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/hooks/dispatch.py src/marim_harness/agent.py src/marim_harness/tools/provider.py tests/test_agent_hooks.py tests/test_ask_user_tool.py
git commit -m "feat(hooks): fire Notification on ask_user and ask-mode approval"
```

---

### Task 5: TaskCompleted

**Files:**
- Modify: `src/marim_harness/hooks/dispatch.py` (add `task_completed`)
- Modify: `src/marim_harness/tools/provider.py` (`update_tasks` → async; diff + fire)
- Test: `tests/test_tasks_tool.py`

**Interfaces:**
- Consumes: `hook_events.TASK_COMPLETED`, `Deps.turn_hooks`.
- Produces: `TurnHooks.task_completed(task_subject, task_id=None, task_description="")`; `update_tasks` (now async) fires it once per task that newly becomes `done`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tasks_tool.py`:

```python
class _TaskSpy:
    def __init__(self):
        self.subjects = []

    async def task_completed(self, task_subject, task_id=None, task_description=""):
        self.subjects.append(task_subject)


def test_update_tasks_fires_task_completed_for_newly_done(tmp_path):
    deps = Deps(workspace_root=tmp_path)
    spy = _TaskSpy()
    deps.turn_hooks = spy
    agent = _agent()
    model, _ = _call_tool(
        "update_tasks",
        {"tasks": [{"text": "a", "status": "done"},
                   {"text": "b", "status": "in_progress"},
                   {"text": "c"}]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert spy.subjects == ["a"]


def test_update_tasks_does_not_refire_already_done(tmp_path):
    deps = Deps(workspace_root=tmp_path)
    deps.tasks.replace([{"text": "a", "status": "done"}])
    spy = _TaskSpy()
    deps.turn_hooks = spy
    agent = _agent()
    model, _ = _call_tool(
        "update_tasks",
        {"tasks": [{"text": "a", "status": "done"},
                   {"text": "b", "status": "done"}]},
    )
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert spy.subjects == ["b"]


def test_update_tasks_no_hooks_is_safe(tmp_path):
    deps = Deps(workspace_root=tmp_path)  # turn_hooks defaults None
    agent = _agent()
    model, _ = _call_tool("update_tasks", {"tasks": [{"text": "x", "status": "done"}]})
    with agent.override(model=model):
        agent.run_sync("go", deps=deps)
    assert [t.text for t in deps.tasks.items] == ["x"]
```

- [ ] **Step 2: Write the dispatch-level test**

Add to `tests/test_agent_hooks.py`:

```python
@pytest.mark.anyio
async def test_task_completed_dispatch_payload(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "tc.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.TASK_COMPLETED: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    await harness.hooks.task_completed(task_subject="ship it")
    hits = _read_hits(out)
    assert hits[0]["hook_event_name"] == "TaskCompleted"
    assert hits[0]["task_subject"] == "ship it"
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_tasks_tool.py -k task_completed tests/test_agent_hooks.py::test_task_completed_dispatch_payload -v`
Expected: FAIL — method missing / no fire.

- [ ] **Step 4: Add the dispatch method**

In `src/marim_harness/hooks/dispatch.py`, add to `TurnHooks`:

```python
    async def task_completed(self, task_subject: str, task_id=None,
                             task_description: str = "") -> None:
        """TaskCompleted: a checklist item transitioned to done. Observe-only."""
        await self._dispatch(
            hook_events.TASK_COMPLETED,
            task_id=task_id,
            task_subject=task_subject,
            task_description=task_description,
        )
```

- [ ] **Step 5: Make update_tasks async + fire on newly-done**

In `src/marim_harness/tools/provider.py`, replace the `update_tasks` function body:

```python
async def update_tasks(ctx: RunContext[Deps], tasks: list[Task]) -> str:
    """Maintain your checklist for the current multi-step task. Pass the
    FULL list every time — it replaces the previous one. Each item is
    {text, status} where status is pending, in_progress, or done. Keep
    exactly one item in_progress, and mark items done as you finish them.
    Use this for non-trivial work spanning several steps so progress is
    visible; skip it for single-step requests. No approval is needed."""
    before = {t.text: t.status for t in ctx.deps.tasks.items}
    ctx.deps.tasks.replace(tasks)
    th = getattr(ctx.deps, "turn_hooks", None)
    if th is not None:
        for t in ctx.deps.tasks.items:
            if t.status == "done" and before.get(t.text) != "done":
                await th.task_completed(task_subject=t.text)
    return summarize(ctx.deps.tasks.items)
```

(pydantic-ai registers async tools transparently — the `agent.tool(update_tasks)` registration at the bottom of `provider.py` needs no change.)

- [ ] **Step 6: Run to verify they pass**

Run: `uv run pytest tests/test_tasks_tool.py tests/test_agent_hooks.py::test_task_completed_dispatch_payload -v`
Expected: PASS (including the pre-existing `update_tasks` tests, unaffected by the async change)

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/hooks/dispatch.py src/marim_harness/tools/provider.py tests/test_tasks_tool.py tests/test_agent_hooks.py
git commit -m "feat(hooks): fire TaskCompleted when a checklist item becomes done"
```

---

### Task 6: Deploy config — copy scripts + repoint hooks.json

**Files:**
- Create: `~/.config/marim/agentmemory-hooks/*.mjs` (copied)
- Modify: `~/.config/marim/hooks.json` (repoint all 9 + add 3)

These are outside the repo (global marim config); not committed. This task wires the new code to agentmemory's scripts.

- [ ] **Step 1: Copy the scripts**

```bash
mkdir -p ~/.config/marim/agentmemory-hooks
cp ~/.claude/plugins/marketplaces/agentmemory/plugin/scripts/*.mjs ~/.config/marim/agentmemory-hooks/
ls ~/.config/marim/agentmemory-hooks/
```
Expected: 14 `.mjs` files including `session-start.mjs`, `post-tool-failure.mjs`, `notification.mjs`, `task-completed.mjs`.

- [ ] **Step 2: Rewrite `~/.config/marim/hooks.json`**

Repoint every command from `.../.claude/plugins/marketplaces/agentmemory/plugin/scripts/` to `/home/mateuscmarim/.config/marim/agentmemory-hooks/`, keeping the inline-env form, and add three entries:

- `PostToolUseFailure` → `post-tool-failure.mjs`
- `Notification` → `notification.mjs`
- `TaskCompleted` → `task-completed.mjs`

(Full file content: the 9 existing events repointed, plus the 3 new events, each command of the form
`AGENTMEMORY_URL=http://nanocore.marim.dev:3111 AGENTMEMORY_INJECT_CONTEXT=true /usr/bin/node "/home/mateuscmarim/.config/marim/agentmemory-hooks/<script>.mjs"`. PreToolUse keeps its `read_file|write_file|edit_file|glob|grep|tree` matcher.)

- [ ] **Step 3: Validate via marim's own loader**

```bash
~/.local/share/uv/tools/marim-harness/bin/python - <<'EOF'
from pathlib import Path
from marim_harness.hooks.config import load_hooks_config
h = load_hooks_config(Path.home(), trust_project=False)
print("events:", sorted(h.keys()), "count:", len(h))
assert {"PostToolUseFailure", "Notification", "TaskCompleted"} <= set(h)
EOF
```
Expected: 12 events listed, assertion passes.

- [ ] **Step 4: Smoke-test the three new scripts against nanocore**

```bash
B=http://nanocore.marim.dev:3111
echo '{"hook_event_name":"TaskCompleted","session_id":"smoke","cwd":"'$HOME'","transcript_path":"/tmp/x","task_subject":"smoke task"}' \
  | AGENTMEMORY_URL=$B /usr/bin/node ~/.config/marim/agentmemory-hooks/task-completed.mjs; echo "task-completed exit $?"
echo '{"hook_event_name":"Notification","session_id":"smoke","cwd":"'$HOME'","transcript_path":"/tmp/x","notification_type":"ask_user","title":"t","message":"m"}' \
  | AGENTMEMORY_URL=$B /usr/bin/node ~/.config/marim/agentmemory-hooks/notification.mjs; echo "notification exit $?"
echo '{"hook_event_name":"PostToolUseFailure","session_id":"smoke","cwd":"'$HOME'","transcript_path":"/tmp/x","tool_name":"edit_file","tool_input":{},"error":"boom"}' \
  | AGENTMEMORY_URL=$B /usr/bin/node ~/.config/marim/agentmemory-hooks/post-tool-failure.mjs; echo "post-tool-failure exit $?"
```
Expected: all exit 0 (scripts POST to `/agentmemory/observe` and are observe-only, so no stdout).

---

### Final verification

- [ ] **Run the full suite**

Run: `uv run pytest -q`
Expected: all pass (no regressions in `test_tasks_tool.py`, `test_agent_hooks.py`, `test_provider.py`).

- [ ] **Lint**

Run: `uv run ruff check src/ tests/`
Expected: clean.
