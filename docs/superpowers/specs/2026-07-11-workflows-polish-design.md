# Workflows Polish Batch — Design

Date: 2026-07-11
Branch: `feat/workflows-polish` (off master at 42825eb, after PR #63 merged)
Status: approved pending user review

## Goal

Four self-contained follow-ups that make the dynamic-workflows feature feel
finished: a hardened model-facing docstring, a first-class workflow card in
the sub-agents screen (with `log()` lines persisted to its pane), tree
grouping of workflow children under that card, and structured output for
schema'd `agent()` calls so validation failures no longer cost a full child
re-run.

## Item 1 — `run_workflow` docstring hardening

`tools/workflow_tools.py` only. Add a "Common mistakes" section to the
`run_workflow` tool docstring, written from failures observed in live runs:

- The script's **last expression** is the tool's result. Ending with
  `print(result)` (or `print(json.dumps(result))`) evaluates to `None`;
  end with the bare value instead.
- No `asyncio.run(...)` — the script body is already async-driven; `await`
  directly.
- `log()` is the progress channel, never the result channel.

Pure docstring edit; no behavior change. One test asserts the marker text
("Common mistakes") is present in the registered tool description so a
future rewrite can't silently drop it.

## Item 2 — Workflow card in the sub-agents screen

Today the workflow run has no presence in the sub-agents screen: children
appear as flat root-level cards, and `log()` is a transient toast
(`app.py` wires `on_workflow_log` to `self.notify`), so progress lines
vanish.

**Engine → UI seams.** Two new optional `UIHooks` callbacks on
`runtime/deps.py`, mirroring the existing `on_workflow_spawn` /
`on_workflow_spawn_done` pair, threaded through `Harness.bind_ui` and fired
by `workflows/engine.py`:

- `on_workflow_start(tool_call_id: str, title: str)` — fired in `run()`
  right after the script parses (a parse failure returns before any card
  exists). `title` is derived by the engine: the script's first comment
  line if present, else `workflow script (N lines)`.
- `on_workflow_done(tool_call_id: str, outcome: str, failed: bool)` —
  fired exactly once at every exit of `run()`: success (`failed=False`,
  outcome = the shaped result), script raise or timeout (`failed=True`,
  outcome = the error string), and the `CancelledError` path
  (`failed=True`, outcome = `"workflow aborted"`) before re-raising. The
  flag is explicit because the engine knows which exit it took — no
  re-sniffing of result text.

`_log` changes from `on_workflow_log(message)` to
`on_workflow_log(tool_call_id, message)` so the TUI can route lines to the
right card. The signature change is internal; headless leaves all three
callbacks `None`, and every call site guards with `is None` /
`getattr(..., None)` as the existing hooks do.

**TUI behavior** (`stream_render.py`, `app.py`):

- `on_workflow_start` claims a `SubAgentWidget` with
  `stream_id = tool_call_id`, type `"workflow"`, task = title. It registers
  in `self.subagents` and `tool_widgets` and gets a pane via `ensure_pane`,
  but is **not mounted into the transcript** — the `run_workflow` tool
  widget already represents the run there; this card exists for the
  sub-agents screen.
- `on_workflow_log` appends the line to the workflow card's transcript
  pane (persisted for the live session) and keeps the existing toast.
- `on_workflow_done` settles the card:
  `finish(outcome, status="failed" if failed else "done")`, then refreshes
  the sub-agents screen, the same shape as `finish_workflow_child`.

## Item 3 — Tree grouping of workflow children

`claim_workflow_spawn` already receives `parent_id` (the `run_workflow`
tool_call_id) and deliberately sets `widget.parent_id = None` with a
"future tree grouping" comment. Flip it: `widget.parent_id = parent_id`.

Because Item 2 registers the workflow card with
`stream_id = tool_call_id`, the sub-agents list's existing depth-first
tree walk (`subagents/stats.py`) nests the children under it with no
changes to the tree code. Its unknown-parent fallback (agents whose
`parent_id` names no card in the list fall to root) keeps behavior sane in
any edge case where the card is absent.

**Main transcript is unchanged:** children keep mounting inline as they
spawn (user decision — watch progress without switching screens). The
nested view is additive, in the sub-agents screen only.

## Item 4 — Structured output for schema'd `agent()` calls

Today `agent(task, schema=...)` enforces the schema by prompt:
`output_contract(schema)` appends a "respond with ONLY a JSON object…"
paragraph, `validate_report` extracts JSON (whole report or first fenced
block) and validates with jsonschema, and a failure costs one **full child
re-spawn** to retry.

**New mechanism.** Pass the schema down the spawn seam instead of baking
it into the task text. `SubagentRunner.run` (and `_execute_spawn` /
`build` beneath it) gains an optional `output_schema: dict | None`
parameter:

- **Native spawns** build the child agent with
  `output_type=StructuredDict(schema)` (pydantic-ai 2.8, verified
  importable). Validation failures become in-run `ModelRetry`s — the model
  re-emits just the final output, instead of the whole task re-running.
  The runner serializes the validated dict with `json.dumps` so
  `run(...) -> str` and every seam above it stay intact.
- **`claude-cli` backend spawns** cannot take an output type (marim is a
  launcher there), so the runner appends the same `output_contract(schema)`
  paragraph to the task itself — the prompt path becomes the fallback,
  chosen by the component that knows the backend.

**Engine changes** (`workflows/engine.py`, `workflows/schema.py`):
`_agent_call` stops appending `output_contract` and passes
`output_schema=schema` through `_spawn_child` to the runner. It keeps the
existing `validate_report` + one re-spawn retry as defense in depth: on
the native path validation passes trivially (the JSON round-trips); on the
cli/fallback path it does exactly the work it does today. `output_contract`
stays in `schema.py`, now called by the runner's fallback instead of the
engine.

The workflow spec's public surface (`agent(task, schema=...)` returning a
validated object) is unchanged; only the enforcement mechanism moves.

