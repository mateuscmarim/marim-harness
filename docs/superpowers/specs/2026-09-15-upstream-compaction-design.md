# Upstream compaction and observation masking

**Date:** 2026-09-15
**Status:** Implementation in progress on `refactor/upstream-compaction`.
**Plan:** [Implementation plan](../plans/2026-09-15-upstream-compaction.md)

## Objective

Replace Marim's history-cutting, summarization, and observation-masking algorithms
with Pydantic AI Harness implementations. Reduce the behavior Marim must maintain
while retaining its session experience and recovery invariants.

This design supersedes the algorithm and output-retention choices in the
[July compaction design](2026-07-21-compaction-pipeline-design.md) for this migration.
It does not supersede the session persistence, permission, or cancellation rules.

## Release evidence

Checked on 2026-09-15:

- Marim locks `pydantic-ai-slim==2.28.0` and declares `>=2.28,<3`.
- `pydantic-ai-harness==0.31.0` requires `pydantic-ai-slim>=2.40.0` and Python
  `>=3.10`. Plan against Harness 0.31.0 and core 2.43.0, the releases inspected.
- Public imports provide `ClearToolResults`, `SummarizingCompaction`,
  `SlidingWindowCompaction`, `TieredCompaction`, `FallbackCompaction`,
  `CompactionStrategy`, `compact_now`, and both token estimators.
- An isolated Python 3.12 probe using these exact releases cleared three of five
  tool results with `keep_pairs=2`, left the input history unchanged, and round-tripped
  both cleared and summarized histories through `ModelMessagesTypeAdapter`.
- A `TestModel` summary via `compact_now(..., usage=usage)` incremented
  `usage.requests` by one. Its summary was a `SystemPromptPart` beginning with
  `Summary of previous conversation:\n\n`.

These probes establish API feasibility, not compatibility with Marim's full suite,
Python 3.10/3.14, real providers, or long-session behavior. No project dependency
files were changed during planning.

Sources:

