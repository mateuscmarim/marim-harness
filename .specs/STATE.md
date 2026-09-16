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
**Where**: C1–C24 independently verified PASS at `f1a1aea9` (light profile).
**In progress**: local implementation and verification complete; PR publication and CI monitoring next.
**Next step**: publish `fix/preserve-session-transcript` and babysit CI/review without merging.
**Blockers**: no local correctness findings; review-bot currently rejects jobs with `backend_rejected` (observed separately on cancellation PR #149).
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
