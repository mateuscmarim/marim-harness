# Upstream Compaction and Observation Masking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Marim's compaction and observation-masking algorithms with upstream strategies and remove the superseded machinery.

**Architecture:** Keep session orchestration in `SessionController`, backed by a small adapter using Pydantic AI Harness's public compaction APIs. Native subagents use upstream clearing capabilities; existing session readers, permissions, recovery boundaries, and UI remain integrated by Marim.

**Tech Stack:** Python >=3.10, uv, Pydantic AI 2.43.0, Pydantic AI Harness 0.31.0, pytest, Textual.

**Spec:** [Upstream compaction design](../specs/2026-09-15-upstream-compaction-design.md). Read the proposed behavior changes before implementing.

**Status:** Planned; no migration code or dependency changes have been made. The isolated release probes recorded in the spec have passed; project compatibility remains to be tested.

## Global constraints

- Python `>=3.10`; no 3.11-only syntax. CI exercises Python 3.10, 3.12, and 3.14.
- Use `uv` for everything. Ruff line length is 100; cyclomatic complexity limit is 10.
- Verify in order: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- Initial dependency target: `pydantic-ai-harness>=0.31.0,<0.32` and
  `pydantic-ai-slim[openai,google,mcp]>=2.43.0,<3`, locked initially to 0.31.0/2.43.0.
- Use public APIs; do not import upstream `compaction._*` helpers.
- Preserve authorization, resumability, cancellation, and side-effect boundaries.
- No parallel engines, new durable runtime, or provider/CLI migration in this plan.
- User requirements outrank historical implementation details. Keep adapters small.

## File responsibilities

| Files | Responsibility |
| --- | --- |
| `pyproject.toml`, `uv.lock` | Compatible upstream releases; no unrelated extras |
| `src/marim_harness/session/compaction.py` (new) | Strategy construction/invocation and reduction result |
| `src/marim_harness/session/ctrl.py` | Threshold, hooks, breaker, commit, usage, model switching |
| `src/marim_harness/runtime/{builder,bootstrap,harness}.py` | Shared construction and low-level strategy configuration |
| `src/marim_harness/compaction.py` | Remove algorithms; retain legacy readers, transcript/title utilities |
| `src/marim_harness/subagents/{policies,runner,run_driver}.py` | Upstream capability, own-model threshold, overflow recovery |
| `src/marim_harness/subagents/masking.py` | Delete after both subagent paths migrate |
| `src/marim_harness/interfaces/tui/session_view.py` | Upstream and legacy summary rendering |
| `src/marim_harness/config/model.py`, `.env.example` | Deprecate character-floor knob |
| `tests/test_upstream_compaction.py` (new) | Public API and adapter contract checks |
| Existing session, provider, subagent, checkpoint, UI tests | Observable integration behavior |

## Task 1: Establish a compatible dependency baseline

**Files:** `pyproject.toml`, `uv.lock`, new `tests/test_upstream_compaction.py`.

**Deliverable:** The selected upstream releases run in Marim with a small executable
contract suite, before changing the active compaction paths.

- [ ] Record baseline results using the required ruff → pyright → pytest order.
  Keep pre-existing failures separate from dependency-upgrade regressions.
- [ ] Change the two dependency ranges to the exact ranges in Global constraints.
  Regenerate the lock with uv and confirm the selected versions are 0.31.0/2.43.0.
  Do not install harness extras such as CLI, workflows, browsers, or durable engines.
- [ ] Add this representative fixture and first contract test to the new test file:

```python
import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.compaction import ClearToolResults, compact_now


def tool_history():
    history = [ModelRequest(parts=[UserPromptPart("Inspect the project.")])]
    for index in range(5):
        call_id = str(index)
        history.extend([
            ModelResponse(parts=[
                ToolCallPart("read_file", {"path": call_id}, tool_call_id=call_id)
            ]),
            ModelRequest(parts=[
                ToolReturnPart("read_file", "x" * 4000, tool_call_id=call_id)
            ]),
        ])
    return history


@pytest.mark.anyio
async def test_clear_preserves_recent_results_and_serializable_history():
    history = tool_history()
    original = ModelMessagesTypeAdapter.dump_json(history)
    cleared = await compact_now(
        ClearToolResults(max_tokens=1, keep_pairs=2), history, model=TestModel()
    )
    returns = [
        part.content for message in cleared for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert returns[-2:] == ["x" * 4000, "x" * 4000]
    assert all(len(content) < 4000 for content in returns[:-2])
    assert ModelMessagesTypeAdapter.dump_json(history) == original
    encoded = ModelMessagesTypeAdapter.dump_json(cleared)
    assert ModelMessagesTypeAdapter.validate_json(encoded) == cleared
```

- [ ] Add a repeated-call-ID variant (reuse a tool ID in separate completed rounds),
  a parallel-round variant, and typed framework tool-result fixtures already covered
  in `tests/test_compaction.py`. Check retained recent contents as well as successful
  serialization. Do not treat JSON validity alone as proof of correct retention.
- [ ] Run `uv run pytest --no-cov -n 0 tests/test_upstream_compaction.py`.
  If a release violates a required invariant, keep the affected migration unswitched,
  save the minimal reproducer, and select a fixed upstream release before continuing.
- [ ] Re-run ruff → pyright → pytest after the dependency change. In particular,
  inspect custom provider clients, nested capture, streaming, and lazy CLI imports;
  core's new transitive SDK/client versions may affect them. Fix only upgrade-related
  compatibility failures. Check that the new first-run banner cannot corrupt JSON
  headless output or the Textual display; scope any suppression to Marim-owned runs.
  Commit the passing dependency baseline.

## Task 2: Implement and test the upstream reduction adapter

**Files:** New `src/marim_harness/session/compaction.py`,
`tests/test_upstream_compaction.py`.

**Interface:** Use the following small result and entry point. `summary=None` selects
upstream deterministic trimming. `model` is the concrete auxiliary model supplied by
the construction layer; the adapter does not resolve environment variables.

```python
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.compaction import CompactionStrategy


@dataclass(frozen=True)
class Reduction:
    messages: list[ModelMessage]
    stages: tuple[str, ...]
    restructured: bool


async def reduce_history(
    messages: list[ModelMessage],
    *,
    model: Model,
    summary: CompactionStrategy[None] | None,
    target_tokens: int,
    keep_messages: int,
    keep_pairs: int,
    clear: bool,
    force: bool,
    focus: str | None,
    usage: RunUsage,
    usage_limits: UsageLimits | None = None,
) -> Reduction:
    """Apply upstream strategies; the caller owns committing history and usage."""
```

- [ ] Start with adapter tests: under-budget normal reduction makes no summary
  request; clearing sufficient to reach budget skips summarization; `force=True`
  reduces eligible history even below budget; input history is unchanged on error.
  Reuse `tool_history()` and `TestModel(custom_output_text="Retained task summary")`.
  A representative forced case is:

```python
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.compaction import SummarizingCompaction
from marim_harness.session.compaction import reduce_history


@pytest.mark.anyio
async def test_forced_summary_runs_below_automatic_threshold():
    usage = RunUsage()
    result = await reduce_history(
        tool_history(),
        model=TestModel(custom_output_text="Retained task summary"),
        summary=SummarizingCompaction(max_tokens=1, keep_messages=3),
        target_tokens=1_000_000,
        keep_messages=3,
        keep_pairs=2,
        clear=False,
        force=True,
        focus="Keep the authentication requirement",
        usage=usage,
    )
    assert result.restructured
    assert "summary" in result.stages
    assert usage.requests == 1
```

- [ ] Run the new tests and verify failure before adding the adapter implementation.
- [ ] Construct `ClearToolResults(keep_pairs=keep_pairs, max_tokens=1,
  clear_tool_inputs=False)` and `SlidingWindowCompaction(keep_messages=keep_messages,
  max_tokens=1)`. Exclude Marim's registered mutating tools from routine clearing;
  use the existing tool-name groups and test `bash`/writes explicitly. Keep upstream
  placeholder text so it does not tell the model to repeat a mutating command.