## Non-goals

- Cross-session replay of workflow cards and `log()` lines (owned by the
  future resumability-journal work; children don't replay today either).
- Detachable/background workflows, saved named workflows,
  concurrency/budget knobs.
- Provider-native `response_format` (constrained decoding) — pydantic-ai's
  default tool-output mode covers every tool-calling provider; native mode
  can be a later flag.
- Any headless behavior change: all new hooks are optional callbacks that
  headless leaves `None`.

## Error handling

- Engine hook calls are guarded (`getattr(..., None)` / `is None`) and
  must not be able to break a run: a UI callback raising is a render bug,
  not a workflow failure — same posture as the existing workflow hooks.
- `on_workflow_done` fires on **every** exit path including cancellation,
  so a workflow card can never be left pending the way children once were.
- On the structured-output path, a child that exhausts pydantic-ai's
  retries surfaces the failure in its report; the engine's existing
  validate-and-re-spawn loop then behaves exactly as today.

## Testing

Same patterns as the previous workflow rounds:

- **Engine** (`tests/test_workflow_engine.py`): `on_workflow_start` fires
  with the derived title; `on_workflow_done` fires with the right
  `(outcome, failed)` on success, script-raise, timeout, and abort paths;
  `_log` passes the tool_call_id; schema'd calls pass `output_schema`
  through the spawn seam and no longer append the contract to the task.
- **Runner** (a new `tests/test_subagent_output_schema.py`, following the
  per-concern `test_subagent_*.py` convention): `output_schema` builds the
  agent with a `StructuredDict` output type (assert via `TestModel` /
  agent introspection) and serializes the dict report; cli-backend spawns
  get the contract paragraph appended instead
  (`tests/test_subagent_cli_spawn.py`).
- **TUI** (`tests/test_app.py`, `tests/test_subagents_screen.py`):
  `bind_ui` wiring for the three callbacks; a claimed workflow card nests
  its children in the tree order; `on_workflow_log` lands in the pane;
  `on_workflow_done` settles the card with the right status.
- **Docstring** (`tests/test_workflow_tool.py`): the registered tool
  description contains the common-mistakes marker text.

## Decisions log

- Polish batch before the resumability journal (user).
- Workflow run gets a first-class card in the sub-agents screen; grouping
  and `log()` persistence ride the same mechanism (user).
- Children stay inline in the main transcript; the tree lives in the
  sub-agents screen (user).
- Engine-announced card (new hooks) over TUI-side inference or a
  transcript-side tree (user, "Approach A").
- Structured output via `StructuredDict` with prompt-contract fallback for
  `claude-cli`, included in this batch (user, "A").
