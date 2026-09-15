# Upstream workflows implementation plan

**Status:** Implemented and verified on Python 3.10, 3.12, and 3.14.

**Design:** [Discovery and transition design](../specs/2026-09-15-upstream-workflows-design.md)

## Outcome

`run_workflow` executes through Pydantic AI Harness. Marim's custom VM driver,
validation prelude, host dispatch loop, cancellation workaround, and print
capture are removed. Native/CLI workers still use the existing Marim runner.

The approved design defines the compatibility changes. Implementation remains
scoped to dynamic workflows and its runner/UI/persistence boundaries.

## Execution and dependency order

1. Establish integration contracts in an isolated dependency environment.
2. Build the catalog/runner adapter and toolset wrapper there.
3. Switch dependency, registration, UI, and shipped scripts together.
4. Remove superseded machinery and prove the complete feature.

Tasks may be separate development commits on the feature branch. **Do not release
or merge the Monty upgrade independently of the working engine replacement.**
The current engine cannot run against Monty 0.0.23.

Target `pydantic-ai-harness[dynamic-workflow]>=0.31.0,<0.32`, core
`pydantic-ai-slim[openai,google,mcp]>=2.43.0,<3`; initially lock Harness/core/Monty
to the inspected 0.31.0/2.43.0/0.0.23. Keep workflows optional. If compaction
already supplies base Harness, add its workflow extra to `[workflows]` and the dev
group without duplicating the base dependency unnecessarily. Retain `jsonschema`
where still used. Recheck newer versions rather than assuming private behavior.

## 1. Establish the contract suite

**Files:** new `tests/test_upstream_workflows.py`; existing workflow acceptance,
runner, approval, and persistence fixtures. Use a disposable dependency
environment until cutover; do not upgrade the user's active environment first.

- [x] Record baseline lint/type/test results and distinguish existing failures.
- [x] Turn the discovery probes into permanent public-API checks: static rejection
  before dispatch; fan-out concurrency; budget across tool calls; typed output;
  approval/resume; CPU cap vs awaited-child time; cooperative cancellation.
- [x] Prove budget reset on a new `Agent.run`, especially approval continuation;
  assert the documented per-run scope rather than a fictional per-user-turn cap.
- [x] Exercise upstream with a real Marim native runner using `TestModel` or
  `FunctionModel`, then fake CLI processes, without paid provider requests.
- [x] Test cancellation during queued spawn, UI announcement, native child work,
  CLI child work, and asynchronous cleanup; test a second interrupt.
- [x] Persist/reload histories after success, failure, denial, and cancellation.
  Verify every tool call has a matching return before the next provider request.

**Pass condition:** The necessary public seams exist and cancellation/resume works
through Marim, not only through a fake callback. An upstream cleanup defect is a
cutover blocker with a reproducer, not a reason to clone upstream internals.

## 2. Build fixed catalog bindings and the runner bridge

**Files:** new `workflows/catalog.py`, `workflows/agents.py` (under
`src/marim_harness/`); shared builder/config; narrowly scoped schema helpers in
`subagents/output_schema.py`; corresponding tests.

- [x] Introduce the small `WorkflowBinding` value described in the design; expose
  it through builder/config, keeping environment/discovery reads in bootstrap.
- [x] Build default entries from trusted discovered roles. Test deterministic
  aliases for hyphens, plugin namespaces, reserved identifiers, and collisions.
- [x] Implement a public `WrapperAgent` bridge with descriptor metadata and one
  `SubagentRunner.run` dispatch. Check the override against upstream's full public
  signature. No descriptor model request or direct subprocess launch is allowed.
- [x] Keep current grants/depth/MCP/trust, role model/tier/thinking resolution,
  and selected worktree behavior in the runner. Test both built-in native roles
  and Claude/Codex CLI role dispatch with fake process fixtures.
- [x] Pass fixed output schemas to the runner, validate/decode full reports, and
  expose the same output schema to upstream. Reject incompatible capped/wrapped
  structured reports explicitly; do not decode preview text as a valid report.
- [x] Add fixed `research_findings` and `verify_claim` bindings using the shipped
  deep-research contracts. CLI validation failures become script-visible errors;
  bridge calls do not automatically respawn.
- [x] Verify session usage and card usage are recorded once on success/failure/
  cancellation; keep `forward_usage=False`, `inherit_model=False` for bridges.

**Pass condition:** Both native and CLI fixtures execute the named functions with
unchanged worker authorization and accurate accounting. Typed pipeline reports
remain validated. Bridge construction itself performs no worker work.

## 3. Integrate upstream execution and cut over atomically

