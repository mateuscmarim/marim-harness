# Dynamic workflows

Dynamic workflows let the main agent coordinate sub-agents with Python control
flow: parallel research, coverage checks, retries, and aggregation. Marim uses
[Pydantic AI Harness Dynamic Workflow](https://pydantic.dev/docs/ai/harness/dynamic-workflow/)
for script validation and sandbox execution. Workers run through Marim's existing
sub-agent runner, with its permissions, model selection, accounting, and UI.

## Enabling workflows

Install the optional extra:

```bash
uv add 'marim-harness[workflows]'
```

`MARIM_WORKFLOWS` defaults to `1`; set it to `0` to disable the tool. The TUI
Settings toggle applies live, including enabling after starting disabled when the
extra is installed. With the workflow tool group enabled, an unavailable workflow
returns an install/enable hint without starting workers, including stale calls.
Use `spawn_agent` when the extra is unavailable.

`run_workflow` is main-agent only. **Ask** mode requests approval before executing
any code or starting a child. The preview shows readable, sanitized Python.
**Auto** executes without prompting; **plan** denies the call. Main CLI providers
run their own tools, so this Marim tool applies to native main-agent sessions.

## Write `run_workflow(code=...)`

The tool takes one argument, `code`, containing a Python script. The tool's
catalog describes available named functions and their result types. Call them
with a keyword task, for example `await explore(task="Review the diff")`.
Role names become Python identifiers: `claude-deep` becomes `claude_deep`.
Use the advertised catalog for exact names, including collision suffixes.

Scripts support top-level `await`, async helpers, `asyncio.gather`, loops,
branches, and exception handling. They run in Monty's sandboxed Python subset.
Filesystem and network work belongs in the workers' tools. Give workers paths or
references for large inputs; put small inputs directly in script literals.

The final expression supplies the result. `print(...)` is captured in the final
response; it does not stream progress. Upstream formats results and captured
output, including an empty object for a script with neither. Marim caps the
serialized response at 24,000 characters and offloads larger output using its
existing spill mechanism, returning an absolute path to the full output in the
session scratchpad (or workspace fallback if the scratchpad is disabled).
The shared [tool-output policy](tool-output.md) can shorten this preview further;
use `read_tool_result` to retrieve the stored preview, then follow its absolute
workflow-result path for the original full output.

### Parallel review example

Send this script as the tool's `code` argument:

```python
# Review the working diff on two axes
import asyncio

async def review(dimension):
    report = await explore(
        task="Read git diff and review the changes for " + dimension
             + ". Report findings and an overall verdict."
    )
    return {"dimension": dimension, "report": report}

results = await asyncio.gather(review("correctness"), review("performance"))
print("Reviewed " + str(len(results)) + " dimensions")
results
```

Ordinary roles return report text. Typed catalog bindings return validated data.
The shipped deep-research pipeline uses `research_findings(task=...)` backed by
`researcher`, and `verify_claim(task=...)` backed by `explore`, when those roles
are available. Its script retains one coverage follow-up and adversarial claim
verification before the main agent writes the report.

### Fixed worker configuration

The host chooses each binding's role, optional output schema, and optional
worktree isolation through [`WorkflowBinding`](../sdk/builder.md#workflow-bindings).
Scripts choose from that catalog;
they do not supply per-call `type`, `model`, `schema`, or `isolation` overrides.
Model, tier, thinking level, grants, depth, and concurrency remain the runner's
responsibility. Custom bindings cannot broaden a role's permissions.

For typed bindings, native workers use the runner's structured output support.
CLI reports are decoded and validated at the bridge boundary. Invalid reports
raise into the script. Sequential calls can catch them with `try`/`except`;
parallel batches have the limitation below. The bridge does not silently spawn
another worker. Structured worktree
bindings are rejected until the runner can provide an unwrapped structured
report; capped report previews are not decoded as if they were complete JSON.

### Parallel failure limitation

With the selected Monty 0.0.23, a worker failure in `asyncio.gather` escapes
helper-level `try`/`except` and fails the batch. `return_exceptions=True` is not
supported. Upstream reports a correctable error with bounded previews of completed
peer results. Reuse complete results and issue a new call for missing work; do not
assume a failed batch had no effects or replay the whole batch automatically.
The deep-research skill follows this recovery path and never treats unfinished
verification as successful.

## Budgets and interruption

- **Wall time:** `MARIM_WORKFLOW_TIMEOUT`, default **1800 seconds**, bounds each
  script including child waits. The tool has no per-call timeout argument.
- **Sandbox compute:** **30 seconds**, separately from time awaiting workers.
  A shorter configured wall deadline also lowers this compute cap. Upstream
  supplies the sandbox memory backstop. Cancellation waits for worker cleanup,
  so the deadline is not a guarantee of immediate process termination.
- **Calls:** upstream permits **50 named worker calls per Pydantic AI
  `Agent.run`**, shared across workflow calls in that run. A new run resets the
  budget, including an approval continuation. This does not count descendants
  and is not a ceiling over the complete Marim user turn.
- **Workers:** the existing runner enforces concurrency, request, and depth
  limits. Its session accounting records usage once; workflow dispatch does not
  add the same child usage again.

Interrupting a workflow cancels in-flight workers and settles the workflow and
child cards. Upstream manages sandbox cleanup; the old Monty 0.0.18 rule against
cancelling VM tasks no longer applies.

Upstream errors can include completed-result previews that help the model revise
the script. Those previews are not a replay journal or an exactly-once guarantee.
A failure does not undo a completed child's edits, and the integration does not
replay those children automatically. A revised workflow is a new call subject to
normal approval. Saved conversations remain resumable; scripts do not resume
mid-execution.

## Migrating older scripts

| Old surface | Current surface |
| --- | --- |
| `run_workflow(script=..., args=..., timeout_secs=...)` | `run_workflow(code=...)` |
| `await agent(task, type="explore")` | `await explore(task=task)` |
| Injected `args` | Script literals or worker-readable paths |
| `agent(..., schema=..., isolation=...)` | Host-declared typed/isolated binding |
| `log(message)` | Child cards for live progress; `print(message)` for captured output |
| Default 300s, per-call override up to a ceiling | Configured 1800s wall deadline per script |
| Engine's special `None`/print recovery | Upstream result and printed-output format |

Old `script`/`args` records still render in saved transcripts and approval
previews. A pending legacy call receives migration guidance without executing.
Marim does not rewrite saved conversations, translate old Python, or replay
completed workflows.

For ordinary independent work, `spawn_agent` remains available. See
[Sub-agents](subagents.md) for runner configuration and role definitions.
