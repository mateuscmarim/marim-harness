# CLI lifecycle parity tasks

Status: In progress under the approved spec. Execute with tlc-spec-driven.
Eight component-sized slices; independent delivery, telemetry and Android work
uses bounded workers under the standing delegation instructions. A fresh
independent verifier follows integration.
Each slice includes its direct wiring and tests so it is independently usable.
Local commits are authorized; remote publication is not.

## Test Coverage Matrix

Generated from AGENTS.md, pyproject.toml, .gitea/workflows/ci.yml, and samples
in test_external_cli_model, test_cli_activity_history, test_claude_cli_model,
test_codex_translate, test_server_host, test_session_view_replay, plus the
mobile mapper/repository tests and mobile AGENTS.md.

| Code Layer | Required Test Type | Coverage Expectation | Location Pattern | Run Command |
| --- | --- | --- | --- | --- |
| Parsers and ledger | unit | Exact spec outcomes, malformed input, identity/order | tests/test_*lifecycle*.py | uv run pytest --no-cov -n 0 tests/test_cli_lifecycle.py |
| Adapters and serve | integration | Fake wire success, failure, isolation, replay | tests/test_*lifecycle*.py | uv run pytest --no-cov -n 0 tests/test_lifecycle_delivery.py |
| Mobile mapper/repository | unit and integration | Room merge, old server compatibility, resync isolation | app/src/test | ./gradlew testDebugUnitTest |
| Full feature | integration | All obligations and relevant existing regressions | both repos | CI-order commands below |

## Gate Check Commands

| Gate Level | When to Use | Command |
| --- | --- | --- |
| Quick | Each pure component | uv run ruff check src tests; uv run pytest --no-cov -n 0 TASK_TEST_FILES |
| Full | Adapter and UI wiring | uv run ruff check src tests; uv run pyright; uv run pytest --no-cov TASK_TEST_FILES |
| Build | Final verification | uv run ruff check src tests; uv run ruff format --check src tests; uv run pyright; uv run pytest; uv build |
| Android | Android component | ./gradlew ktlintCheck detekt; ./gradlew testDebugUnitTest; ./gradlew :app:assembleDebug |

TASK_TEST_FILES is replaced by each task's listed new tests and touched existing regressions.
No existing tests may be deleted, skipped or weakened. Capture gate evidence
and assertion-to-requirement mapping in evidence.md before the task commit.

## Execution Plan

```text
T1 -> T2 -> T3 -> T4 -> T5 -> T6 -> T7 -> T8
```

## Task Breakdown

### T1: Ordered backend notice component

**What**: Deliver the ordered backend notice component and its required integration.
**Where**: `src/marim_harness/config/external_cli.py`
**Files**: config/lifecycle.py; config/external_cli.py; runtime/cli_activity.py; tests/test_cli_lifecycle.py; tests/test_cli_activity_history.py (paths under src/marim_harness unless otherwise stated).
**Depends on**: None
**Requirement**: LIFE-26, LIFE-28, LIFE-31, LIFE-32
**Reuses**: Existing activity/notice/Room seams described in design.md.
**Tools**: Repository shell and file tools; coding-guardrails, test-driven-development, verification-before-completion. No connector access required.
**Done when**:
- [x] Verified: notice identity, ordered metadata persistence, best-effort callbacks, unchanged model prose.
- [x] Relevant gate passes with exact test outcomes recorded in evidence.md.
**Tests**: unit
**Gate**: quick
**Commit**: `feat(cli): ordered backend notice component`

### T2: Claude lifecycle adapter

