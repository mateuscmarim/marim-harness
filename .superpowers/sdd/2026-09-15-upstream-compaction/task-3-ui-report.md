# Task 3 UI / daemon report

## Changes

- Recognize both the legacy user-prompt summary marker and Pydantic AI's
  `Summary of previous conversation:\n\n` system-prompt marker.
- Replay upstream summaries as `SummaryWidget` instances while hiding unrelated
  system prompts.
- Carry optional `changed`, `summary`, `post_tokens`, and `stage` fields on
  `compaction.finished`; older payloads remain valid.
- Publish controller compaction metadata when present and preserve the two-argument
  callback contract.
- Render explicit summaries even when message counts remain equal. Explicit
  `changed=False` clears the spinner without finding a stale history summary.
- Update the remote context gauge only from `post_tokens`; `after` remains a
  message count.

## Verification

- `uv run pytest --no-cov -n 0 tests/test_compaction.py tests/test_server_client.py tests/test_server_host.py tests/test_app.py`
  — 335 passed, 4 dependency deprecation warnings.
- `uv run ruff check` over all owned source and test files — passed.
- `uv run pyright` over all owned source files — 0 errors, 0 warnings.

## Commit

- The implementation and this report are contained in the Task 3 UI commit.

## Concerns

- The metadata publisher intentionally guards a missing `last_compaction_details`
  attribute. The main Task 3 worker must populate that controller dictionary before
  invoking the existing callback, as specified in the brief.

## Fix round 1

The implementer restricted history-summary fallback to legacy callbacks where
`changed is None` and added a regression for explicit `changed=True, summary=None`
after clearing only. Ruff and formatting checks passed for the changed files.
After cleanup restored shared imports, root ran:

```text
uv run pytest --no-cov -n 0 tests/test_app.py -k compact
10 passed, 205 deselected in 2.72s
```
