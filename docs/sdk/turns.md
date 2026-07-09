# Turns, modes & approval

## Running a turn

```python
reply: str = await harness.run_turn("summarize the failing tests")
```

`run_turn(prompt, event_stream_handler=None, attachments=None) -> str` runs
the agent until it produces a final text answer, looping through any approval
rounds, and returns that text. `attachments` is an optional list of
`(bytes, media_type)` pairs (e.g. images) sent with the prompt.

Turns are sequential per harness: one `run_turn` at a time. The session
accumulates history across turns (see
[Sessions & state](sessions-and-state.md)), so a second `run_turn` on the
same harness continues the conversation.

## The approval loop

The agent's output type is `[str, DeferredToolRequests]`. Tools registered
with `requires_approval=True` (built-in: `write_file`, `edit_file`, `bash`,
`web_search`, `fetch_url`; plus any [custom tool](custom-tools.md) you gate)
do not run immediately — the model's call *defers*, and the harness resolves
the deferred batch against the current `Mode` before continuing the run:

```
model calls write_file ──► deferred ──► resolve_approvals(mode)
      ▲                                      │ approve / deny per call
      └────────── run continues with results ┘
```

This loop is invisible to `run_turn`'s caller — you just get the final text —
but it is the mechanism that makes `Mode` meaningful, and it works headless
with no UI attached.

## Mode semantics

`Mode` is a string enum: `Mode.auto`, `Mode.ask`, `Mode.plan`. Set the
initial mode with `with_mode(...)`; the current mode lives at
`harness.deps.workspace.mode`.

| Mode | Gated tools | Notes |
| --- | --- | --- |
| `auto` | Run unprompted | The right default for headless/unattended embedders. |
| `ask` | Delegated to your approval callback | Wired via `bind_ui(request_approval=...)`. **With no callback wired, `ask` denies every gated call** rather than crash — nothing can grant approval. |
| `plan` | Denied | Read-only research mode. A read-only `bash` command (`git log`, `ls`, …) is allowed through best-effort; mutating commands are denied. |

Plan mode also denies the network tools (`web_search`, `fetch_url`) with an
explicit egress message: plan mode is presented as *local* read-only
research, and a prompt-injected agent could otherwise read any file and
exfiltrate it through a fetch URL or search query with zero approval.

## `bind_ui` — attaching an interactive front-end

Headless embedders never call `bind_ui`; every callback stays `None` and the
harness guards each one. If you are building an interactive front-end (the
TUI is the reference consumer), wire callbacks in one place:

```python
harness.bind_ui(
    request_approval=my_approval_fn,   # async; drives Mode.ask
    ask_user=my_question_fn,           # the ask_user tool (with_tasks)
    on_mode_change=refresh_statusbar,
    # ... plus sub-agent/task/job/compaction/rename observers
)
```

`request_approval` receives the deferred tool call object and returns a
pydantic-ai `DeferredToolApprovalResult` or a plain `bool`. Everything else
is an observer — the full parameter list is in
`runtime/harness.py::Harness.bind_ui`.

Do not poke `harness.deps` fields one at a time from your interface layer;
`bind_ui` exists so callback wiring lives in one place. *Reading* harness
state (e.g. `harness.deps.tasks.items`) is fine.

## Streaming

`stream_turn` (a first-class async-iterator API) is planned but not yet
implemented. Today, pass pydantic-ai's `event_stream_handler` to observe
events (model deltas, tool calls, tool results) as they happen:

```python
async def on_event(ctx, event) -> None:
    ...  # render deltas, log tool calls, etc.

reply = await harness.run_turn(prompt, event_stream_handler=on_event)
```

The handler signature and event types are pydantic-ai's
(`EventStreamHandler`); marim passes it straight through to the underlying
agent run.

## Errors

- **Provider/infra failures** (rate limits, 5xx, network) raise out of
  `run_turn` — wrap it in `try/except` and decide your own retry/report
  policy. An unattended embedder should treat a failed turn as "log and exit
  non-zero". With persistence on, the aborted turn's completed progress is
  not discarded: a repaired, resumable snapshot is flushed to the session
  (see [Resumability](#resumability-persisted-sessions) below), so the next
  turn can pick up from what already happened rather than from before the
  turn.
- **Hard provider failures spill a debug payload** best-effort to
  `<workspace>/.marim/last-provider-error.json` regardless of session
  config. Gitignore `.marim/` if your workspace is a repo — see
  [Sessions & state](sessions-and-state.md#the-marim-spill).
- **The model not doing what you asked** (e.g. never calling the tool you
  expected, writing to the wrong path) is not an error the harness can see —
  `run_turn` returns whatever text the model settled on. Verify contracts
  yourself after the turn (the [tutorial](tutorial-daily-report.md) checks
  that the report file actually exists and exits non-zero when it doesn't).

## Resumability (persisted sessions)

With `with_sessions()` on, histories are persisted such that they can be
resumed safely: a persisted history never ends with a dangling tool call
(every provider rejects that on the next request), and an aborted turn is
flushed to a resumable state. This is handled inside the harness
(`_repair_unanswered_tool_calls`, `_flush_resumable`) — embedders don't
manage it, but it's why you can kill a process mid-turn and reload the
session without the next request being rejected.
