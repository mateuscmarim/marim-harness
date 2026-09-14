# Phase 3 execution state

## Decisions

AD-001 (active, 2026-09-14): The approved Phase 3 spec owns scope. Public
lifecycle events use session.notice; backend compaction never invalidates
marim mirror history. No @internal protocol dependencies.

## Handoff

Feature: cli-lifecycle-parity. Spec approved 2026-09-14. Design and eight
implementation slices recorded. No application code changed yet. Branch:
feat/cli-lifecycle-parity. Execution clone is in the session scratchpad;
restore commits to the durable harness worktree before finishing.
Next: T1 ordered notice component and focused tests.
