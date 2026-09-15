# Output-limits qualification

Implementation: `89067d37`; independent-review proof improvement: `d6be53e0`.
Selected dependencies: core **2.43.0**, Harness **0.31.0**. No live provider calls.

## Repository checks

Ruff lint and formatting, pyright, and wheel/sdist build passed.

| Python | Non-live suite | Coverage |
| --- | --- | --- |
| 3.10.20 | 4,995 passed; 7 skipped | 95.36% |
| 3.12.13 | 4,995 passed; 7 skipped | 95.36% |
| 3.13.14 | 4,995 passed; 7 skipped | 95.38% |
| 3.14.7 | 4,995 passed; 7 skipped | 95.35% |

Command: `uv run pytest -m 'not live' --tb=short`. The 3.10/3.12/3.14
legs used separate `UV_PROJECT_ENVIRONMENT` paths and `--python`/`--frozen`.
The first full run exposed eight test compatibility failures; repaired the
instruction-recipe introspection, mocked build signature, and expected tool sets
without changing their behavioral assertions. The table records the green runs.
The subsequent C11 change strengthens only the literal-filter proof; it changes
no production code. Independent verification reruns the entire named proof set.
The strengthened retrieval proof also passed separately on 3.10/3.12/3.14 at
`d6be53e0`; the independent round-2 batch passed all 32 cases on 3.13.

Existing warnings include provider deprecations and old dictionary-history
fixtures; all runs exceed the repository's 90% coverage floor.

## Combined compaction scenario

A disposable detached worktree combined output `89067d37` with compaction
`bc051c6b95e048b84e359e95ecb5fc12b6368fec`. The source branches were not changed
or merged into master. This is qualification of that exact snapshot, not of
future changes to either branch.

Five conflict resolutions preserved both features:

- `pyproject.toml` and `uv.lock`: retained the output branch's qualified constraints;
  both branches already resolved the same actual release pair.
- `subagents/runner.py`: retained compaction's moved checkpoint, after clearing,
  and output's widened capability type plus captured output capability.
- `tests/test_builder.py`: retained the output branch's instruction unwrapping.
- `tests/test_config_seams.py`: retained compaction's equivalent shared unwrapping
  helper and both instruction-gating assertions.

The combined tree passed Ruff and pyright. The integration run passed **28 tests**:
27 output-policy cases plus [the combined scenario](integration_check.py).
The output-only `test_resume_after_compaction` was deselected because that branch
removes its old `compact_history` API; the combined scenario replaces that check
with the new `reduce_history` boundary.

The scenario produces five actual large tool results with distinct call IDs,
asserts both clearing and summarization stages run, saves and restores JSON
history, reads the original spill through a fresh session capability, removes the
file, and checks the missing-result response without another producer execution.
Its summarizer is a deterministic TestModel, so this establishes storage and
history integration rather than real-model summary quality.

Replay after resolving the same integration merge:

```bash
uv run pytest --no-cov tests/test_output_limits.py \
  .specs/features/upstream-output-limits/integration_check.py \
  -k 'not resume_after_compaction'
```

## Maintenance change

Production source: **161 lines added, 373 deleted; net 212 fewer lines**.
Marim no longer owns ordinary-output serialization, measurement, spilling,
preview formatting, fallback clipping, or the retrieval implementation.
Its integration retains session storage selection and media preservation.
Legacy pointer readers, producer collection bounds, and explicit report budgets
remain by the approved scope. Tests and planning/verification documents add lines
and are excluded from this production-source count.