**What**: Deliver the claude lifecycle adapter and its required integration.
**Where**: `src/marim_harness/config/claude_cli_model.py`
**Files**: claude/lifecycle.py; claude/process.py; config/claude_cli_model.py; tests/test_claude_lifecycle.py; tests/fakes/fake_claude.py (paths under src/marim_harness unless otherwise stated).
**Depends on**: T1
**Requirement**: LIFE-01, LIFE-02, LIFE-03, LIFE-11, LIFE-12, LIFE-13, LIFE-14, LIFE-18, LIFE-19, LIFE-20, LIFE-21, LIFE-22, LIFE-23, LIFE-29, LIFE-33, LIFE-35
**Reuses**: Existing activity/notice/Room seams described in design.md.
**Tools**: Repository shell and file tools; coding-guardrails, test-driven-development, verification-before-completion. No connector access required.
**Done when**:
- [x] Verified: public system chunks, truthful compaction, shell identity, inventory replacement, fake transport continuation.
- [x] Relevant gate passes with exact test outcomes recorded in evidence.md.
**Tests**: integration
**Gate**: full
**Commit**: `feat(cli): claude lifecycle adapter`

### T3: Codex lifecycle adapter

**What**: Deliver the codex lifecycle adapter and its required integration.
**Where**: `src/marim_harness/codex/translate.py`
**Files**: codex/translate.py; codex/turn.py; codex/server.py; codex/collab.py; config/codex_cli_model.py; tests/test_codex_lifecycle.py (paths under src/marim_harness unless otherwise stated).
**Depends on**: T2
**Requirement**: LIFE-01, LIFE-02, LIFE-03, LIFE-04, LIFE-12, LIFE-16, LIFE-33, LIFE-34, LIFE-35
**Reuses**: Existing activity/notice/Room seams described in design.md.
**Tools**: Repository shell and file tools; coding-guardrails, test-driven-development, verification-before-completion. No connector access required.
**Done when**:
- [x] Verified: completed compaction, reroutes, retry notices, wrong-turn filtering and shared-server isolation.
- [x] Relevant gate passes with exact test outcomes recorded in evidence.md.
**Tests**: integration
**Gate**: full
**Commit**: `feat(cli): codex lifecycle adapter`

### T4: Session notice delivery and replay

**What**: Deliver the session notice delivery and replay and its required integration.
**Where**: `src/marim_harness/server/host.py`
**Files**: stream_events.py; server/schema.py; server/wire_events.py; server/host.py; interfaces/cli/headless.py; interfaces/tui/session_view.py; interfaces/tui/app.py; tests/test_lifecycle_delivery.py (paths under src/marim_harness unless otherwise stated).
**Depends on**: T3
**Requirement**: LIFE-05, LIFE-06, LIFE-07, LIFE-10, LIFE-15, LIFE-27
**Reuses**: Existing activity/notice/Room seams described in design.md.
**Tools**: Repository shell and file tools; coding-guardrails, test-driven-development, verification-before-completion. No connector access required.
**Done when**:
- [x] Verified: same-turn wire delivery, stderr separation, history/live deduplication, no mirror resync.
- [x] Relevant gate passes with exact test outcomes recorded in evidence.md.
**Tests**: integration
**Gate**: full
**Commit**: `feat(cli): session notice delivery and replay`

### T5: Backend inventory and telemetry surfaces

**What**: Deliver the backend inventory and telemetry surfaces and its required integration.
**Where**: `src/marim_harness/interfaces/tui/link.py`
**Files**: runtime/harness.py; server/http.py; server/client.py; interfaces/tui/link.py; interfaces/tui/commands.py; interfaces/tui/widgets/status_bar.py; tests/test_lifecycle_inventory.py (paths under src/marim_harness unless otherwise stated).
**Depends on**: T4
**Requirement**: LIFE-13, LIFE-14, LIFE-17, LIFE-22, LIFE-23, LIFE-24, LIFE-25
**Reuses**: Existing activity/notice/Room seams described in design.md.
**Tools**: Repository shell and file tools; coding-guardrails, test-driven-development, verification-before-completion. No connector access required.
**Done when**:
- [x] Verified: session detail snapshots, existing inventory views, built-in command precedence, verified invocation and no checkpoint writes.
- [x] Relevant gate passes with exact test outcomes recorded in evidence.md.
**Tests**: integration
**Gate**: full
**Commit**: `feat(cli): backend inventory and telemetry surfaces`