- [Compaction documentation](https://pydantic.dev/docs/ai/harness/compaction/)
- [Harness 0.31.0 metadata](https://pypi.org/pypi/pydantic-ai-harness/0.31.0/json)
- [Core 2.43.0 metadata](https://pypi.org/pypi/pydantic-ai-slim/2.43.0/json)
- [Harness source](https://github.com/pydantic/pydantic-ai-harness)
- [Harness version policy](https://pydantic.dev/docs/ai/harness/#version-policy)

## Ownership boundary

| Responsibility | Owner after migration |
| --- | --- |
| Safe history cutoffs, old-result clearing, summary construction | Upstream |
| Summary failure fallback to deterministic trimming | Upstream strategies |
| Token estimation for compaction decisions | Upstream public estimators |
| Budget versus served context window; per-model discovery | Marim `ContextLimits` |
| Manual/automatic/forced invocation and rapid-refill breaker | Marim session controller |
| PreCompact/PostCompact, UI notifications, usage ledger | Marim integration |
| Persisting history, invalidating rewind points, dirty approval history | Marim session/runtime |
| Reading existing summaries and output pointers | Small Marim compatibility readers |
| Native subagent request-time clearing | Upstream capability |
| Overflow classification, bounded retry, CLI backends | Existing Marim runtime |

## Execution design

### Main session

Keep `TurnController._compact_and_invalidate` and
`SessionController.maybe_compact` as the single entry path. Introduce one small
`session/compaction.py` adapter that builds and invokes public upstream strategies.
Do not also register automatic main-agent compaction: two owners would duplicate
work and could rewrite history during an approval round without session coordination.

Normal automatic compaction uses `TieredCompaction` with optional
`ClearToolResults` followed by summarization with a sliding-window fallback.
Use the resolved absolute Marim threshold, including local-model window overrides.
Keep the outer measured-usage gate authoritative: a request measured over budget
must not become a no-op because a second, different estimate is below budget.
When that occurs, invoke the reduction stages directly, as on a forced request.

Manual and forced compaction invoke stages directly through `compact_now`.
`TieredCompaction` alone is insufficient here: it stops when its target is already
met, even when called through `compact_now`. Forward manual focus to the summary
strategy before wrapping it in a fallback; do not assume every composite forwards
`with_focus`.

Keep defaults of four recent tool pairs and twenty recent messages. These are
upstream retention targets, not a promise of exactly the old message boundaries.
Keep tool inputs intact (`clear_tool_inputs=False`). Preserve explicit user
constraints in the summary instructions; the exact eight-heading Marim format
does not need to survive.

The adapter returns new history, stage information, a restructure flag, and usage.
It does not persist, launch hooks, modify `Deps`, or emit UI messages. Commit the
result only after successful reduction. Bank incurred summary usage once even if
the summary fails or is cancelled. Cancellation and `UsageLimitExceeded` propagate;
neither should be swallowed by a broad fallback.

`compact_now` accepts usage accounting but no `usage_limits` argument. For bounded
mid-turn overflow recovery, construct a public `RunContext` with the remaining
turn limits and invoke the strategy's public `compact` method. Use `compact_now`
for between-turn/manual calls. This small invocation distinction preserves limits
without introducing another reduction algorithm.

### Construction and model switching

Replace internal `make_summarizer` callback wiring with an upstream summary
strategy. Proposed low-level config: `HarnessConfig.compaction_strategy` accepts a
`CompactionStrategy` or `None` (deterministic trimming only). Builder defaults
construct `SummarizingCompaction`; bootstrap passes environment-derived settings
through the builder. Document the migration from the unstable `summarizer=` override
instead of building a second summary engine to emulate arbitrary callbacks.

Pass a concrete auxiliary model into the session adapter. Continue using
`aux_model_for` on initial construction and `/model` changes, so summaries never
reuse a live Claude/Codex conversation. Default strategies inherit that context
model; explicitly configured upstream strategy models remain explicit overrides.

### Native subagents

Replace `ObservationMasker` with a fresh `ClearToolResults` capability per spawn.
Retain `MaskingPolicy.trigger_for` to budget against the spawned agent's own model.
Keep existing history sanitizers and checkpoint processing; verify their ordering
with the new capability and that persisted retries retain the reduced history.

Make the overflow-clearing helper asynchronous and invoke upstream clearing after
the existing history repair. Preserve the one-recovery-attempt bound and the
distinction between an oversized request and local-server pool contention.
Clearing does not execute any tool again. If no tokens can be reclaimed, surface
the original failure instead of announcing successful recovery.

## Proposed behavior changes

These choices deliberately trade exact historical behavior for less maintenance.
They are migration decisions, not changes already made to Marim.

1. **Use upstream summary structure and safe cutoffs.** Retain a small UI reader
   for both upstream system-message summaries and historical Marim user-message
   summaries. Never rewrite upstream summaries back into the old format on the
   model-facing path; that would interfere with incremental summarization.
2. **Clear stale outputs without writing new scratchpad copies.** Existing output
   pointers remain readable and are still revalidated on load. Existing tool-level
   offloading stays in place. Do not add `ToolOutputLimits` or another spill store
   in this migration; new clearing can lose historical output that cannot be
   reproduced. Exclude mutating tool results from routine clearing initially so
   the agent is not encouraged to repeat a write or command to recover its output.
3. **Retire the per-result character floor.** Upstream `min_clear_tokens` is a
   whole-pass savings threshold and is not equivalent to `mask_min_chars`.
   Remove internal use of that knob; accept the old environment variable with a
   one-time deprecation notice for one release and document the replacement behavior.
4. **Use upstream state persistence semantics.** Remove the custom set of masked
   call IDs after verifying actual consecutive requests and retry/resume behavior.
5. **Migrate low-level custom summarizer callbacks to strategies.** The builder's
   normal behavior remains configured; direct `summarizer=` callers receive explicit
   migration guidance to a `SummarizingCompaction` instance. No permanent dual engine.

## Invariants and migration checks

- Preserve valid tool-call/result pairing and typed framework results through JSON
  round trips. Include histories with repeated tool-call IDs and parallel tool rounds.
  Harness 0.31.0 clearing globally matches IDs and can clear a recent result when
  an older call reuses that ID. A small public clearing wrapper conservatively
  excludes tool names involved in duplicate call IDs for that pass. It delegates
  all clearing to upstream, logs the skipped reduction, and never rewrites IDs.
  This can reclaim less context; summaries or existing irreducible-overflow errors
  remain the fallback. Retire this guard once an upstream release passes the
  reproducer. Do not silently lose recent outputs or copy the clearing algorithm.
- No tool execution or permission expansion from the adapter; no whole `Coder` stack.
- Under-budget automatic compaction does not invoke the summary model.
- Manual `/compact <focus>` works below the normal threshold. A blocked manual
  PreCompact leaves history untouched; automatic hook block verdicts remain advisory.
- Mask-only changes retain rewind indices. Summarization/trimming invalidate
  checkpoints before persisting, including same-length history replacements.
- Empty history and irreducible oversized tails do not report success or enter an
  unbounded retry/summarization loop.
- Clear the progress indicator in a `finally` path on success, no-op, failure, and
  cancellation; emit PostCompact only for a committed change.
- Persist accepted history changes immediately, reset stale input-token measurements,
  and keep background saves from publishing dirty approval history.
- Preserve existing saved sessions, summary widgets, and pointer repair readers.
- Keep headless and TUI construction aligned and CLI startup imports lazy.

## Scope limits and completion

No approval-loop migration, StepPersistence adoption, workflow replacement, CLI
backend replacement, broad file-tool changes, or rewrite of the session store.
Retain the small thrash breaker and context-window discovery in this pass.

Completion means the main session and native subagents use upstream compaction;
custom cutoff, summary-agent, stale-output walking, and masked-ID machinery are
removed. The old `compaction.py` may retain transcript/title helpers and legacy
readers used by other subsystems. A retained reader is not a second active engine.

Roll out as independently tested dependency, main-session, and subagent changes.
Use source-control reverts for rollback rather than shipping an engine-selection
flag. Back up sample sessions before testing; upstream-format histories require
the compatibility reader to remain available if an algorithm switch is reverted.
