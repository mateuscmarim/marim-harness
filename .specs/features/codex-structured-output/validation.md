# Codex structured output: independent verification

## Validation: PASS — STRUCT-01 through STRUCT-07 verified

**Date:** 2026-09-16. **Verifier:** independent TLC sub-agent (author != verifier).
**Final diff:** `fb558301d2def1f3c90011dbeec0ec262c6ec888..f5c3fa10`, including typed output37920a16, trusted policy532c9000, and failure accountingf5c3fa10. Initial failure and its correction are preserved below.

The verifier followed TLC `references/validate.md`/`sub-agents.md`, repository AGENTS.md and coding-guidelines.md. All Python/test/build commands used uv. Only this validation report was edited in the real repository. No live provider calls, source/test edits, stashes or nested agents were used. The subsequent investigation of other CLI providers is explicitly deferred.

The inline execution plan in spec.md is the task/gate source; this small compatibility fix has no separate tasks.md/design.md. All three planned changes are implemented. Seven requirements now pass after one failure-accounting fix/re-verification cycle. Review-bot integration and live deployment remain separate, unclaimed gates.

Scratch evidence root: `/tmp/marim-1000/marim.dev-2c3541d274cd/20260916-221006-55e451/scratchpad/codex-structured-verifier/`. References to `scratch/test_edges.py` below mean the independent check at that location.

## Spec-anchored outcome evidence

| Criterion | Required outcome | Actual file:line assertion evidence | Assessment |
| --- | --- | --- | --- |
| STRUCT-01 typed request | Forward schema every turn; validate typed result | `tests/test_codex_structured_output.py:63`: `output == Report(summary="checked")`, parametrized streaming/non-streaming; `:66-68`: summary type string, required exactly `["summary"]`, additionalProperties False; `:90`: schema on every corrective turn. | PASS |
| STRUCT-02 progress/tools before final JSON | Exclude progress/tool text from result, retain callbacks | `tests/test_codex_structured_output.py:107`: exact Report after progress and command; `:108-111`: two callbacks, bash command `pwd`, result `/w`. Independent malformed-final checks reject fallback to earlier valid-looking progress. | PASS |
| STRUCT-03 success/validation failure | Preserve observed usage and existing bounded retries | `tests/test_codex_structured_output.py:64`: exact `(input,output,cached) == (12,5,3)` for both APIs; `:87-92`: corrected Report, two turns, schema and `summary`/`Field required` corrective feedback. `scratch/test_edges.py:36-38`: caller and observed ledger equal `(2,24,10,6)` for Agent.run, `(1,12,5,3)` for public run_stream validation failure, matching native API semantics. | PASS |
| STRUCT-04 plain text | Preserve text/tools/lifecycle/cancellation | `tests/test_codex_cli_model.py:106-109`: Hi there, unchanged provider/model, exact usage; `:376-379` and `:658-662`: cancelled/abandoned turn interrupted and next request succeeds. `tests/test_codex_lifecycle.py:113-118`: notice identity and `["before", "", "after"]` text order. | PASS |
| STRUCT-05 builder output | run_turn returns validated structured_output | `tests/test_codex_structured_output.py:73` builds with_output_type(Report); `:78`: `outcome.structured_output == Report(summary="checked")`, through real builder/adapter/transport and scripted server. | PASS |
| STRUCT-06 trusted overrides | Forward host policy, preserve mandatory isolation | `tests/test_codex_server.py:79-87`: exact policy `-c` pairs, plugins/apps disabled, app-server last. `scratch/test_edges.py:74-76`: conflicting true entries precede mandatory false entries. | PASS |
| STRUCT-07 observed usage | Every successful, failed or cancelled turn counted exactly once | `tests/test_codex_structured_output.py:121-125`: failed turn ledger `(1,12,5)`; `:129-133`: same-total successor yields `(2,12,5)` without double counting; `:152-157`: cancellation ledger `(1,12,5,3)`. `scratch/test_edges.py:135-136`: success ledger and response usage both `(1,12,5,3)` for typed/plain × streaming/non-streaming. `:150-152`: failure plus new-spend successor gives response delta `(18,6,5)` and cumulative ledger `(2,30,11,8)`. | PASS |

