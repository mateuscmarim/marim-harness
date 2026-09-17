# Codex structured output

Status: Authorized as a Marim compatibility fix for the review-bot migration.

## Problem Statement

The Codex model adapter ignores output schemas and mixes progress/tool text
with its final answer, so embedded typed reviews cannot complete reliably.

## Goals

- Preserve Codex's model, provider, tool loop, subscription and usage accounting.
- Support typed outputs through the existing app-server outputSchema field.

## Out of Scope

- Replacing Codex's internal loop or imposing native-model investigation caps.
- Changing plain-text turns or other providers.

## Assumptions & Open Questions

| Decision | Default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Delegation | Codex owns its tool loop | User explicitly accepts delegation through Marim | Yes |
| JSON transport | Native turn/start.outputSchema | Existing server transport and official app-server documentation | Yes |
| Result boundary | Last agent-message item after successful turn completion | Codex emits progress in separate items before its final answer | Replay tests and live probe required |

Open questions: none; live deployment is a separate integration gate.

## User Stories

### Typed embedding

1. WHEN an embedder requests a typed output THEN Marim SHALL send its schema on every Codex turn and validate the returned object. (STRUCT-01)
2. WHEN Codex emits progress or tool activity before the final JSON THEN Marim SHALL exclude that text from the typed result while preserving activity callbacks. (STRUCT-02)
3. WHEN a typed turn succeeds or fails validation THEN Marim SHALL preserve observed usage and the existing bounded validation retry behavior. (STRUCT-03)
4. WHEN a caller requests plain text THEN Marim SHALL retain its existing text, tool rendering, lifecycle and cancellation behavior. (STRUCT-04)
5. WHEN HarnessBuilder.with_output_type is used with Codex THEN run_turn SHALL return the validated structured_output. (STRUCT-05)
6. WHEN an embedder supplies trusted Codex configuration overrides THEN the private server SHALL forward them while retaining Marim's extension-isolation overrides. (STRUCT-06)
7. WHEN a Codex turn succeeds, fails or is cancelled after observed token usage THEN the model SHALL expose that usage exactly once in its cumulative observed_usage ledger. (STRUCT-07)

## Edge Cases

Malformed JSON must fail validation, never fall back to an earlier progress item.
Streaming and non-streaming requests must produce the same object and usage.
Cancellation must not turn partial progress into a successful structured result.

## Execution Plan

1. Fix native schema forwarding and final-message extraction in codex_cli_model.py,
   its profile constructor seam in external_cli.py, and regression tests.
   Gate: adapter, builder and lifecycle tests; lint and type checks.
   Commit: fix(codex): honor structured output in embedded turns.
2. Expose trusted server configuration overrides in codex/server.py and cover
   their argv and isolation ordering in test_codex_server.py. Document the SDK
   contract; run independent TLC verification before publishing the fix.
3. Preserve provider-observed spend in codex/turn.py and CodexCliModel even when
   no ModelResponse can be returned. Verify failure/cancellation and no double
   counting on subsequent turns. Commit fix(codex): retain failed-turn usage.

Assumption: the existing server transport already forwards outputSchema.
Success: real adapter/fake server round trips and a live review return a typed
object without changing the provider or stopping Codex's investigation early.

## Requirement Traceability

| Requirement ID | Evidence | Status |
| --- | --- | --- |
| STRUCT-01 | test_codex_returns_typed_output_without_progress_text | Implemented |
| STRUCT-02 | test_structured_activity_still_reaches_callback | Implemented |
| STRUCT-03 | test_codex_invalid_output_is_retried_with_schema and usage assertions | Implemented |
| STRUCT-04 | Existing test_codex_cli_model and test_codex_lifecycle suites | Implemented |
| STRUCT-05 | test_builder_validates_codex_structured_output | Implemented |
| STRUCT-06 | test_embedding_config_overrides_keep_extension_isolation | Implemented |
| STRUCT-07 | test_failed_turn_preserves_observed_usage_once and test_cancelled_turn_preserves_observed_usage | Implemented |

## Execution Evidence

The new typed-output test failed against v0.11.0 because progress/tool text was
parsed as JSON. The fix passes both streaming paths and the real builder.
The full suite passed 5075 tests (9 existing opt-in skips, 1 existing xfail),
95.44% coverage. Strict schema and corrective-feedback additions were checked
with the focused adapter/lifecycle/builder suite. Ruff and pyright pass.
All new assertions map to STRUCT-01 through STRUCT-05; no unrelated contracts
or implementation-shape assertions were added. Independent verification pending.

Step 1 committed as 37920a16. Step 2 adds the private-server configuration seam
needed to retain the review service's read-only container sandbox and exclusion
of PR instructions; extension isolation still takes precedence. The server
regression suite is the gate for this additive configuration field.

Step 3 preserves a cumulative public provider ledger because Pydantic AI cannot
bank a response that was never returned. Failed/cancelled usage is recorded once;
the bot uses this ledger exclusively for fresh Codex attempts. 75 focused
adapter/turn/lifecycle tests pass, including a failure followed by another turn
and cancellation after observed usage. Ruff and pyright pass. The verifier's
initial failure identified this additional obligation; re-verification follows.