- [ ] For automatic reductions, compose `TieredCompaction(target_tokens=target_tokens,
  tiers=...)`. For forced reductions, invoke clearing and the summary/trim stage
  directly using `compact_now`. Apply focus to a focus-capable summary strategy before
  placing it in `FallbackCompaction`. Use public `SupportsFocus` behavior.
- [ ] Configure fallback for recoverable model failures, including malformed summary
  output (`UnexpectedModelBehavior`), rather than `Exception`. Add explicit tests
  proving cancellation and `UsageLimitExceeded` escape without trimming.
- [ ] For bounded mid-turn recovery, use a public `RunContext(deps=None, model=model,
  usage=usage, usage_limits=usage_limits, messages=messages)` and invoke the chosen
  strategy's public `compact` method. Keep `compact_now` for calls without those
  limits; it has no `usage_limits` parameter. Verify a spent request budget prevents
  the summary request and that the parent request's reserved slot remains available.
- [ ] Determine actual changes from returned messages, not only their count. Mark
  a changed summary/trim stage as restructuring even when the count stays equal;
  clearing alone must not set the restructure flag. Never mark a no-op successful.
- [ ] Use upstream public estimators for trigger/reclaim checks. Keep one bounded
  stage sequence; an irreducible tail returns without another summarization loop.
  When composing stage reporting, use public strategy wrappers that delegate `compact`
  if needed; do not recreate cutoff/clearing logic or import private helpers.
- [ ] Run `uv run pytest --no-cov -n 0 tests/test_upstream_compaction.py`; commit the
  tested adapter. This commit adds no second active compaction path.

## Task 3: Switch the main session and summary display together

**Files:** `session/ctrl.py`, `runtime/{builder,bootstrap,harness}.py`,
`interfaces/tui/session_view.py`, `compaction.py` (under `src/marim_harness/`);
`tests/test_session.py`, `tests/test_turn_controller.py`, `tests/test_agent_checkpoints.py`,
`tests/test_compaction.py`, `tests/test_tui_compact_notice.py`, `tests/test_cli_startup.py`.

**Deliverable:** Automatic, manual, and forced main-session compaction use the
adapter; histories and summaries survive save/reload in headless and TUI usage.

- [ ] Add failing behavioral tests to existing session/controller fixtures for the
  following cases. Preserve fixture setup; replace assertions about exact historical
  cutoff indices with the specified outcomes.

| Case | Required assertion |
| --- | --- |
| Measured usage over threshold but text estimate below | Reduction is attempted directly; no contradictory second gate |
| Manual compaction with focus below threshold | Summary request contains focus; manual hook semantics preserved |
| Manual PreCompact block | Original history and persistence untouched |
| Mask-only change | Save occurs; rewind invalidation does not |
| Summary or trim, including same-length replacement | Rewind invalidation occurs before save |
| Summary failure | Deterministic fallback commits valid history; incurred usage retained |
| Cancelled summary | Original history remains; indicator clears; cancellation propagates |
| Usage budget exhausted | No fallback hiding the limit; usage accounted once |
| Model switch | Next summary uses the new auxiliary model; configured explicit strategy model stays explicit |
| No-op/empty/irreducible history | No misleading success, repeated summary, or PostCompact |

- [ ] Replace `HarnessConfig.summarizer` and its builder/bootstrap construction with
  `compaction_strategy` as specified in the design. Keep its imports lazy, using the
  existing lightweight config typing convention and a cast at construction if needed.
  `None` preserves deterministic-only construction; builder defaults supply an
  upstream `SummarizingCompaction` with the existing `keep_last_messages` setting.
- [ ] Pass the concrete auxiliary model into `SessionController`; update it through
  `aux_model_for` on `/model` changes. Remove `make_summarizer` from production
  construction. Keep titler construction and ephemeral CLI cloning behavior intact.
- [ ] Replace `_stage_mask`/`_stage_summarize` with one adapter call inside the existing
  outer threshold/hook/breaker flow. Use `force=True` for manual/overflow calls and
  measured-only threshold crossings. Do not register a second main-agent capability.
