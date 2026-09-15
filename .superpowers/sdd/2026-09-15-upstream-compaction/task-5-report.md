# Task 5 report

Cleanup commit: `0ea2cceb` (`refactor: remove legacy compaction engines`).

## Result

- Removed marim's custom history cutoff, summary-agent, and stale-observation
  walking algorithms. Retained the measured-token gate, rapid-refill breaker,
  transcript/title helpers, historical summary reader, and saved-pointer repair.
- Routed the shared message estimate through Harness's public
  `estimate_token_count`.
- Removed active `mask_min_chars` configuration and Settings UI. An explicitly
  set `MARIM_MASK_MIN_CHARS` is accepted for one release, ignored, and warned
  about once.
- Updated current configuration, session, sub-agent, embedding, SDK, architecture,
  environment example, and changelog documentation for upstream strategies,
  approximate retention, usage attribution, and clearing without new scratchpad
  copies.
- Preserved focused coverage for old summary formats, typed-return repair, and
  saved output pointers; active strategy behavior remains covered by
  `tests/test_upstream_compaction.py` and session integration tests.

## Verification

- `uv run pytest --no-cov -n 0 tests/test_docs_reference.py tests/test_offload.py tests/test_compaction.py tests/test_cli_startup.py`
  — 38 passed.
- `uv run pytest --no-cov -n 0 tests/test_logging.py tests/test_config.py tests/test_settings_screen.py tests/test_settings_env_helpers.py tests/test_subagent_masking.py tests/test_upstream_compaction.py`
  — 239 passed, 1 expected xfail, 1 existing Pydantic AI deprecation warning.
- Focused `uv run ruff check` — passed.
- Focused `uv run ruff format --check` — 11 files already formatted.
- Production inventory found no remaining calls to `ObservationMasker`,
  `mask_stale_observations`, `_plan_tail_start`, `compact_history`, or
  `make_summarizer`. The `mask_min_chars` name remains only in the one-release
  environment deprecation reader.

## Full-suite follow-up

Full integration testing exposed one TUI rewind test that stored plain strings as
model history. The upstream estimator correctly requires `ModelMessage` objects, so
the fixture now uses a `ModelRequest`/`ModelResponse` pair and retains the same
busy-state assertion. No permissive production fallback was added.

The remaining rewind tests that seeded the same invalid string histories were
updated in the same way to prevent status-timer races from hiding or exposing the
fixture error depending on timing.

- `uv run pytest --no-cov -n 0 tests/test_app.py::test_rewind_command_refuses_while_busy`
  — 1 passed.
- `uv run pytest --no-cov -n 0 tests/test_app.py -k rewind`
  — 5 passed, 210 deselected.
- `uv run ruff check tests/test_app.py` — passed.
- `uv run ruff format --check tests/test_app.py` — already formatted.
