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

## Handoff

### Upstream advisor — port verification pending

Branch `refactor/upstream-advisor` ports advisor-only commits from the original
feature onto fb558301d2def1f3c90011dbeec0ec262c6ec888. The original feature passed
31/31 checks at 35cae596, with report retained as historical evidence in
`.specs/features/upstream-advisor/verification.md`; that PASS does not cover this
port. Fresh builder evidence is in checks.md: Python 3.13.14 ordered lint, format,
type, full offline suite (5104 passed, 7 skipped, 1 xfailed), build, base-only smoke,
and all 57 named advisor cases passed. Independent full-diff verification remains
pending. The user requested a PR and babysitting after adaptation/reverification.
Nothing has been pushed by the builder. Existing master lessons are unchanged.

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
