# Task 1 report: dependency baseline

## Dependency selection

- `pydantic-ai-slim[openai,google,mcp]>=2.43.0,<3`, locked to 2.43.0.
- `pydantic-ai-harness>=0.31.0,<0.32`, locked to 0.31.0.
- No harness extras were enabled.

## Baseline before upgrade

- Ruff: passed (`task-1-baseline-ruff.log`).
- Pyright: 0 errors (`task-1-baseline-pyright.log`).
- Pytest: 4,985 passed, 9 skipped; 95.34% coverage
  (`task-1-baseline-pytest.log`).

## Contract suite

`uv run pytest --no-cov -n 0 tests/test_upstream_compaction.py`:
3 passed, 1 strict xfail. The passing cases cover sequential distinct IDs, a parallel
tool round, typed `ToolSearchReturnPart` preservation, input immutability, recent-content
retention, and message JSON round trips.

Harness 0.31.0 has one confirmed upstream limitation: `ClearToolResults` groups tool
pairs globally by `tool_call_id`. When an ID is reused in separate completed rounds,
`keep_pairs=1` clears the newest return too. The strict xfail contains the minimal
reproducer; `task-1-contract-initial.log` records the raw failure. The migration ruling
is a bounded public wrapper that excludes affected tool names from clearing.

## Upgrade compatibility

- Pydantic AI 2.43 wraps registered instruction callbacks in `SourcedInstruction`.
  Tests now accept both the old direct-callable registration and the wrapped shape
  without weakening their registration and behavioral assertions.
- The post-upgrade pyright run remained clean.
- A full post-upgrade run made during Task 2's in-progress edits found only the five
  instruction-introspection regressions above plus Task 2's transient import/ruff
  failures. The five owned regressions pass after the compatibility adjustment:
  33 passed, 1 xfailed across the two affected suites and the contract suite.
- Pydantic AI 2.43's public `BANNER_ENABLED` defaults to true. Marim must disable it
  before Marim-owned agent runs so terminal/coding-agent stderr cannot interfere with
  TUI or headless output. This requires runtime wiring outside Task 1 ownership and was
  reported to the root agent.

## Logs

- `task-1-baseline-{ruff,pyright,pytest}.log`
- `task-1-contract-{initial,final}.log`
- `task-1-final-{ruff,pyright,pytest}.log` (captured while Task 2 was in progress)
