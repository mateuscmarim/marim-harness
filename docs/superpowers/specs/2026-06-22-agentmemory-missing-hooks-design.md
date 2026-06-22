# Design: marim missing lifecycle hooks (PostToolUseFailure, Notification, TaskCompleted)

- **Date:** 2026-06-22
- **Status:** Approved (design); pending implementation plan
- **Goal:** Fire the three Claude-Code lifecycle events the hooks engine does not
  yet emit — `PostToolUseFailure`, `Notification`, `TaskCompleted` — so
  agentmemory's three currently-dormant capture scripts
  (`post-tool-failure.mjs`, `notification.mjs`, `task-completed.mjs`) record
  tool failures, attention moments, and task completions. Pure additive
  extension of the existing engine; no contract change.

## Motivation

marim's hooks engine (see `2026-06-17-marim-hooks-engine-design.md`) emits 9 of
Claude Code's 12 lifecycle events. agentmemory's plugin ships 12 hook scripts;
the 3 unwired events mean tool failures, notifications, and task completions are
never captured into memory. This adds those three fire points so the agentmemory
integration reaches full parity, and decouples the on-disk scripts from Claude
Code's plugin clone.

## Decisions (locked during brainstorming)

1. **Notification scope:** attention-only — fire on `ask_user` and on a real
   approval prompt. Decoupled from the desktop `Notifier` opt-in
   (`MARIM_NOTIFICATIONS`): the hook fires regardless of whether desktop
   notifications are enabled.
2. **Approval gating:** the approval `Notification` fires only when the user is
   actually prompted, i.e. a `DeferredToolRequests` round in `Mode.ask`. In
   `auto` mode (auto-approved, no prompt) it does **not** fire.
3. **Failure vs success are distinct:** a failed tool result fires
   `PostToolUseFailure` *instead of* `PostToolUse`, matching Claude. Detected via
   the result part type.
4. **Rigor:** TDD, tests mirroring the existing `tests/` patterns for the hooks
   engine and tools.
5. **Scripts on disk:** copy agentmemory's hook scripts into
   `~/.config/marim/agentmemory-hooks/` and repoint **all** `hooks.json` entries
   there, so marim no longer depends on `~/.claude/plugins/marketplaces/...`.

## Non-goals

- No veto/blocking semantics (consistent with the base engine).
- No `Notification` fire for MCP per-tool approval prompts
  (`mcp/config.py` `make_approval_hook`) in v1 — only the main
  `DeferredToolRequests` approval round and `ask_user`.
- No new event-injection: all three are observe-only; `INJECTING_EVENTS`
  unchanged.
- No change to the on-disk `hooks.json`/payload contract.

## Architecture

### `hooks/events.py` — add three constants

```python
POST_TOOL_USE_FAILURE = "PostToolUseFailure"
NOTIFICATION          = "Notification"
TASK_COMPLETED        = "TaskCompleted"
```

`INJECTING_EVENTS` stays `{SESSION_START, USER_PROMPT_SUBMIT}`.

### `hooks/dispatch.py` — three new `TurnHooks` methods

Each assembles a payload (base fields from the live session, via the existing
`_payload`) plus the event-specific extras the scripts read:

- `post_tool_use_failure(tool_name, tool_input, error)` → extras
  `tool_name`, `tool_input`, `error`.
- `notification(notification_type, title, message)` → extras
  `notification_type`, `title`, `message`.
- `task_completed(task_subject, task_id=None, task_description="")` → extras
  `task_id`, `task_subject`, `task_description`.

All observe-only (return `None`; not in `INJECTING_EVENTS`).

### `deps.py` — bridge hooks into the tool layer

Tools (`ask_user`, `update_tasks`) only receive `ctx.deps`, which has the raw
`HookRunner` but not the session needed for a full payload. Add:

```python
turn_hooks: Optional["TurnHooks"] = None   # set by the Harness after binding
```

