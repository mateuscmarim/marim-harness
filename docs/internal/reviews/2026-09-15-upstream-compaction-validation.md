# Upstream compaction migration validation

Status: implementation and review complete on `refactor/upstream-compaction`.

Design: [upstream compaction](../../superpowers/specs/2026-09-15-upstream-compaction-design.md).
Plan: [implementation plan](../../superpowers/plans/2026-09-15-upstream-compaction.md).

## Dependency baseline

Before the upgrade, lint and type checks passed; 4,985 tests passed with 9 skipped.
The isolated dependency commit `04c172af` also passed lint and type checks:
4,988 tests passed, 9 skipped, 1 expected failure, 95.33% coverage on Python 3.13.14.
That expected failure is the raw upstream repeated-tool-ID reproducer, described below.

## Integration acceptance

Repository lint, formatting (587 files), and Pyright passed. Full-suite results:

| Python | Passed | Skipped | Expected failure | Coverage |
| --- | ---: | ---: | ---: | ---: |
| 3.10.20 | 4,978 | 9 | 1 | 95.34% |
| 3.12.13 | 4,978 | 9 | 1 | 95.35% |
| 3.13.14 | 4,978 | 9 | 1 | 95.33% |
| 3.14.7 | 4,978 | 9 | 1 | 95.34% |

These were local runs on the same source. Python 3.13 used `uv run pytest`;
the three CI-matrix versions used isolated uv environments and eight pytest
workers with separate coverage reports. Hosted CI was not triggered.

The [final code review](2026-09-15-upstream-compaction-code-review.md) approved
spec compliance and code quality with no remaining findings, and independently
ran 126 focused passing tests plus the expected upstream-defect failure.
The final source change after suite startup only corrected a module docstring.

The migration deletes the custom cutoff, summarizer-agent, observation-clearing,
and remembered-ID algorithms. Production source has 274 fewer lines overall,
including the new adapter and the session/UI integration.

The first packaging attempt included temporary test virtual environments from
the task scratch directory and failed on their absolute interpreter symlinks.
Those finished execution artifacts were removed before the final build.
The final `uv build --python 3.12` passed, producing the source distribution and
wheel. The temporary task directory was removed; the implementation worktree
and branch are preserved for integration.

Live-provider verification is unavailable: the configured local `ornith-1.0-9b`
endpoint returned a connection error during availability probing. No cloud-provider
API keys were available. No live model request was made.

## Decisions and limits

### Integration after PR #143

Merged `origin/master` at `01dab16d` (including output-limit PR #143) into
the compaction branch at `6dc4355d`. Kept the qualified Harness 0.31.0 pin,
the compaction checkpoint after sanitizing/clearing, and session-owned output
storage. Shared instruction-registration test helpers remain in place.

Updated the output-limit resume test and combined integration artifact for
the final `ReductionOptions` API. The combined checks cover spilling,
clearing, summarization, serialization, resumed retrieval, and missing files
without rerunning the original producer.

- `uv sync --locked`: passed, retaining core 2.43.0 and Harness 0.31.0.
- Ruff lint/format and Pyright: passed.
- Focused integration checks: 73 passed, 1 expected upstream-defect failure.
- Full suite on Python 3.13.14: 5,011 passed, 9 skipped, 1 expected failure;
  95.40% coverage.
- `uv build`: source distribution and wheel passed.

### Migration decisions

- Work in an isolated branch/worktree to preserve the existing dirty checkout.
  Moving commits back to another checkout remains a source-control operation.
- Pydantic AI Harness 0.31.0 groups tool results by call ID globally. Reusing an ID
  across completed rounds can clear a recent result. The small public-API wrapper
  excludes involved tool names for that pass. This sacrifices some token recovery
  and can leave an overflow irreducible; it avoids maintaining a clearing algorithm.
  The raw defect remains a strict expected-failure test, alongside passing wrapper
  retention tests. A future dependency update must revisit the guard.
- The inactive adapter and dependency checks were developed concurrently with
  separate file ownership. This risked adapter rework if the release changed.
- Main-session integration and UI/server changes used an explicit metadata contract
  so attached clients receive the committed summary and token estimate. This adds
  optional wire fields; older clients and events remain supported.
- Suppress the upstream first-run banner through the public switch at CLI bootstrap.
  Generic builder construction leaves the process-wide setting alone. A different
  CLI construction path may require adjusting that boundary.
- Direct upstream summaries attribute usage to the explicit or inherited auxiliary
  model. Opaque custom composite strategies without an exposed model use `unknown`;
  they retain totals and available exact cost, but cannot provide a per-model split.
  This avoids silently attaching the main model's name or CLI metadata to that usage.