Payload/conjunction rule: every schema, result, activity payload, request count and usage claim above is checked by value, rather than merely checking that a call occurred.

## Edge cases and API precision

- Valid-looking JSON progress followed by malformed final JSON cannot succeed: independent `scratch/test_edges.py:16-41` exercises both public APIs and asserts UnexpectedModelBehavior plus exact request/token accounting.
- Buffered progress cannot become successful output during cancellation: `scratch/test_edges.py:58-64` checks CancelledError, no observed result, and turn/interrupt.
- Host overrides cannot take precedence over extension isolation: explicit conflicting-value assertions at `scratch/test_edges.py:74-75`.
- Successful streaming and non-streaming requests return the same exact object and observed usage (repository parametrized test).
- **Clarified API distinction:** public Pydantic AI `Agent.run_stream(...).get_output()` validates an exposed stream without the retry loop used by Agent.run. An independent native FunctionModel reproduces one vs two requests with retries=1 (`scratch/test_edges.py:82-99`). The orchestrator confirmed that the spec's equality refers to successful completed turns and that existing per-API validation semantics must be preserved. No custom streaming retry loop is warranted; this is not a regression. Invalid-stream observed usage is still banked exactly once.

All twelve independent edge/ledger/baseline checks pass. The initially failing provider-accounting check now validates the documented public ledger, while explicitly preserving the underlying caller limitation. The original stronger streaming-retry assumption was corrected only after independently reproducing upstream native-model behavior and obtaining the explicit interpretation; it was not silently weakened.

## Gate check

AGENTS.md requires lint → types → tests, then packaging. Independently executed:

1. `uv run ruff check src tests`: pass.
2. `uv run pyright`: zero errors/warnings/information (only tool-update notice).
3. `uv run pytest -n 4 ... -ra` with full configured coverage: **5079 passed, 9 skipped, 1 xfailed**, no unexpected failure; 107.44 seconds, Python3.13.14. **95.45%** coverage exceeds required90%. JSON/XML reports went to scratch.
4. `uv build --out-dir <scratch>/dist`: sdist and wheel built successfully.

The nine unchanged opt-in skips are four Codex live-provider cases, two OpenRouter live cases, and three other-provider live cases; no paid calls were authorized for this verifier run. The unchanged xfail is `test_clear_preserves_recent_result_when_call_ids_repeat`, documenting pydantic-ai-harness0.31.0's global tool_call_id grouping. They are not new skips or hidden failures. Live deployment review remains a separate integration gate.

Test integrity: independently collected **5081 cases** from archived base, versus **5089 final cases** (5079 + 9 + 1), delta **+8**. The complete test diff consists of the new structured-output file (including two ledger regressions) and one additive server test. The accounting fix extends imports and adds assertions; earlier assertion expressions remain unchanged. No existing test/assertion was removed or weakened; no coverage thresholds or baselines changed.

## Isolated discrimination sensor

Source archive at final532c9000 was copied into scratch, with actual module `__file__` asserted to lie below `<scratch>/sensor/src` before tests. The interpreter/dependencies came from `uv run --project <real-worktree> --no-sync`, while PYTHONPATH pointed at copied source/tests. This prevents the editable installation from accidentally testing real production code.

**Scratch baseline:** 101 passed across structured output, adapter, lifecycle and server tests. Six separate behavior mutants followed, one at a time:

| Mutation | Source | Actual detection |
| --- | --- | --- |
| Omit outputSchema | `config/codex_cli_model.py:428` | Both typed streaming/non-streaming schema assertions fail (2 cases). |
| Retain progress in final buffer | `config/codex_cli_model.py:727` | Both typed APIs reject polluted final JSON (2 cases). |
| Drop activity callback | `config/codex_cli_model.py:619` | Required bash call/result callback test fails. |
| Replace validation feedback with original prompt | `config/codex_cli_model.py:409` | Exact corrective field/error feedback assertion fails. |
| Omit trusted host overrides | `codex/server.py:296` | Exact argv policy assertions fail. |
| Omit mandatory extension isolation | `codex/server.py:297` | Required plugins/apps=false assertions fail. |

