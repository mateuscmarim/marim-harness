# Task 4 report: native sub-agent clearing and overflow recovery

Implemented native sub-agent history reduction with Pydantic AI Harness capabilities.

## Result

- `MaskingPolicy` retains per-spawn model threshold resolution and now creates a fresh
  `SafeClearToolResults` capability, or `None` when disabled.
- The runner orders sanitizers, upstream clearing, then checkpoint persistence. Saved
  checkpoints therefore contain the same valid reduced history sent to the model.
- Overflow recovery repairs captured history, invokes `compact_now` with aggressive
  `keep_pairs=1` clearing and the concrete spawn model, and resumes only if the upstream
  token estimate decreases.
- Mutating tool results and duplicate call IDs use the shared conservative exclusions.
- The legacy stateful `ObservationMasker` and `subagents/masking.py` were removed.
- Transient retries, pool-contention handling, one overflow attempt, usage accounting,
  and tool non-replay behavior remain intact.

## Verification

```text
uv run ruff check src/marim_harness/subagents/policies.py src/marim_harness/subagents/runner.py src/marim_harness/subagents/run_driver.py tests/test_subagent_masking.py tests/test_subagent_retry.py
All checks passed!

uv run pyright src/marim_harness/subagents/policies.py src/marim_harness/subagents/runner.py src/marim_harness/subagents/run_driver.py
0 errors, 0 warnings, 0 informations

uv run pytest --no-cov -n 0 tests/test_subagent_masking.py tests/test_subagent_retry.py tests/test_provider_errors.py tests/test_context_limits.py
100 passed in 1.21s
```

The first policy test failed with `AttributeError` because the legacy factory exposed only
`masker()`, confirming the intended red state before implementation.
