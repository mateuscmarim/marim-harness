# Phase 3 execution state

## Decisions

AD-001 (active, 2026-09-14): The approved Phase 3 spec owns scope. Public
lifecycle events use session.notice; backend compaction never invalidates
marim mirror history. No @internal protocol dependencies.

## Handoff

Feature: cli-lifecycle-parity, approved 2026-09-14. Execute T1-T3 implemented
and focused gates passing. T4 delivery/child replay, T5 inventory/telemetry,
T6 mobile notices and T7 result stats are in integration. Branch in both
repos: feat/cli-lifecycle-parity. Harness execution clone is in the session
scratchpad; restore commits to durable harness worktree before finishing.
Next: finish integration tests, full CI gates, independent verifier, docs.
No publication, merge or deployment authorized. Preserve original untracked files.
