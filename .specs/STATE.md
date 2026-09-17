# Phase 3 execution state

## Decisions

AD-001 (active, 2026-09-14): The approved Phase 3 spec owns scope. Public
lifecycle events use session.notice; backend compaction never invalidates
marim mirror history. No @internal protocol dependencies.

AD-002 (active, 2026-09-16): The approved upstream-advisor migration makes
`pydantic-ai-harness==0.31.0` a base dependency with core `>=2.43,<3`.
Runtime advice preserves Marim's configured model routing through upstream local
execution; explicit SDK capabilities expose upstream native/auto selection.
The workflows/Monty extra remains optional. Other upstream migrations must reuse
this dependency boundary instead of introducing another advisor implementation.

AD-003 (active, 2026-09-16): User approved an advisor-only port to current
master fb558301d2def1f3c90011dbeec0ec262c6ec888. Preserve master's existing
Harness ==0.31.0 base dependency and core >=2.43,<3 range; adapt AC26/C26 metadata
and base-wheel proof to package 0.11.0. Other advisor obligations are unchanged.
Do not port the old underlying workflow commit or replace newer master migrations.
The former advisor verification is historical; the transplanted tree must be
reverified before its requested PR and babysitting workflow.

AD-004 (active, 2026-09-16): Display transcripts and reduced model context
persist in one atomic session snapshot. History replay must not feed the model
or substitute archived token estimates for current context measurements.

## Handoff

### Upstream advisor — port verified, PR authorized

Branch `refactor/upstream-advisor` ports advisor-only commits from the original
feature onto fb558301d2def1f3c90011dbeec0ec262c6ec888. Independent full-diff
verification at 96daf5b0 passed all 31 checks, with 57 named advisor cases rerun,
build/base-only smoke passing, and completion validator exit 0. The report in
`.specs/features/upstream-advisor/verification.md` covers this port, not merely the
historical feature. Fresh full offline suites on Python 3.12 and 3.13 each passed
5104 tests, with 7 skipped and 1 expected failure. Python 3.12 line/branch coverage
is 95.4353%/89.6746%, above the unchanged quality thresholds. Existing master
lessons, dependency pins, and unrelated original-workspace edits are unchanged.
The user authorized publishing a PR and babysitting review plus CI; no merge is
authorized. This acceptance-record commit precedes publication.

### Preserve session transcript — prior handoff (preserved)

**Feature**: preserve-session-transcript
**Where**: C1–C24 independently reverified PASS at `ad0b7b62` (light profile, scoped round 2).
**In progress**: PR #151; CI-specific complexity regression fixed with typed paired snapshots, full suite 5129 passed, source complexity restored to 57.
**Next step**: push the verified follow-up and babysit new-head CI/review without merging.
**Blockers**: no remaining local findings; review-bot rejects jobs with non-retryable `backend_rejected` on both PR #149 and PR #151.
**Uncommitted**: none after the accompanying implementation commit.
**Branch**: `fix/preserve-session-transcript`

### Prior CLI lifecycle handoff (preserved)

Feature cli-lifecycle-parity: all eight slices complete and independent
verification PASS. All 35 LIFE requirements have evidence in validation.md;
three isolated regression faults were caught. Harness 4899 tests (9 opt-in
live skips),coverage 95.30%,lint/type/build gates pass. Android 624 tests,
lint and APK gates pass,commit 9154b24. Both repos use feat/cli-lifecycle-parity.

Durable harness worktree:
/home/mateuscmarim/Projects/marim.dev/marim-harness-cli-lifecycle-parity

User authorization covers local implementation/commits. Nothing pushed,
merged to main/master or deployed. Next roadmap work is Phase 4 Codex push,
plan and diff; it needs its own scoped task. Initial untracked mobile
instruction files/directories are preserved.