**Files:** new `workflows/integration.py`; `runtime/{builder,bootstrap,harness,deps}.py`
(`HarnessServices` lives in `deps.py`); `tools/{provider,names,workflow_tools}.py`;
`pyproject.toml`, `uv.lock`; workflow wiring tests.

- [x] Compose the public upstream toolset with an execution wrapper and outer
  `.approval_required()`. Remove the competing function registration. Assert
  exactly one `run_workflow` tool in the native main agent and none in children.
- [x] Preserve custom-tool/group composition and detect embedder collisions.
- [x] Establish invocation context/child IDs for UI callbacks without shared
  mutable per-call state. Forward `for_run` correctly.
- [x] Set the 30s sandbox compute cap; apply the configured wall deadline around
  upstream execution. Verify timeout cleanup with the tested runner path.
- [x] Keep availability lazy and live-switchable. Missing extra, disabled state,
  or stale queued calls dispatch no child and return a useful hint.
- [x] Reuse final-result cap/spill helpers on the upstream result envelope.
  Test large results, printed output, empty output, budget exhaustion, and retry
  previews without discarding the failure status or partial work.
- [x] Fail a script after a completed mutating child. Confirm the integration
  performs no automatic replay, and a revised model-authored call passes through
  normal approval. Do not promise exactly-once execution from result previews.
- [x] Replace `services.run_workflow` and `_workflow_runner` wiring with the new
  availability/integration ownership; keep builder and bootstrap aligned.
- [x] Update dependencies/lock and remove direct old Monty API uses in this same
  cutover. Confirm a base install remains usable without Monty.

**Pass condition:** A native main-agent turn can request approval, run the named
parallel workflow, display its result, and resume normally after cancellation.
No old-engine execution path remains selectable.

## 4. Migrate terminal presentation and shipped scripts

**Files:** `interfaces/tui/interactions/approval.py`, `interfaces/tui/stream_render.py`,
`interfaces/tui/{app,settings,settings_env}.py`, workflow stream/wire event consumers
as needed; `builtin/skills/deep-research/SKILL.md`; guide/config/SDK docs;
`tests/test_deep_research_skill.py` and workflow UI tests.

- [x] Render new `code` approvals with terminal-control sanitization; continue to
  display historical `script`/`args` records correctly.
- [x] Retain workflow start/finish and child streaming/grouping. Finalize every
  started card on success, retry, timeout, or cancel. UI exceptions must not make
  a successful child rerun. Remove reliance on synchronous script `log()`.
- [x] Ensure disabled/enabled settings and headless behavior agree with the new
  registration model. Live enable must not depend on startup being enabled.
- [x] Translate the deep-research reference to named typed functions and ordinary
  Python data literals. Use `print` only as captured output, not promised live
  progress. Retain its coverage/adversarial verification and spawn fallback.
- [x] Execute that exact reference script against fake upstream catalog agents;
  do not just search its text or compile it through the deleted engine prelude.
- [x] Document `code`, named functions, fixed schemas/isolation, missing `args`
  and `log`, configured wall timeout, compute cap, and per-Agent.run call budget.
- [x] A legacy deferred invocation returns migration guidance without effects;
  saved transcripts stay loadable and no completed workflow is replayed.

**Pass condition:** The documented pipeline actually runs, approval displays what
will execute, old sessions remain readable, and workflow/child cards settle in
both success and interruption scenarios.

## 5. Delete, verify, and release as one feature

- [x] Delete `workflows/engine.py` and obsolete engine errors/tests. Migrate
  essential behavioral tests before deletion; retire assertions for intentionally
  replaced script syntax, print/None policy, and Monty 0.0.18 timing semantics.
- [x] Remove old `_VALIDATION_PREFIX`, VM state/drain, host registry, hidden schema
  retry, result recovery, and direct `Monty(script, ...)` use. Remove orphan imports,
  callbacks, service aliases, and registration code. Preserve historical readers.
- [x] Audit remaining `workflows/schema.py` references, including builder schema
  validation; remove or relocate helpers without breaking non-workflow callers.
- [x] Update `docs/guides/workflows.md`, configuration/SDK references, architecture
  instructions, `.env.example`, and changelog. Update the docs index if routing
  changes. Do not rewrite historical design documents to imply they shipped.
- [x] Verify in repository order: `uv run ruff check src tests` → `uv run pyright`
  → `uv run pytest`. Run focused contracts during development; run the full suite
  for final cutover. Exercise Python 3.10, 3.12, 3.14 and `uv build` on 3.12.
