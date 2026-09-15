# Whole-branch final review

Scope: `281630a7..e9354dd1`, including the final valid-message rewind fixture corrections. Read-only production review; no production or test edits.

## Verdict

**Spec compliance: approved. Code quality: approved. No concrete high/medium findings.**

The implementation removes an actual maintenance responsibility: custom cutoff planning, summary-agent construction, stale-result rewriting, and remembered masked-call-ID state are deleted. The shared adapter delegates reduction to public upstream strategies. Remaining custom logic concerns invocation, model/usage integration, stage reporting, and the documented conservative repeated-ID exclusion guard.

## Integration checks

- Main compaction remains behind the controller/session coordination boundary, with no competing automatic main-agent capability. The measured-token gate can force direct reduction; manual focus reaches the summary strategy before fallback composition.
- Input history is retained until upstream reduction completes. Restructuring invalidates rewind points before persistence, including same-length replacements; clearing alone preserves indices. The existing dirty approval-history exclusion remains in force.
- The overflow-summary failure fix restores bounded captured-history flushing for exceptions and cancellation. The regression tests load the saved prompt and repaired pending tool result. Auxiliary spend is banked once and request limits are recomputed before retry.
- Recoverable summary failures use deterministic upstream fallback; cancellation and usage-limit exceptions propagate. Indicator cleanup runs in `finally`, and PostCompact follows a committed change.
- Builder/bootstrap/model-switch wiring supplies an isolated CLI auxiliary clone. Explicit summary models remain explicit; opaque strategy attribution uses the accepted unknown-model policy. Auxiliary ledger recording does not reuse main CLI backend metadata.
- Native spawns receive fresh upstream clearing capabilities after sanitizers and before checkpoint writes. Reduced histories survive consecutive requests and retries; recovery remains bounded, checks actual token reduction, and avoids clearing on pool contention.
- Live/local/remote UI metadata composes with persisted upstream and legacy summary readers. Explicit null summary metadata suppresses stale-summary replay for clear-only changes.
- Dependency integration uses public imports and the CLI-owned banner switch. No legacy active engine references remain. Final rewind fixture corrections use real ModelRequest/ModelResponse values and preserve the original rewind assertions.

## Independent verification

Ran:

```text
uv run pytest --no-cov -n 0 tests/test_compaction_adapter.py tests/test_session_upstream.py tests/test_run_failure_recovery.py tests/test_subagent_masking.py tests/test_subagent_retry.py tests/test_aux_model_clone.py tests/test_server_host.py tests/test_server_client.py tests/test_upstream_compaction.py
```

Result: **126 passed, 1 accepted xfailed**, 4 existing websocket deprecation warnings, 4.33 seconds. The xfail is the raw upstream repeated-ID defect, with guarded production behavior separately covered. Root owns full-suite, matrix, build, and live-provider availability reporting.

## Informational

- `src/marim_harness/session/compaction.py:1` still says “Inactive adapter”; it is now active. This is a documentation-only cleanup, not a release blocker.
- Live-provider acceptance remains unavailable under the reported local endpoint failure; deterministic tests do not establish provider-specific summary quality.

## Scoped follow-up — bc051c6b

Reviewed the complete one-line module-docstring change. The informational “Inactive adapter” note is resolved: the module now describes the active Marim integration accurately. No executable behavior changes and no new findings. **Approved; prior whole-branch approval stands.** No additional tests warranted for this documentation-only correction.
