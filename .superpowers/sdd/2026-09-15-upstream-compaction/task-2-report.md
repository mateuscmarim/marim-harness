# Task 2 report: upstream reduction adapter

Implemented the inactive session reduction adapter in
`src/marim_harness/session/compaction.py`.

## Result

- `reduce_history()` composes the public upstream clear, tier, fallback, summary, trim,
  focus, token estimation, and out-of-run APIs.
- Automatic reduction stops as soon as the target is met. Forced reduction executes one
  bounded pass through its eligible stages.
- Only model/API and malformed-output errors fall back from summary to deterministic trim.
  Cancellation and usage limits propagate.
- `SafeClearToolResults` is a public generic capability, with
  `safe_tool_result_clearer()` as its public factory for future main/sub-agent reuse.
- Clearing excludes Marim's known mutating tools. If a call ID repeats, every tool name
  associated with that ID is excluded for the pass; upstream performs all actual clearing.
  The warning records only the number of excluded names and never result contents.
- The adapter has no production caller in this task.

## Verification

```text
uv run ruff check src/marim_harness/session/compaction.py tests/test_compaction_adapter.py
All checks passed!

uv run pytest --no-cov -n 0 tests/test_compaction_adapter.py
11 passed in 0.08s

uv run pyright src/marim_harness/session/compaction.py
0 errors, 0 warnings, 0 informations

git diff --check
clean
```

The first test run failed during collection because the adapter module did not exist,
confirming the intended red state before implementation.

## Known tradeoff

Repeated call IDs conservatively reduce reclaim for every involved tool name in that pass.
This preserves recent results under the upstream 0.31/core 2.43 ID-based clearing behavior.