- [ ] Pass a fresh `RunUsage` accumulator into the adapter and bank its delta once
  through the existing session/stats mechanism, including error/cancellation paths.
  Keep it separate from already-banked main-run usage. Inspect the updated stats
  record to ensure summary-model cost attribution is not labelled as the main model
  when an explicit summary model differs. Pass the controller's remaining turn limits
  into the adapter on mid-turn overflow recovery, using Task 2's public RunContext
  invocation. Ordinary between-turn/manual compaction uses `compact_now`; no unsupported
  parameter is passed to it. Check remaining limits again before the parent resumes.
- [ ] Commit changed history through the versioned setter; invalidate checkpoints
  according to `Reduction.restructured` before persistence; clear stale measured input
  tokens. Preserve the approval dirty-history latch and existing rollback behavior.
- [ ] Put completion/cleanup notifications in a `finally` path. Emit logs containing
  trigger, strategy/stage, before/after token estimates, duration, and outcome; omit
  prompt, summary, and tool-output contents. Fire PostCompact only after a committed
  change, retaining its existing `micro`/`summary` stage vocabulary.
- [ ] Extend `summary_text` to recognize both the historical marker and the observed
  upstream marker. This is a version-tested UI compatibility reader, not an import of
  a private upstream constant. Replay recognized `SystemPromptPart` summaries as
  `SummaryWidget`; keep other system prompts hidden. Update live summary detection
  so a same-length replacement is visible. Preserve upstream history on disk unchanged.
- [ ] Add a save/load/render fixture for each summary format and a mixed-history
  fixture. Keep existing missing-scratchpad-pointer and typed-result repair tests.
- [ ] Run focused session, controller, checkpoint, UI and CLI startup tests, then
  ruff → pyright → pytest. Commit the integrated main-session replacement.

## Task 4: Switch native subagent masking and overflow recovery

**Files:** `src/marim_harness/subagents/{policies,runner,run_driver,masking}.py`;
`tests/test_subagent_masking.py`, `tests/test_subagent_retry.py`,
`tests/test_provider_errors.py`, `tests/test_context_limits.py`.

**Interfaces:** `MaskingPolicy` continues to resolve a spawn's threshold. Its factory
returns a fresh upstream `ClearToolResults` capability or `None` when disabled.
`SpawnRunDriver._shed_context` becomes asynchronous and receives the concrete spawn
model for `compact_now`; its caller awaits it.

- [ ] Add failing integration cases for two spawns with different models and repeated
  tool IDs, consecutive model requests, retry from reduced history, parallel tool
  results, disabled masking, and the original per-spawn budget selection.
- [ ] Replace the factory's `ObservationMasker` construction with upstream
  `ClearToolResults(max_tokens=trigger, keep_pairs=keep_recent,
  clear_tool_inputs=False, exclude_tools=...)`. Apply the same mutating-tool retention
  policy as the main session. No custom set of remembered call IDs remains.
- [ ] Broaden the runner's capability collection annotation from `ProcessHistory`
  to the appropriate public capability base. Add clearing after history sanitizers;
  verify checkpoint writes and captured failure history preserve valid reduced state.
- [ ] Change `_shed_context` and its caller to async. After `_resumable_history`, use
  `compact_now` with aggressive clearing (`keep_pairs=1`) and the actual spawn model.
  Return reduced history only when the upstream estimate decreases. Keep non-repeatable
  tool results excluded; an irreducible overflow may legitimately remain an error.
- [ ] Assert one overflow recovery attempt, no tool replay by the adapter, no recovery
  notice for a no-op, and no clearing on the pool-contention path. Keep the current
  transient-retry and CLI backend implementations.
- [ ] Compare outgoing histories across a sequence of requests: cleared prefixes stay
  unchanged between threshold crossings. Assert JSON round trips and recent-result
  preservation, including reused IDs; if upstream fails, follow Task 1's fixed-release
  rule rather than retaining a permanent parallel masker.
- [ ] Run `uv run pytest --no-cov -n 0 tests/test_subagent_masking.py
  tests/test_subagent_retry.py tests/test_provider_errors.py tests/test_context_limits.py`
  as one shell command. Delete `subagents/masking.py` once no production import remains,
  and commit the native-subagent replacement.