The Harness sets `deps.turn_hooks = self.hooks` immediately after constructing
`TurnHooks` (agent.py ~276). Every fire from a tool is a cheap
`if ctx.deps.turn_hooks is not None` no-op when hooks are unconfigured. Sub-agent
deps leave it `None` unless explicitly wired (out of scope here).

### Fire points

1. **PostToolUseFailure** — `hooks/dispatch.py::TurnHooks.tool_event`, the
   `FunctionToolResultEvent` branch. The result part is
   `ToolReturnPart | RetryPromptPart`; `isinstance(part, RetryPromptPart)`
   signals a failed/retried call. On failure: fire `post_tool_use_failure` with
   `error=part.model_response()` and the correlated `tool_input`; otherwise the
   existing `PostToolUse` path. (Confirmed: pydantic-ai 1.107
   `FunctionToolResultEvent.result: ToolReturnPart | RetryPromptPart`; the
   current code already reads the part defensively via `getattr`.)

2. **Notification (ask_user)** — `tools/provider.py::ask_user`, before
   `await ctx.deps.ask_user(coerced)`: fire `notification("ask_user",
   "Question from agent", <first question text>)` via `ctx.deps.turn_hooks`.

3. **Notification (approval)** — `agent.py::run_turn`, when
   `isinstance(result.output, DeferredToolRequests)` **and**
   `self.deps.mode is Mode.ask`, before `resolve_approvals`: fire
   `notification("approval_needed", "Approval needed", <comma-joined tool
   names>)`.

4. **TaskCompleted** — `tools/provider.py::update_tasks`: snapshot
   `{text: status}` before the `TaskList.replace`, then after replace fire
   `task_completed(task_subject=text)` once per task whose status is now `done`
   and was not `done` before. New-and-already-done items (added directly as
   `done`) count as completed; pending/in-progress do not.

### Config / scripts

- **Copy:** `~/.claude/plugins/marketplaces/agentmemory/plugin/scripts/*.mjs` →
  `~/.config/marim/agentmemory-hooks/`.
- **`~/.config/marim/hooks.json`:** repoint all existing 9 commands to the new
  dir, and add the 3 new entries:
  - `PostToolUseFailure` → `post-tool-failure.mjs`
  - `Notification` → `notification.mjs`
  - `TaskCompleted` → `task-completed.mjs`

  All keep the inline `AGENTMEMORY_URL=http://nanocore.marim.dev:3111
  AGENTMEMORY_INJECT_CONTEXT=true /usr/bin/node "<path>"` form.

## Testing (TDD)

- **dispatch:** `post_tool_use_failure`/`notification`/`task_completed` build the
  documented payload and dispatch to the right event name; observe-only
  (return `None`).
- **tool_event branch:** a `RetryPromptPart` result fires `PostToolUseFailure`
  (not `PostToolUse`) with `error` populated; a `ToolReturnPart` fires
  `PostToolUse` as before.
- **ask_user:** firing `Notification("ask_user", ...)`; no-op when
  `turn_hooks is None`.
- **approval:** `Notification("approval_needed", ...)` fires for a
  `DeferredToolRequests` round in `Mode.ask`; does **not** fire in `Mode.auto`.
- **update_tasks:** `TaskCompleted` fires once per newly-`done` task; not for
  pending/in-progress or unchanged-already-done; correct `task_subject`.
- **deps bridge:** `turn_hooks` defaults `None`; Harness wiring sets it.

## Risks

- **Approval double-counting:** a `DeferredToolRequests` round may carry several
  tool calls; v1 fires one `approval_needed` notification per round (not per
  call), matching the single user prompt. Acceptable.
- **Sub-agents:** sub-agent tool failures still fire `PostToolUseFailure`
  through the sub-agent's own `tool_event`; `ask_user`/`update_tasks` from a
  sub-agent are no-ops for the new tool-layer events unless its deps get
  `turn_hooks` — explicitly out of scope.
