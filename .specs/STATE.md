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

AD-005 (active, 2026-09-18): User approved Pydantic AI's native Codex
subscription provider as opt-in, reusing upstream read-only Codex login with
documented refresh limitations. Retain CLI and defaults until a later decision.
Raise core to >=2.44.0,<3; preserve master's Harness ==0.31.0 pin. No Codex SDK
integration, API-key fallback, publication, or live subscription evaluation.

AD-006 (active, 2026-09-18): User approved upstream authentication failure timing:
missing/malformed/API-key-only credentials fail provider setup; rejected refresh
fails the active request. Both require login guidance and prohibit provider fallback.
This explicitly revises AC 9/C9's former requirement that all failures occur in a turn.

AD-007 (active, 2026-09-18): Claude CLI tier routing is Claude-only in this
slice. `claude-cli:<model>` is the qualified tier target shape for transparently
routing native agent roles; an explicitly authored non-native `backend:` remains
authoritative. A selected Claude target must fail visibly without native/API
fallback, and interrupted work resumes from its persisted effective backend/model
rather than current tier configuration.

## Handoff

### Codex subscription provider — complete, independently verified

Branch `feat/codex-subscription-provider`, base `8c5fa218` (master v0.13.0).
Plan approved 2026-09-18; 38 proof-backed checks, profile light. Implementation
ecebd71f and proof-strengthening 16bd7300 are complete. Independent round 2 at
16bd73009c5cafbe22f7c3117d8b0160e3fd0109 passed all 38 checks, reran 53 cases,
and resolved the round-one TUI result-evidence gap. Completion validator exits 0.
Plan/checks/evaluation live in `.specs/features/codex-subscription-provider/`;
new specs remain local under repository policy. C2/C9 honor approved AD-006.
No implementation decision remains outstanding. All 53 subscription cases and
85 relevant regressions pass (138 total); the TUI follow-up passed 172 cases.
The final auth and TUI additions change tests only. No fault injection claimed.
Full isolated suites on Python 3.10/3.14 (core 2.45) and Python 3.12 (core 2.44)
each passed 5257 tests, 7 skipped, 1 xfailed. Main Python 3.12/core 2.45 also passed
5257 tests, 7 skipped, 1 xfailed, with 95.55% coverage (90% gate).
Ruff lint/format, Pyright, and sdist/wheel build pass. Earlier concurrent suites
overloaded the host; isolated reruns pass without weakening tests.
Live evaluation is NOT RUN; current CLI/default selection remains unchanged.
The original checkout's unrelated edits are untouched. No push/deploy authorized.
This acceptance-record commit changes only STATE metadata after verified code.

PR preparation, 2026-09-18: user requested publication and babysitting, not PR
merge. Incorporated current master 416e9726 (release 0.14, dependency security
fix, server version response, and nested-subagent concurrency fix). C18's test
now observes upstream request-limiter admission in place of the removed _slot;
the concurrency and tool-grant obligations are unchanged. All 53 subscription
cases plus 42 affected concurrency/model/cancellation cases pass. The earlier
acceptance above is historical; current integration evidence lives in the
feature verification report and the PR's current-head CI/review.

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
