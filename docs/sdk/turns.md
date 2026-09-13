# Turns, modes & approval

## Running a turn

```python
outcome = await harness.run_turn("summarize the failing tests")
print(outcome.result)
```

`run_turn(prompt, event_stream_handler=None, attachments=None) -> TurnOutcome`
runs the agent until it produces a final answer, looping through any approval
rounds, and returns a `TurnOutcome`. For a plain harness that's
`outcome.result` (the text) with `outcome.structured_output` left `None`; a
harness built with `with_output_type(...)` instead reports the validated
object in `outcome.structured_output` — see
["Structured output"](#structured-output) below. `attachments` is an
optional list of `(bytes, media_type)` pairs (e.g. images) sent with the
prompt.

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

This loop is invisible to `run_turn`'s caller — the turn just ends in one
`TurnOutcome` whose `.result` carries the final text — but it is the
mechanism that makes `Mode` meaningful, and it works headless with no UI
attached.

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
- **A tripped usage limit raises `UsageLimitExceeded`** (pydantic-ai's
  exception) out of `run_turn`, the same way an infra failure does — a
  turn that blew its budget is a failed attempt, not a result. The limit
  set by `with_usage_limits(...)` covers the whole turn across approval
  rounds, so a model that keeps calling a gated tool stops after
  `request_limit` requests no matter how many times it was approved. The
  spend up to the trip is still banked on `harness.session.usage` (and the
  persisted session, if any), so an unattended embedder can log it before
  exiting. A limit that isn't reached is invisible.
- **Hard provider failures spill a debug payload** best-effort to
  `<workspace>/.marim/last-provider-error.json` regardless of session
  config. Gitignore `.marim/` if your workspace is a repo — see
  [Sessions & state](sessions-and-state.md#the-marim-spill).
- **The model not doing what you asked** (e.g. never calling the tool you
  expected, writing to the wrong path) is not an error the harness can see —
  `run_turn` returns a `TurnOutcome` carrying whatever text the model settled
  on (`.result`). Verify contracts
  yourself after the turn (the [tutorial](tutorial-daily-report.md) checks
  that the report file actually exists and exits non-zero when it doesn't).

## Structured output

`with_output_type` makes every turn end in validated structured data instead
of free text — the schema is a property of the harness (Claude-Agent-SDK
style), and tools plus the approval loop work exactly as before mid-turn:

```python
from pydantic import BaseModel
from marim_harness import HarnessBuilder


class Audit(BaseModel):
    summary: str
    files_changed: list[str]


harness = (HarnessBuilder(workspace=Path("."), model="anthropic:claude-sonnet-4-6")
           .with_output_type(Audit)          # or an object-rooted JSON Schema dict
           .build())

outcome = await harness.run_turn("audit the last commit")
assert outcome.subtype == "success"
audit: Audit = outcome.structured_output
```

`run_turn` always returns a `TurnOutcome` (even without a schema — then
`structured_output` is `None` and `result` carries the text):

| Field | Meaning |
|---|---|
| `subtype` | `"success"` · `"error_max_structured_output_retries"` · `"error_during_execution"` (reserved, not emitted) |
| `result` | final assistant text; `None` when the run ended in pure structured output |
| `structured_output` | the validated model instance (BaseModel schema) or dict (JSON Schema) |
| `errors` | failure detail on error subtypes |
| `usage` | a `RunUsage` for **this turn** — requests, tokens, tool calls summed across every approval round. `harness.session.usage` stays the cumulative total across turns; `outcome.usage` is this turn's slice of it. |

Enforcement differs by schema kind: a `BaseModel` is validated by
pydantic-ai inside the run (with retries); a JSON Schema dict constrains
generation provider-side and is checked after the turn — one corrective
round runs if it fails. Infra/provider errors still raise; only schema
failures become error outcomes. JSON Schemas must be object-rooted.

## Resumability (persisted sessions)

With `with_sessions()` on, histories are persisted such that they can be
resumed safely: a persisted history never ends with a dangling tool call
(every provider rejects that on the next request), and an aborted turn is
flushed to a resumable state. This is handled inside the harness
(`_repair_unanswered_tool_calls`, `_flush_resumable`) — embedders don't
manage it, but it's why you can kill a process mid-turn and reload the
session without the next request being rejected.