## Task 5: Remove superseded code and document the changed contract

**Files:** `src/marim_harness/compaction.py`, `session/ctrl.py`,
`config/model.py`, `runtime/harness.py`, `subagents/policies.py`;
`.env.example`, `docs/reference/configuration.md`, `docs/guides/sessions.md`,
`docs/guides/subagents.md`, `docs/embedding.md`, `docs/sdk/builder.md`,
`AGENTS.md`, `CHANGELOG.md`; related tests.

- [ ] Inventory remaining references before deletion:

```bash
rg -n 'ObservationMasker|mask_stale_observations|_plan_tail_start|compact_history|make_summarizer|mask_min_chars|MARIM_MASK_MIN_CHARS' src tests docs .env.example
```

- [ ] Delete custom cutoff/summary-agent/clearing algorithms and unused production
  exports. Keep the title/transcript helpers, thrash breaker, existing-pointer repair,
  and summary-format readers still used outside the migrated paths. Keep comments
  explaining why each compatibility reader remains.
- [ ] Route compaction estimation through public upstream estimators. Leave unrelated
  UI estimates alone unless they would now display a contradictory compaction budget.
  Remove `_measured_or_estimated` only after all callers use the new gate correctly.
- [ ] Remove active `mask_min_chars` wiring. Accept `MARIM_MASK_MIN_CHARS` for one
  release with one deprecation notice when explicitly set; retain its reference-doc
  entry marked deprecated so configuration-completeness checks remain meaningful.
- [ ] Document the strategy override replacing `summarizer=`, new summary format,
  approximate recent-pair retention, summary usage accounting, and absence of new
  scratchpad copies at clearing time. State that existing tool offloading and saved
  pointers continue to work. Preserve old-session repair tests.
- [ ] Update AGENTS architecture descriptions and add a changelog entry around the
  final implemented behavior. Do not rewrite historical design documents as though
  they described the new implementation; link this superseding design instead.
- [ ] Run `uv run pytest --no-cov -n 0 tests/test_docs_reference.py tests/test_offload.py
  tests/test_compaction.py tests/test_cli_startup.py` as one shell command. Confirm
  no production calls into a legacy compaction or masking engine remain. Commit cleanup.

## Task 6: Final acceptance and rollout

- [ ] Run the full local sequence, in this order:

```bash
uv run ruff check src tests
uv run pyright
uv run pytest
uv build
```

- [ ] Exercise Python 3.10, 3.12, and 3.14 through the existing CI matrix. Report
  results actually obtained; do not infer cross-version success from a 3.12 probe.
- [ ] With a copied session and the user's configured provider, perform a bounded
  headless task containing enough file reads to trigger clearing, then a TUI task
  using `/compact <focus>`, resume, and rewind. Use synthetic files, a deliberately
  small configured budget, and the existing usage limits to bound spend. Include a
  requirement early in the conversation and verify it remains usable after summary.
- [ ] Interrupt during a summary and resume. Verify no orphaned tool results, duplicate
  execution, lost usage accounting, or stuck compaction indicator. Run a native
  subagent through clearing and a deterministic injected overflow/retry scenario.
- [ ] Record changed files, removed algorithms, retained compatibility helpers, actual
  verification evidence, and any observed provider-specific limit in the final PR.
  If a live smoke cannot run, report it separately from automated results.
- [ ] Roll back a failing slice with source control. Preserve upstream-summary readers
  if sessions have already been written in that format. Do not introduce a permanent
  runtime switch selecting old versus new engines.

## Completion criteria

- [ ] Main-session and native-subagent compaction use the selected upstream APIs.
- [ ] Custom history cutoff, summary-agent, observation walking, and masked-ID state
  are removed; no legacy engine executes in production.
- [ ] Required session, permission, recovery, UI, and saved-history contracts pass.
- [ ] New configuration/behavior differences are documented and dependency changes
  pass the required checks.
- [ ] The result removes an owned maintenance responsibility; wrappers contain only
  Marim integration and do not reproduce upstream algorithms.
