# Phase 3 execution state

## Decisions

AD-001 (active, 2026-09-14): The approved Phase 3 spec owns scope. Public
lifecycle events use session.notice; backend compaction never invalidates
marim mirror history. No @internal protocol dependencies.

AD-003 (active, 2026-09-16): Display transcripts and reduced model context
persist in one atomic session snapshot. History replay must not feed the model
or substitute archived token estimates for current context measurements.

## Handoff

**Feature**: preserve-session-transcript
**Where**: C1–C24 independently reverified PASS at `ad0b7b62` (light profile, scoped round 2).
**In progress**: PR #151; CI-specific complexity regression fixed with typed paired snapshots, full suite 5129 passed, source complexity restored to 57.
**Next step**: push the verified follow-up and babysit new-head CI/review without merging.
**Blockers**: no remaining local findings; review-bot rejects jobs with non-retryable `backend_rejected` on both PR #149 and PR #151.
**Uncommitted**: none after the accompanying implementation commit.
**Branch**: `fix/preserve-session-transcript`

## Previous handoff: cli-lifecycle-parity

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