**Six injected, six killed, zero survived.** Each mutant produced pytest exit1 through behavioral assertions/validation, not import/syntax failure. `sensor-results.json` stores actual source positions/results; individual logs and `run_sensor.py` preserve reproduction details.

The mutated archive was discarded. Real `git status --porcelain` was **empty before and after**, compared byte-for-byte and saved in scratch. No index/source/stash mutation occurred. Only after that isolation proof did the orchestrator resume its next fix and this report get written.

### Final failure-accounting sensor

At final `f5c3fa10`, **108 scratch baseline tests passed** across adapter, structured output, lifecycle, server and turn suites. Four additional production mutants were injected singly:

| Mutation | Source | Detection |
| --- | --- | --- |
| Bank only completed turns | `config/codex_cli_model.py:523` | Failure and cancellation ledger tests fail (2). |
| Bank observed tokens twice | `config/codex_cli_model.py:523` | Both exact-total tests fail (2). |
| Do not advance thread baseline | `codex/turn.py:228` | Failure→successor no-double-count assertion fails (1). |
| Recompute an already-recorded snapshot | `codex/turn.py:221` | Failed-turn exact-total assertion fails (1). |

**Four injected, four killed; total ten/ten killed across this feature.** Final evidence: `scratch/ledger-sensor/{import-proof.txt,results.json,*.log}`. The source archive was removed; real porcelain was byte-identical before/after, containing only this already-untracked validation report. Report edits occurred afterward. No real implementation, tests, index or stash were changed.

## Code quality

The diff remains scoped to schema/profile forwarding, final-message buffering, retry feedback, private-server policy, supporting tests/docs. It reuses the public Pydantic AI native-output profile/schema transformer and existing app-server outputSchema transport, rather than constructing a second model loop. Model identity, provider, Codex tool ownership and subscription path are preserved. The helper owns one buffer state; the dispatch extraction keeps cyclomatic complexity within the existing lint gate. No unrelated refactor or future configuration framework was added.

Changed tests map directly to STRUCT-01 through STRUCT-07. Existing lifecycle/cancellation/tool-rendering tests remain the regression evidence for the unchanged text path. SDK domain behavior has criterion-level outcome assertions; no HTTP route is involved. This infrastructure compatibility change does not require interactive UI UAT. Runtime diagnostics added by the change contain image counts/history-state booleans, not prompts, schema payloads or credentials.

## Resolved accounting finding and public contract

The initial independent check scripted tokenUsage12input/5output followed by a failed turn and found caller RunUsage remained zero. An archived base reproduction proved the limitation predates this feature. Pydantic AI cannot bank a ModelResponse never returned; pretending that accumulator was repaired would be incorrect.

Commit `f5c3fa10` resolves the embedding obligation with documented cumulative `CodexCliModel.observed_usage`. `record_turn_usage` snapshots each turn once and advances its thread baseline; adapter finally blocks bank the snapshot even on error/cancellation. Exact success, failure, cancellation and subsequent-new-spend assertions pass independently. `scratch/test_edges.py:111-113` deliberately proves both sides: caller usage stays `(0,0)` on the failed request, while the public provider ledger retains12/5.

A fresh-model embedder must use this ledger as its sole Codex attempt accounting source, never add it to successful-response usage. Unreported provider usage remains unknowable. The review-bot consumer correction requires its own verification; this upstream PASS does not claim that bot integration or the live deployment already works.

**Remaining upstream implementation gaps:** none. No surviving mutants. The streaming API distinction is resolved by independent native-model evidence and explicit interpretation. Live typed review remains a separate integration gate.

## Lessons and closing gate

The initial failed accounting probe is a grounded signal. Lessons are routed to the orchestrator because this verifier owns only validation.md: preserve observed provider usage before terminal errors, and verify the documented public accounting boundary. The initial failure and final contract are retained here for that grounding. No lesson is needed for the resolved native API distinction.

Final `uv run python <tlc-skill>/scripts/validate_state.py codex-structured-output` must pass after this update. Independent upstream verdict is7/7PASS; review-bot integration, publication and live operation are separate obligations.