- [x] Smoke-test native TUI and headless workflows, optional-extra absence, fake
  CLI workers, live settings, and saved-session resume. Verify restored sessions
  with a new tool schema produce a fixable result rather than broken history.
- [x] Review the deletion diff: Marim must no longer drive Monty or own a second
  orchestration loop. Remaining custom code must map to a concrete integration gap
  in the design and be covered by behavior tests.

**Done:** One upstream execution path; documented syntax transition; preserved
authorization/persistence/accounting; passing integration/CI checks; no superseded
engine. Roll back engine + dependency lock together if needed.

## Verification record

The implementation follows the design's recorded adjustments for gathered errors,
repeated cancellation, short wall deadlines, serialization, and CLI checkpoints.
The independent review's three findings were reproduced, corrected, and rechecked.

### Permanent behavioral coverage

- `test_upstream_workflows.py`: public sandbox validation, concurrency, budgets,
  compute/wall timing, cancellation, print/result envelopes, Pydantic value
  serialization, UI failure status, and output offload.
- `test_workflow_agents.py`: catalog identity/collisions, fixed structured reports,
  native/fake-CLI dispatch, grants/worktree arguments, usage, and UI cancellation.
- `test_workflow_cancellation.py`: real controller/native/Claude/Codex lifecycles,
  queued work, repeated interrupts, partial transcripts, process cleanup,
  completed-side-effect preservation, and saved-session reload.
- `test_workflow_resume.py`: success/failure/denial persistence, old deferred schema
  rejection without dispatch, and embedder tool collisions.
- `test_workflow_wiring.py`, provider, approval, settings, and existing App/serve
  tests: registration, approval/resume, live availability, and terminal events.
- `test_deep_research_skill.py`: executes the exact shipped script, including
  research/verification failures and recovery guidance without automatic replay.

### Checks and smoke runs

| Check | Result |
| --- | --- |
| Baseline Python 3.12 | Ruff/pyright clean; 4,987 passed, 7 skipped; coverage 94.95% |
| Final Python 3.12.3 | Ruff/format/pyright clean; 5,009 passed, 7 skipped; coverage 95.41% |
| Python 3.10.20 | Ruff/pyright clean; 5,009 passed, 7 skipped; coverage 95.41% |
| Python 3.14.7 | Ruff/pyright clean; 5,009 passed, 7 skipped; coverage 95.39% |
| Python 3.12 package build | Wheel + sdist built; wheel contains upstream integration/catalog and omits old engine |
| Base install | Fresh `--no-dev` environment has neither Harness nor Monty; approved fallback returns install guidance |
| Textual smoke | Actual upstream call through a bound `HarnessApp`; workflow and child cards both settle as done |
| Headless continuation smoke | One `run_turn` executes 49 + 2 workers across approval continuations, proving upstream per-run budget reset |
| Review recheck | Eight focused regressions pass for all three independent review findings |

Fresh Python matrix environments exposed an existing LSP factory test invoking
multilspy's npm installer. The test now stubs only runtime dependency setup while
still asserting actual TypeScript factory routing. This removes an unrelated
installation/network dependency from the test; no LSP production behavior changed.
The core upgrade also required replacing private instruction-list assertions with
public model-request observations, including formerly vacuous negative checks.

Commands used the locked dependency set, `uv run --no-sync`, disposable Python
matrix/base environments, and a writable temporary uv cache. Checks ran in the
repository order (ruff, pyright, pytest); `uv build --python 3.12` produced the
artifacts. Models and CLI peers in integration verification were local fakes;
no paid-provider requests were made. Historical transcript readers remain.

### Integration onto current master before PR publication

Reconciled with `1b5f9669` (v0.10.0), which already ships upstream compaction and
ordinary tool-output limits. Retained its shared Harness dependency and regenerated
only the Monty workflow dependency changes from master's lock. Preserved compaction
SDK docs/tests and public instruction observations; the latter now distinguish
prompt assembly from the output store's independent scratchpad lookup.

The new output-limit interaction regression dispatches a real typed native worker
with a 30,000-character report, verifies the script receives the complete report,
and retrieves the original JSON through the shared handle and inner file pointer.
The base-install check now correctly expects Harness to be installed for the
other capabilities while Monty stays absent and workflows stay unavailable.

Final integrated Python 3.12.13 verification: **5,034 passed, 9 skipped, 1 xfailed**;
branch-enabled total coverage **94.10%**. Ruff, formatting, pyright, and wheel/sdist
build passed. Complexity findings fell to 57 (current master baseline 60). The
original three-version results above precede integration; PR CI checks the new
combined tree across the supported Python matrix.
