# Transition dynamic workflows to Pydantic AI Harness

**Date:** 2026-09-15

**Status:** Approved, implemented, and verified; see the plan's verification record.

**Plan:** [Implementation plan](../plans/2026-09-15-upstream-workflows.md)

## Recommendation

Replace Marim's `WorkflowEngine` with the public `DynamicWorkflowToolset` supplied
by Pydantic AI Harness's `DynamicWorkflow`. Keep a small integration around that
toolset and an agent adapter that dispatches through `SubagentRunner.run`.
Upstream owns script execution; Marim owns permissions, worker selection,
session accounting, output files, and terminal presentation.

Adopt upstream's named-agent script interface. Reimplementing the old arbitrary
`agent(..., schema=..., model=...)` host function would preserve much of the code
this migration should remove. Existing scripts need migration guidance, not a
second interpreter or a Python source translator.

This targets dynamic, model-authored orchestration. Pydantic Graph and durable
execution backends solve different problems; neither is needed for this change.
Code Mode executes tools and is a possible alternative, but would require careful
tool filtering and more compatibility policy to recreate the workflow surface.
The dedicated upstream workflow capability is the closer fit.

## Evidence and release selection

Discovery checkout: `master`, `281630a7`; core locked to `pydantic-ai-slim==2.28.0`,
Monty to `0.0.18`, and no Harness dependency. The active implementation is
`workflows/engine.py`, constructed by `runtime/harness.py::_build_workflow_engine`.

PyPI metadata checked on this date reports these latest releases:

| Package | Inspected version | Compatibility |
| --- | --- | --- |
| `pydantic-ai-harness` | 0.31.0 | Python >=3.10; requires core >=2.40.0 |
| `pydantic-ai-slim` | 2.43.0 | Proposed common baseline with the compaction migration |
| `pydantic-monty` | 0.0.23 | Required by Harness's `dynamic-workflow` extra |

**The Monty upgrade and engine switch must land together.** Monty 0.0.23 exposes
a pool/checkout/feed API. Its `Monty` constructor no longer accepts script text,
and `Monty.run_async` and `Monty.type_check` are absent. Upgrading Monty while
leaving Marim's engine active breaks workflows and `test_deep_research_skill.py`.

An isolated Python 3.12.3 environment with the exact versions above passed:

- Static rejection of a positional `task`, before any child dispatch.
- Two concurrent named calls through a public `WrapperAgent` test bridge.
- A two-call budget shared across successive calls on one per-run toolset.
- Deferred approval, zero dispatch before approval, and one dispatch after resume
  through a real `Agent` using `FunctionModel` and `DeferredToolResults`.
- Cancellation propagating while a cooperative fake child finishes its cleanup.
- A 10ms sandbox compute budget allowing a 30ms awaited child, and stopping a
  non-yielding loop in under three seconds.
- Native typed output entering the script as a dictionary.

The probe used public imports and fake workers, not Marim's real runner. It does
not establish complete runner, CLI process, Textual, packaging, or Python
3.10/3.14 compatibility. It is an API feasibility check, not a migration test run.
The planning probe is in the session scratchpad as `upstream_workflow_probe.py`;
implementation must turn these cases into permanent contract tests.

Primary references:

