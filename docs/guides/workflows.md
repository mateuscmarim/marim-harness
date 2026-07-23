# Dynamic workflows

Dynamic workflows let the agent orchestrate sub-agents with real control flow.
The gated `run_workflow` tool executes a model-authored Python script in a
[pydantic-monty](https://pypi.org/project/pydantic-monty/) sandbox: the script
spawns sub-agents through an `agent()` host function, fans them out with
`asyncio.gather`, loops, branches, retries, and aggregates — deterministic
orchestration that the one-shot `spawn_agent` tool cannot express.

The model reaches for it when coordinating several sub-agents whose number,
order, or inputs depend on data: a review sweep over N dimensions, a
retry-until-valid loop, map-reduce over a file list. For one or two independent
spawns, plain `spawn_agent` is simpler and the tool description says so.

## Enabling workflows

Workflows need the optional `[workflows]` extra, which brings in the sandbox
interpreter and the schema validator:

```bash
uv add 'marim-harness[workflows]'   # pydantic-monty + jsonschema
```

Two switches control availability:

- **The extra.** Without `pydantic-monty` installed, the tool still exists but
  answers every call with an install hint and suggests `spawn_agent` instead.
- **`MARIM_WORKFLOWS`** — on by default; set `MARIM_WORKFLOWS=0` to turn the
  feature off. The TUI Settings screen has a matching toggle that persists the
  variable and flips the live seam without a restart.

One more knob bounds runtime (see [Budgets](#budgets-and-safety)):

```bash
# Ceiling (seconds) on the wall-clock budget one run_workflow call may request
# via its timeout_secs parameter. Default 1800 (30 min).
MARIM_WORKFLOW_TIMEOUT=1800
```

`run_workflow` is main-agent only — sub-agents never get it. It is registered
with `requires_approval=True`, exactly like `write_file`, `edit_file`, and
`bash`: in **ask** mode you are prompted before the script runs (the prompt
shows the script, so you can read what is about to execute), in **auto** mode
it runs unprompted, and in **plan** mode it is denied like every other
mutating tool.

## The script environment

Scripts run in Monty, a sandboxed Python subset — not CPython. The dialect the
model is told to write (and the engine supports):

- No classes, no `match` statements.
- No imports beyond `asyncio`, `json`, `re`, and `datetime`.
- No filesystem or network access from the script itself — sub-agents do that
  work with their own tools.
- `async def` helper functions, `await` at top level, comprehensions,
  `try`/`except`, and `asyncio.gather(...)` all work; the script body already
  runs inside an event loop, so `asyncio.run(...)` must not be used.

Only three names are injected beyond that subset:

- **`agent(...)`** — spawn a sub-agent and return its report (details below).
- **`log(message)`** — surface one short progress line to the user in the
  TUI. Synchronous, not awaited, returns `None`. It is a progress channel
  only, never the result channel.
- **`args`** — the value passed in the tool call's `args` parameter. The
  model is told to pass structured inputs here rather than interpolating big
  data into the script text. (A stringified-JSON `args` that parses to a
  dict or list is decoded for the script automatically — a common model
  serialization slip; scalars and plain text pass through verbatim.)

The script's **last expression** is the tool's result. It must be plain data
(dict / list / string / number); the engine JSON-serializes it, caps it at
24,000 characters, and spills anything larger to
`.marim/workflow-output/<tool-call-id>.json` in the workspace, returning the
head plus a pointer. A script that mistakenly ends with `print(result)`
evaluates to `None`, but the engine keeps a bounded tail of printed output and
returns it with a corrective note rather than throwing away work that may have
cost several sub-agent runs.

Before anything executes, the engine parses the script and runs Monty's static
type check against the declared host surface. Unresolved names — the classic
model-authored-script bug — and calls that misuse `agent()`'s return (for
example, indexing a plain-string report as if it were a dict) are rejected
with line/column diagnostics at zero sub-agent cost. Parse and validation
failures are returned as correctable errors; nothing runs.

### `agent()` in detail

```python
await agent(task, *, type="general", model=None, schema=None,
            max_output_chars=None, isolation=None)
```

Parameters mean exactly what they mean on `spawn_agent`:

- `task` — the sub-agent's assignment.
- `type` — `"explore"`, `"general"`, or a custom agent name from
  `.marim/agents/`.
- `model` — an optional explicit model slug override for this spawn.
- `schema` — an optional JSON Schema dict; see below.
- `max_output_chars` — a soft budget on the report; oversized reports are
  spilled to a file and replaced by a within-budget pointer.
- `isolation` — `"worktree"` runs the spawn in its own git worktree so
  parallel mutating spawns cannot clobber each other or the main tree.

**Return value.** Without `schema`, `agent()` returns the report as a plain
string. With `schema`, it returns parsed data (the decoded JSON object/array),
validated against the schema.

**Schema validation.** The schema itself is checked for well-formedness first
(`jsonschema`'s meta-schema check), so a broken schema fails fast instead of
burning a spawn whose report could never validate. Enforcement then rides the
spawn seam — object-rooted schemas on native spawns use structured output —
with engine-side validation as defense in depth: the report's JSON is
extracted (the whole report if it parses, else the first parseable fenced
```` ```json ```` block), then validated. On a mismatch the engine retries
**once**, re-issuing the task with the validation error appended and an
instruction to respond with only the corrected JSON. If the retry also fails,
`agent()` raises with a model-readable reason.

**Failures raise into the script.** A failed spawn or a validation failure is
an ordinary exception at the `agent()` call site — scripts wrap calls in
`try`/`except` to degrade gracefully (skip a dimension, substitute a default)
instead of losing the whole run.

## Budgets and safety

**Wall-clock budget.** Each `run_workflow` call may request its own budget via
the tool's `timeout_secs` parameter. Omitted requests get **300 seconds** —
short enough that an ordinary sweep fails fast. Long multi-agent work (a
multi-researcher deep-research run) requests more, and the request is clamped
to the harness ceiling — `MARIM_WORKFLOW_TIMEOUT`, default **1800 seconds**
(30 minutes). The budget is enforced twice with the same number: an outer
wait on the whole run, and the Monty VM's own duration limit, which counts
real wall-clock time (including time spent awaiting `agent()` calls) and is
the only thing that can stop a non-yielding compute loop. The VM also runs
under a 256 MiB memory limit. On timeout the tool returns
`Workflow timed out after Ns; in-flight sub-agents were cancelled.` and the
model can decide whether to retry with a bigger budget.

**Aborts never cancel the VM.** Cancelling the sandbox task outright crashes
the interpreter (a verified pydantic-monty limitation), so the engine winds a
run down through its host functions instead: on abort it sets a flag that
makes every in-flight and future `agent()` call raise `WorkflowCancelled`
into the script, cancels the in-flight sub-agent spawns, and then gives the
VM a short deadline (3 seconds) to finish on its own before abandoning it —
safe, because once its host calls have raised, the VM holds no external
resources. A script may catch `WorkflowCancelled`, but every subsequent
`agent()` call raises it again, so even a catching script winds down
promptly.

What you see when you interrupt a turn (Esc) mid-workflow: in-flight
sub-agents are cancelled, the workflow's card reports `workflow aborted`, and
the turn winds down as usual. There is no partial result and no resume — a
`run_workflow` call is all-or-nothing, and an aborted or timed-out run must
be issued again from the top. The only durable artifact a run leaves is the
spill file for an oversized result.

## A worked example

A script the model might write to review a diff on two axes in parallel,
validate each report, and aggregate — every construct here is supported by
the engine:

```python
# Parallel two-axis review of the working diff
import asyncio

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string"},
    },
    "required": ["findings", "verdict"],
}

async def review(dimension):
    try:
        report = await agent(
            "Run `git diff` and review the changes for " + dimension
            + " issues. Report findings and an overall verdict.",
            type="explore",
            schema=SCHEMA,
        )
        return {"dimension": dimension, "ok": True, "report": report}
    except Exception as exc:
        return {"dimension": dimension, "ok": False, "error": str(exc)}

results = await asyncio.gather(review("correctness"), review("performance"))
log("reviewed " + str(len(results)) + " dimensions")

{
    "findings": [f for r in results if r["ok"]
                 for f in r["report"]["findings"]],
    "verdicts": {r["dimension"]: r["report"]["verdict"]
                 for r in results if r["ok"]},
    "failed": [r["dimension"] for r in results if not r["ok"]],
}
```

Notes on why this is shaped the way it is:

- With `schema=`, `report` is parsed data, so `report["findings"]` is valid —
  and the static pre-flight would reject that same indexing on a schema-less
  call, where the report is a plain string.
- The tasks tell the sub-agents to read the workspace themselves (`git diff`)
  instead of pasting the diff into the script — sub-agents have their own
  tools, and large content in a task string wastes context.
- Each `review()` catches failures so one bad axis degrades the result
  instead of destroying the run.
- The script ends on a bare dict expression — the tool result — not
  `print(...)`.

## Relationship to sub-agents

`agent()` is not a separate spawning system: it delegates to the same
`SubagentRunner.run` that powers `spawn_agent`, through the
`services.run_workflow` seam. Everything in the sub-agents guide
([subagents.md](subagents.md)) applies to workflow spawns:

- **Tiering.** With no explicit `model=`, the spawn's model resolves through
  the normal tier machinery — the agent spec's `tier:` frontmatter, else the
  tool-reach default (read-only types tier cheap, mutating types high) —
  mapped through `MARIM_SUBAGENT_TIER_*`. `agent()` has no `tier=` parameter;
  `model=` is the per-call escape hatch.
- **Concurrency.** `asyncio.gather` may fan out as wide as the work needs; the
  runner's semaphore caps concurrent model runs downstream
  (`MARIM_SUBAGENT_CONCURRENCY`, default 8), queueing the excess.
- **Limits.** Per-spawn request budgets, report capping/spilling, and the
  nesting depth ceiling all apply — workflow spawns run at the workflow
  caller's depth + 1, under the same `SUBAGENT_MAX_DEPTH` ceiling.
- **Visibility.** Each workflow spawn streams into the sub-agents screen as
  its own card, nested under the workflow's card, alongside `log()` lines and
  the run's title (taken from the script's first comment line).

Workflow spawns are granted no MCP servers, and since `run_workflow` is a
main-agent-only tool, a workflow script cannot start another workflow —
fan-out nests through `agent()` alone, bounded by the depth ceiling.