### T6: Android notice transcript component

**What**: Deliver the android notice transcript component and its required integration.
**Where**: `app/src/main/java/dev/marim/mobile/domain/TranscriptMapper.kt`
**Files**: mobile: domain/TranscriptMapper.kt; domain/TranscriptItem.kt; ui/transcript/TranscriptScreen.kt; tests for mapper and repository (paths under src/marim_harness unless otherwise stated).
**Depends on**: T5
**Requirement**: LIFE-08, LIFE-09, LIFE-10, LIFE-27, LIFE-28
**Reuses**: Existing activity/notice/Room seams described in design.md.
**Tools**: Repository shell and file tools; coding-guardrails, test-driven-development, verification-before-completion. No connector access required.
**Done when**:
- [x] Verified: Room-before-rendering, sequence and identity deduplication, ordered history metadata notes, no cache reset.
- [x] Relevant gate passes with exact test outcomes recorded in evidence.md.
**Tests**: integration
**Gate**: Android
**Commit**: `feat(cli): android notice transcript component`

### T7: Backend result stats component

**What**: Deliver the backend result stats component and its required integration.
**Where**: `src/marim_harness/stats/recorder.py`
**Files**: stats/types.py; stats/recorder.py; stats/ledger.py; tests/test_lifecycle_stats.py (paths under src/marim_harness unless otherwise stated).
**Depends on**: T6
**Requirement**: LIFE-29, LIFE-30
**Reuses**: Existing activity/notice/Room seams described in design.md.
**Tools**: Repository shell and file tools; coding-guardrails, test-driven-development, verification-before-completion. No connector access required.
**Done when**:
- [x] Verified: optional result details retained and older entries preserve exact totals.
- [x] Relevant gate passes with exact test outcomes recorded in evidence.md.
**Tests**: unit
**Gate**: quick
**Commit**: `feat(cli): backend result stats component`

### T8: Phase 3 acceptance verification

**What**: Deliver the phase 3 acceptance verification and its required integration.
**Where**: `docs/plans/cli-backend-parity-roadmap.md`
**Files**: docs/plans/cli-backend-parity-roadmap.md; docs/reference/serve-api.md; .specs/features/cli-lifecycle-parity/validation.md (paths under src/marim_harness unless otherwise stated).
**Depends on**: T7
**Requirement**: LIFE-01 through LIFE-35
**Reuses**: Existing activity/notice/Room seams described in design.md.
**Tools**: Repository shell and file tools; coding-guardrails, test-driven-development, verification-before-completion. No connector access required.
**Done when**:
- [ ] Verified: complete CI-order checks, capability dispositions, independent verifier and discrimination sensor.
- [ ] Relevant gate passes with exact test outcomes recorded in evidence.md.
**Tests**: integration
**Gate**: build
**Commit**: `docs(cli): phase 3 acceptance verification`

## Diagram-Definition Cross-Check

| Task | Depends On | Diagram Shows | Status |
| --- | --- | --- | --- |
| T1 | None | None | Match |
| T2 | T1 | T1 | Match |
| T3 | T2 | T2 | Match |
| T4 | T3 | T3 | Match |
| T5 | T4 | T4 | Match |
| T6 | T5 | T5 | Match |
| T7 | T6 | T6 | Match |
| T8 | T7 | T7 | Match |

## Test Co-location Validation

| Task | Code Layer | Matrix Requires | Task Says | Status |
| --- | --- | --- | --- | --- |
| T1 | Component and direct wiring | unit | unit | Match |
| T2 | Component and direct wiring | integration | integration | Match |
| T3 | Component and direct wiring | integration | integration | Match |
| T4 | Component and direct wiring | integration | integration | Match |
| T5 | Component and direct wiring | integration | integration | Match |
| T6 | Component and direct wiring | integration | integration | Match |
| T7 | Component and direct wiring | unit | unit | Match |
| T8 | Component and direct wiring | integration | integration | Match |