- [Dynamic Workflow guide](https://pydantic.dev/docs/ai/harness/dynamic-workflow/)
- [Harness 0.31.0 source](https://github.com/pydantic/pydantic-ai-harness/tree/v0.31.0/pydantic_ai_harness/dynamic_workflow)
- [Upstream executor source](https://github.com/pydantic/pydantic-ai-harness/blob/v0.31.0/pydantic_ai_harness/_monty_exec.py)
  (inspected to understand cleanup; do not import this private module)
- [Public toolset composition](https://pydantic.dev/docs/ai/toolsets/)
- [Harness release metadata](https://pypi.org/pypi/pydantic-ai-harness/0.31.0/json)
- [Core release metadata](https://pypi.org/pypi/pydantic-ai-slim/2.43.0/json)
- [Monty release metadata](https://pypi.org/pypi/pydantic-monty/0.0.23/json)

## Responsibility and deletion map

| Concern | After transition |
| --- | --- |
| Script compilation, static signatures, sandbox driving | Upstream |
| Concurrent dispatch, nested-workflow refusal, call budget | Upstream |
| Script retries, partial-result previews, printed-output envelope | Upstream |
| Pending host-call cleanup and sandbox worker lifecycle | Upstream, subject to integration gates |
| Role discovery/trust, grants, tiers, CLI backends, worktrees | Existing Marim runner |
| Approval rounds and resumable conversation persistence | Existing Marim controller |
| Workflow wall-clock deadline, settings switch, progress cards | Small Marim toolset wrapper |
| Typed catalog declarations and CLI report validation | Small Marim catalog/runner adapter |
| Final output cap and scratchpad pointer | Existing Marim spill primitives |

Delete `workflows/engine.py`, its VM task/abort state, host-function registry,
static prelude, print tail, and orchestration retry loop. Replace
`tools/workflow_tools.py`'s script executor registration and remove the old
`services.run_workflow` callable seam once the upstream toolset is wired.
Keep the `run_workflow` name in permission/tool-group metadata.

Keep schema validation helpers only where the CLI/string-report boundary still
needs them; move these to `subagents/output_schema.py` if that improves cohesion.
Do not delete `jsonschema` or helpers used by other callers without a reference
audit. Reuse the existing cap/spill utilities rather than retaining the old
engine's result-shaping algorithm.

## Integration design

### One registered toolset

Construct the catalog after the runner exists, through shared builder/harness
construction. Use `DynamicWorkflow(...).get_toolset()` and public toolset
composition. Register this toolset once; do not simultaneously register the old
function or the same `DynamicWorkflow` as a capability that contributes another
`run_workflow` tool. Embedders supplying a colliding workflow must get a clear
construction error.

Wrap approval outside the execution wrapper using `.approval_required()`, so the
existing auto/ask/plan resolver remains authoritative. Approval occurs before any
sandbox or child work. The execution wrapper checks live availability, establishes
the workflow ID/child sequence, applies a wall deadline, emits lifecycle events,
and caps the final output. Forward `for_run` correctly so upstream owns its own
per-run state. Use public `WrapperToolset` APIs; no subclassing private helpers.

Keep `MARIM_WORKFLOWS` and live settings. A disabled/uninstalled workflow can be
hidden from the next model request; a stale call must fail without dispatch and
give an install/enable hint. No unconditional imports of the optional package on
CLI startup. Enabling after launch should work when dependencies are available.

### Agent catalog and runner adapter

Upstream accepts `AbstractAgent`/`WorkflowAgent` entries, not a raw spawn callback.
A narrow public `WrapperAgent` adapter is justified by this gap. Its descriptor
agent supplies name/description/output schema; overridden `run` dispatches exactly
once through `SubagentRunner.run` and returns `AgentRunResult(output=...)`. It must
not introduce another model turn, build workers eagerly, or execute descriptor
agent tools. Verify the full public method contract with pyright.

Build default entries from the existing trusted role catalog. Derive readable
Python identifiers (`claude-deep` becomes `claude_deep`); retain the original role
name for runner dispatch. Resolve invalid/reserved names and collisions
deterministically, with a stable suffix where necessary. Test plugin-qualified
names, duplicate normalized names, and Python keywords. Never silently overwrite
an entry or broaden its role's grants.

Proposed small host configuration value: `WorkflowBinding(name, agent_type,
output_schema=None, isolation=None)`. Builder/config accepts optional bindings
alongside discovered defaults; bootstrap owns discovery. These fixed declarations
allow a typed reviewer or worktree-isolated writer without restoring per-call
worker configuration in the script. Models and tiers continue to come from the
existing role specs and live runner resolution. No new model-routing system.

For structured bindings, expose the schema through the descriptor, pass it to
the runner, and validate/decode the full report before returning a dict/list.
Avoid parsing capped report text or a worktree prose wrapper as JSON; explicitly
reject unsupported structured/worktree combinations until a raw-result seam is
available. Do not invent a second raw-result transport merely for this migration.
CLI reports still need boundary validation; failure raises a script-visible
error, with retries expressed in workflow code rather than a hidden respawn loop.

Move the shipped deep-research `FINDINGS` and `VERDICT` contracts into two fixed
bindings, backed by the existing `researcher` and `explore` roles, so that pipeline
retains validated data. Its new script calls `research_findings(task=...)` and
`verify_claim(task=...)`. Skip unavailable role bindings with an actionable
diagnostic. Validated custom schemas require host bindings; ordinary `spawn_agent`
remains available for ad hoc tasks but does not expose an output-schema parameter.

The tool wrapper provides invocation context to bridges with a scoped ContextVar,
reset in `finally`, so concurrent children inherit the correct parent ID. Keep UI
sequence numbers out of shared `Deps`. Recheck abandonment after an awaited UI
announcement before calling the runner. Rendering errors must not discard a
completed report. Headless callbacks remain optional.

### Accounting, limits, and cancellation

Set `forward_usage=False` and `inherit_model=False` for runner-backed entries.
The runner already records native and CLI spend in the session; forwarding usage
would risk double counting and overriding model resolution would bypass tiers.
Runner request/concurrency limits remain authoritative. Upstream's
`sub_agent_usage_limits` cannot magically govern a custom bridge or a CLI process;
do not advertise those limits unless the adapter actually forwards/enforces them.

Use upstream's 50-call default. It bounds bridge dispatches per Pydantic AI
`Agent.run`, not descendants or Marim's entire user turn. Upstream `for_run`
resets the counter, including on a new approval continuation. Accept and document
this scope; do not add a second counter to pretend it is a logical-turn ceiling.

Set an explicit 30-second **sandbox compute** cap and keep upstream's memory
backstop. Use `MARIM_WORKFLOW_TIMEOUT` (default 1800s) as the host **wall-clock**
deadline per script, including child waits. These are different budgets in Monty
0.0.23. Implement the outer deadline using Python-3.10-compatible cancellation
around upstream execution, only after real-runner cleanup passes the tests.

Upstream drains pending tasks, but its cleanup has no general deadline and raw
repeated task cancellation can interrupt it. Test native cancellation, CLI process
termination, queued children, an interrupt during UI announcement, and repeated
Ctrl-C. Do not carry the old Monty 0.0.18 workaround into the new API by analogy.
If cleanup cannot satisfy Marim's interrupt/resume invariant, keep the switch
unmerged and require an upstream fix/public hook or a narrowly justified adapter.

### Script and user-facing changes

| Existing behavior | Proposed behavior |
| --- | --- |
| `run_workflow(script, args, timeout_secs)` | Upstream `run_workflow(code)` |
| `agent(task, type=..., schema=..., model=...)` | Named `role(task=...)`, configuration in catalog |
| Injected `args` | Ordinary script literals; pass workspace paths/refs to workers for large data |
| `log(...)` streams custom progress | Child lifecycle/stream events provide live progress; `print` is captured output |
| Per-call 300s default/ceiling override | Configured wall deadline per workflow; default 1800s |
| Engine-specific None/print recovery | Upstream result/print envelope, including `{}` for empty result |
| Hidden schema respawn | Native runner validation; CLI validation error handled by script |

These compatibility changes were approved for the implementation.
Do not rewrite saved conversations or rerun historical scripts. Render both old
`script` and new `code` records. A deferred legacy invocation must receive an
actionable migration result with no side effects, and history must remain paired.
Update approval previews to show sanitized `code` as readable Python.

Upstream error previews help a model reuse completed results, but are not a replay
journal or an exactly-once guarantee. Test a script that fails after a completed
mutating child; the integration must not automatically re-execute that child.
The model may author a revised script, which remains subject to normal approval.

Retain final output offload at the existing 24,000-character threshold. Apply it
to the serialized upstream result envelope; preserve error/partial-result meaning
and return an absolute scratchpad pointer. Do not implement another print capture.

## Release gates and scope

### Implementation decisions from contract tests

- **Cancellation boundary:** Repeated raw `Task.cancel()` interrupts upstream
  cleanup when execution is wrapped in `asyncio.wait_for`. The adapter now waits
  on one execution task, cancels it once, and uses the existing
  `runtime.backend_jobs.drain_task` under an AnyIO shield to finish cleanup before
  releasing session ownership. Upstream still owns sandbox and pending children;
  no Monty internals or host dispatch loop are copied. Tests exercise direct and
  real-controller cancellation, including a second interrupt and queued workers.
- **Parallel errors:** Monty 0.0.23 does not catch a gathered worker failure in
  a helper's `try`/`except`; `gather(return_exceptions=True)` is unsupported.
  Accept upstream batch-error semantics. The shipped script preserves completed
  result previews and directs recovery toward missing work, without automatically
  replaying completed workers or treating incomplete verification as successful.
  Exact-script tests cover both research and verification failure paths.
- **Lazy registration:** Register a public per-run toolset factory. It explicitly
  forwards `for_run(ctx)` before applying outer approval because factories are
  resolved after the ordinary toolset traversal. Keep a gated unavailable hint
  when the group is enabled, including base installs and live disabled sessions.
- **CLI checkpoint:** Real-process cancellation exposed an existing one-message
  lag in Claude transcript checkpoints. Flush the final translated partial
  transcript during cleanup, guaranteeing process close even when saving fails.
  This narrow runner fix preserves the approved interruption/resume invariant.
- **Schema ownership:** Retain pure `workflows/schema.py` helpers because the
  builder also uses schema validation; remove obsolete result-shaping helpers.
  No schema retry loop or alternate execution engine remains.
- **Short deadlines:** Monty's initial compute slice can block the event loop
  before the outer wall timer fires. Set the compute cap to the lesser of 30s
  and the configured wall deadline. A regression proves a short deadline stops
  a non-yielding script; cancellation still waits for resource cleanup.
- **Result boundary:** Use Pydantic's public serializer before applying output
  caps so dates, bytes, and sets retain upstream tool-return semantics. UI failure
  detection recognizes the budget envelope (`error`, `last_error`, `completed`)
  rather than arbitrary report keys. Public-toolset regressions cover both;
  the exact budget-envelope shape is reserved for upstream status reporting.
- **Integration with v0.10.0:** Compaction and ordinary output limits landed on
  master before this PR. Retain their shared Harness 0.31.0 dependency; only Monty
  and its workflow extra remain optional. The shared output limiter also handles
  workflow returns, so a large workflow preview may acquire a retrieval handle.
  Preserve the original full-result file and pointer through both layers rather
  than introducing an exception to the new shared policy.

The plan below owns integration proof and removal. A release must preserve
approval, trust/grants, side-effect boundaries, valid persisted tool-call pairs,
worker accounting, and visible terminal completion on success/error/cancel.
No workflow journaling/replay, main CLI-provider replacement, broad subagent
migration, full upstream Coder adoption, or compaction implementation is included.

Coordinate core/Harness versions with the separate
[compaction proposal](2026-09-15-upstream-compaction-design.md). That migration can
upgrade core and add Harness without its workflow extra; the Monty upgrade belongs
to this cutover. A rollback must revert the engine switch and dependency lock
together. Keep historical transcript readers through rollback.
