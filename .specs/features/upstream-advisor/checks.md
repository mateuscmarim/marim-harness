# Upstream Advisor checks

Profile: light
Plan: `.specs/features/upstream-advisor/plan.md`
Approved plan: user “go on”, 2026-09-16, accepting the presented routing default.

31 checks in 4 outcome slices; 6 one-way doors; 0 blocking questions.
Status: C1–C31 ported and proven locally on Python 3.13.14; fresh independent port
verification is pending. Complete labels reflect the current local proof run.
Obligations and proof selectors remain frozen except the approved C26 adjustment.
All pytest proofs use deterministic fake providers; no paid-provider calls were made.

## Approved port adjustment

The user approved preserving current master's exact Harness 0.31.0 base pin and
core >=2.43,<3 range, and updating C26's wheel proof to package 0.11.0 before
implementation was transplanted. All other checks remain frozen. Current port
base: `fb558301d2def1f3c90011dbeec0ec262c6ec888`; prior branch evidence is historical.
The transplanted feature requires fresh full verification.

## Checks

### S1 - Portable upstream advice

**C1** - Complete — A runtime consultation executes upstream Advisor and returns `Check the rollback.` for `advisor(prompt="Review the migration")` (ADV-01, AC 1).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_runtime_uses_upstream_advisor`

**C2** - Complete — A FunctionModel executor with no native advisor support obtains one local advisor result (ADV-01, AC 2).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_non_native_executor_uses_local_advisor`

**C3** - Complete — Qualified and bare runtime advisor IDs use the configured source's returned Model, preserving its identity/settings, rather than upstream string inference (ADV-01, AC 3).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_runtime_preserves_model_source`

**C4** - Complete — The advisor receives completed prior messages and the explicit consultation prompt but zero calls from the current unresolved executor response (ADV-01, AC 4).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_runtime_forwards_completed_history`

**C5** - Complete — A turn with neither runtime nor explicit SDK advisor exposes zero advisor tools and no advisor guidance (ADV-01, AC 5).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_disabled_advisor_has_no_tool_or_guidance`

**C6** - Complete — Both Claude and Codex advisor models use an ephemeral read-only clone, with workspace cwd, no live session reference, and cleanup without closing the live parent (ADV-02, AC 6).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_cli_advisor_isolation`

**C7** - Complete — Both Claude and Codex main executors receive zero runtime advisor capabilities and cause zero advisor model builds (ADV-02, AC 7).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_cli_executor_omits_runtime_advisor`

**C8** - Complete — An explicit SDK Advisor with runtime advice disabled exposes exactly one logical advisor for a local model, a supported native mock, and a native-unsupported fallback (ADV-03, AC 8).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_explicit_sdk_advisor_composition`

**C9** - Complete — Explicit SDK plus runtime advisor configuration raises before the first executor/provider request, including runtime activation after construction (ADV-03, AC 9).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_duplicate_advisor_rejected_before_request`

### S2 - Stable turn and saved-session selection

**C10** - Complete — Saved model selects itself, saved `off` disables a configured default, and absent saved selection inherits the default on session open (ADV-04, AC 10).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_session_selection_precedence`

**C11** - Complete — A model switch or disable during an active turn leaves the original advisor choice on approval continuation, retry, and dictionary-output correction rounds (ADV-04, AC 11).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_selection_frozen_across_turn_rounds`

**C12** - Complete — The next turn uses the newly selected advisor or omits it after disable, with the identical main Agent object (ADV-04, AC 12).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_selection_applies_next_turn_without_rebuild`

**C13** - Complete — Changing advisor selection with dirty in-memory tool history writes the selected metadata while leaving the on-disk clean messages unchanged (ADV-04, AC 13).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_selection_persists_metadata_only`

**C14** - Complete — Direct `/advisor` selection/off, picker selection/off, and startup notices state next-turn timing; CLI-executor notices state unavailability; bare `/advisor` opens the picker (ADV-04, AC 14).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_ui.py::test_advisor_selection_notices`

**C15** - Complete — Loading and saving an old session retains its no-argument advisor call and original usage-trailer result verbatim (ADV-04, AC 15).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_historical_advisor_exchange_round_trips`

### S3 - Budgets, errors, cancellation and accounting

**C16** - Complete — Two parallel advisor calls in one executor response with `max_uses=1` make exactly one nested model request and return one limit response (ADV-05, AC 16).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_parallel_consultations_obey_request_cap`

**C17** - Complete — Two consecutive executor responses each requesting advice with `max_uses=1` produce exactly two consultations in one turn (ADV-05, AC 17).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_cap_resets_each_model_request`

**C18** - Complete — A deterministic run with three executor requests at 11 input/5 output and two advisor requests at 7 input/3 output records 5 requests, 47 input and 21 output tokens in turn and session totals, including across approval continuations (ADV-05, AC 18).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_advisor_usage_banked_once`

**C19** - Complete — A turn request limit of one permits the executor request and blocks its advisor request, with advisor call count zero, also when previous approval rounds consumed the budget (ADV-05, AC 19).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_advisor_shares_remaining_turn_limits`

**C20** - Complete — Provider failure and cancellation bank already-reported executor/advisor usage exactly once, without adding a separate advisor delta (ADV-05, AC 20).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_failed_or_cancelled_advice_banks_usage_once`

**C21** - Complete — An advisor provider exception propagates to normal turn error handling; persisted ordinary tool calls all have returns and the next turn can run (ADV-06, AC 21).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_advisor_failure_leaves_resumable_history`

**C22** - Complete — Interrupting a pending consultation finishes its cancellation/cleanup before another owner can acquire the session; repeated interruption does not abandon the child (ADV-06, AC 22).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_advisor_cancellation_retains_claim_until_cleanup`

**C23** - Complete — Runtime output limits 0, 512 and 1023 fail before a request; 1024 and 2048 are accepted. SDK use limits -1 and 0 fail; 1 and None are accepted (ADV-06, AC 23).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_advisor_bounds`

**C24** - Complete — Bootstrap environment values unset and `0` map the use cap to None, and `2` maps to 2 (ADV-06, AC 24).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_environment_use_cap`

**C25** - Complete — A mixed executor/advisor usage total with only partial billed-cost details is never `cost_is_exact=True`; known upstream estimated totals are used without repricing all tokens at the executor rate, and unknown cost is not reported as zero (ADV-06, AC 25).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_lifecycle.py::test_mixed_advisor_cost_is_not_falsely_exact`

### S4 - One engine and migrated surfaces

**C26** - Complete — A base-only package installation can import and run upstream Advisor without workflows/Monty; metadata requires Harness `==0.31.0` and core `>=2.43,<3` (ADV-07, AC 26).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_base_dependency_contract`
Proof: `uv run --isolated --no-project --with ./dist/marim_harness-0.11.0-py3-none-any.whl python .specs/features/upstream-advisor/base_install_smoke.py`
The smoke follows `uv build`, runs a deterministic installed-wheel advisor round
trip and asserts Monty is absent. It is a single executable check, not a test suite.

**C27** - Complete — Both historical import paths are identity-equal to `pydantic_ai_harness.Advisor`; upstream option defaults apply and retired `id`, `description`, `defer_loading` arguments raise constructor errors (ADV-07, AC 27).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_legacy_imports_are_upstream_aliases`

**C28** - Complete — Production advisor code contains zero custom consultation-agent, clipping retry, advisor counter, usage-trailer generator, or `services.advise` implementation references (ADV-07, AC 28).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor.py::test_no_legacy_advisor_engine`

**C29** - Complete — Live TUI events and saved-message replay put advice outside the collapsed ordinary tool group and preserve the advice text (ADV-08, AC 29).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_ui.py::test_advice_live_and_replay_standalone`

**C30** - Complete — Active advisor reference/SDK/TUI documentation covers all seven migrated behaviors: prompt argument, per-request cap, 1024-token minimum, next-turn switching, propagated errors, routing distinction and CLI-main limitation (ADV-08, AC 30).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_ui.py::test_advisor_documentation_contract`

**C31** - Complete — The settings form labels uses as calls per model request, refuses 1023 output tokens, and accepts 1024 (ADV-08, AC 31).
Proof: `uv run pytest --no-cov tests/test_upstream_advisor_ui.py::test_settings_advisor_bounds_and_labels`

## Coverage

| Set (size) | Member -> proof | Unproven |
| --- | --- | --- |
| Landing doors (6) | dependency C26 · prompt contract C1 · routing C3 · turn boundary C11 · budget/errors C16 C17 C21 C23 · SDK alias C27 | - |
| Construction places (3) | Harness C1 · HarnessBuilder C3 C8 · bootstrap C24 | - |
| Runtime model IDs (2) | bare C3 · qualified C3 | - |
| SDK execution (3) | local model C8 · native supported C8 · native unsupported fallback C8 | - |
| Auxiliary CLI backends (2) | claude-cli C6 C7 · codex-cli C6 C7 | - |
| Capability presence (3) | disabled C5 · explicit SDK C8 · conflicting runtime+SDK C9 | - |
| Session precedence (3) | saved model C10 · saved off C10 · unset C10 | - |
| Turn continuations (3) | approval C11 · retry C11 · dictionary correction C11 | - |
| Selection transitions (3) | off-to-model C12 · model-to-model C12 · model-to-off C12 | - |
| Selection UI outcomes (7) | command model C14 · command off C14 · command bare C14 · picker model C14 · picker off C14 · startup C14 · CLI unavailable C14 | - |
| Consultation outcomes (5) | advice C1 · cap refusal C16 · provider exception C21 · shared-limit exception C19 · cancellation C22 | - |
| Runtime max_tokens bounds (5) | 0 C23 · 512 C23 · 1023 C23 · 1024 C23 · 2048 C23 | - |
| SDK max_uses bounds (4) | -1 C23 · 0 C23 · 1 C23 · None C23 | - |
| Environment max_uses (3) | unset C24 · 0 C24 · 2 C24 | - |
| SDK legacy imports (2) | package C27 · module C27 | - |
| Retired SDK options (3) | id C27 · description C27 · defer_loading C27 | - |
| Cost outcomes (3) | partial billed C25 · upstream estimate C25 · unknown C25 | - |
| Rendering paths (2) | live C29 · replay C29 | - |
| Documentation changes (7) | prompt C30 · cap C30 · token minimum C30 · switch timing C30 · errors C30 · routing C30 · CLI limitation C30 | - |

Surface signatures map to C3/C23 (builder), C11–C13 (setter), C14 (command),
the environment checks C23 and C24, C1/C16/C19/C21 (tool outcomes), C27 (SDK aliases),
and C10/C13/C15 (stored selection). No HTTP status set or new persisted entity
exists in the approved plan. Existing daemon reads and remote-command rejection
retain their existing tests; this migration adds neither surface.

## Swept

- validation: C23, C24, C31
- failure modes: C20, C21, C22
- idempotency: C9, C18, C20; consultations are not replayed by a second Marim engine
- authorization: C6, C7, C19; existing runtime mode and session ownership remain authoritative
- concurrency: C11, C16, C22
- data lifecycle: C10, C13, C15; no new TTL or backfill
- dependency failure: C21, C23; use upstream retry/exception semantics
- state transitions: C5, C10, C11, C12, C14
- observability: C18, C20, C25, C29, C30

## Historical handoff (original feature branch)

One builder for S1–S4. Measured core source/UI/advisor-test reading is 535,381 bytes
/ 4 = approximately 133,846 tokens. Additional focused documentation/test analogues
are read as needed; avoid loading unrelated modules wholesale. This fits the default
150k reading budget as one cohesive advisor migration; the verifier is separate.

Feature base: `1eb9a8e6c26b1a68cb26d4a8ec54df065440512c`.
Proof status: C1–C31 complete in the migration commit containing this update.
The orchestrator owns independent verification and any additional Python matrix runs.
User's pre-existing modified `AGENTS.md`, untracked editor directories and compaction
plans must remain outside feature commits except a separately staged advisor paragraph.

Required final gates: ruff check, ruff format --check (present in current CI), pyright,
full pytest, uv build. Record actual Python versions exercised; do not claim remote
CI was run without a push. Independent verification uses all checks at profile light.

## Historical build evidence (original feature branch)

- Python 3.12.3: all named selectors ran across the three upstream-advisor test
  modules (57 parametrized cases), plus existing advisor wiring/provider coverage:
  142 passed. The final strengthened mixed-cost/default proofs passed separately.
- Focused matrix, independently run by the orchestrator in isolated locked uv
  environments: Python 3.10.20, 57 passed (7.79s); Python 3.14.7, 57 passed
  (8.73s). Both ran all three upstream-advisor proof modules with live tests
  excluded. These are focused local matrix runs, not the full remote CI matrix.
- Ordered gates: `uv run ruff check src tests`, `uv run ruff format --check src tests`,
  `uv run pyright`, then `uv run pytest -m 'not live' --cov-report=term`:
  5040 passed, 7 skipped, 95.37% coverage. The live marker was excluded explicitly
  to avoid provider/subscription use. Existing deprecation/fixture serializer
  warnings remain; advisor fixture background-autoname warnings were eliminated
  by disabling unrelated titling in those fixtures.
- `uv build` passed. C26's exact isolated-wheel command passed, executing both
  upstream and Marim-builder consultations and confirming Monty is absent.
- `validate_checks.py upstream-advisor` passed with the pre-existing warning that
  C26's standalone smoke is not a pytest selector.
- First full run found one stale bare-builder tool inventory expectation for the
  removed static advisor registration; migrated to the approved disabled contract.
  C29's new replay proof found and fixed advisor grouping on saved-history replay.
- Retired custom-engine tests (clipping retries, no-argument calls, per-turn cap,
  errors-as-text, old capability options) were replaced by the frozen proofs.
  Existing session persistence, provider inventory, and selection tests remain.
- No remote CI, paid provider smoke, push, merge or deployment was performed.

Builder boundary: S1–S4 complete; independent verifier to inspect the entire
feature base-to-HEAD diff and all 31 checks.
User decisions during build: none beyond the approved plan.
Abandoned behavior: only the custom consultation semantics explicitly retired
in the approved plan; no alternative engine remains.

## Historical verification fix round 1

The independent verifier found C12's next-turn proof observed tool availability
but never consulted the selected model. The strengthened proof now calls advice
on each enabled turn and asserts distinct `advice from old` / `advice from new`
results, with no advice after disable. It retains the Agent-identity and exact
tool-presence assertions for off→new, old→new, and old→off. Production is unchanged.

Python 3.12.3: ruff check, ruff format --check, and pyright passed; all 57 migration
proof cases passed in 4.96s. C12's implementation evidence is complete again;
independent scoped re-verification remains the orchestrator's next step. Concurrent
uncommitted Claude CLI changes were neither authored nor staged by this builder.

## Historical final acceptance (not acceptance of the port)

Independent light-profile verification passed all 31 checks at `35cae596` in
round 2; see `verification.md`. All 57 named proofs, build, and the exact
base-only wheel smoke were rerun independently. The completion validator exited
0 with no errors or warnings. The checks validator retains only its documented
C26 standalone-command warning; no obligation or assertion was waived.

Full offline quality gates (ruff check, format check, pyright, then pytest):

| Python | Commit | Full-suite result | Coverage |
| --- | --- | --- | --- |
| 3.12.3 | `4b95039f` production tree | 5040 passed, 7 skipped | 95.37% |
| 3.10.20 | `35cae596`, clean detached checkout | 5040 passed, 7 skipped | 95.35% |
| 3.14.7 | `35cae596`, clean detached checkout | 5040 passed, 7 skipped | 95.34% |

The final commit changes only C12's proof and evidence, not production. Its
57 migration cases additionally passed on Python 3.12.3 and independently on
3.12.13. Matrix commands used `uv run --isolated --locked --python <version>`
with `ruff check src tests`, `ruff format --check src tests`, `pyright`, and
`pytest -m 'not live' --cov-report=term:skip-covered`. Existing dependency and
fixture warnings remain; no live provider calls were enabled.

The first clean Python 3.10 full run timed out in existing
`test_harness_steers_image_into_open_cli_turn[codex]`; its focused rerun and the
entire unchanged suite then passed (53.95s). Clean Python 3.14 passed in 41.94s.
Earlier main-workspace Python 3.14 results were contaminated by concurrent,
unrelated CLI source/test edits and are not used as committed-feature evidence.
Those edits, the pre-existing AGENTS preference section, editor directories,
and compaction plans remain outside feature commits. Nothing was pushed.

## Current port build evidence and handoff

Base: `fb558301d2def1f3c90011dbeec0ec262c6ec888`, branch
`refactor/upstream-advisor`. Advisor-only patches from `12f99e53`, `570d5c9e`,
`4b95039f`, `35cae596`, and historical records from `0e8e9f9e` were transplanted.
The old underlying workflow migration was not ported. Original-workspace changes
were untouched. Master lesson records were preserved, not replaced by the old
branch's conflicting L-003; historical report identifiers remain historical.

Conflict resolutions retain master's exact dependency/lock files, its
`session_output_limits()` capability alongside `AdvisorLifecycle()`, and its
SystemPromptPart compaction-summary replay alongside standalone advisor cards.
The obsolete custom advisor engine was removed as approved. Changelog entry is
under Unreleased, not retroactively attributed to the current 0.11.0 release.
There were no additional behavior changes or obligation waivers. The only proof
adaptation is the user-approved C26 dependency literal/current wheel version.

Fresh ordered gates on Python 3.13.14, core 2.43.0, Harness 0.31.0:

- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: 594 files already formatted.
- `uv run pyright`: 0 errors, 0 warnings.
- `uv run pytest -m 'not live' --cov-report=term:skip-covered`: 5104 passed,
  7 skipped, 1 xfailed, 22 warnings in 59.69s; 95.44% coverage. Default parallel
  workers were retained; no paid-provider calls were enabled.
- `uv build`: built the 0.11.0 source distribution and wheel successfully.
- C26's exact isolated-wheel command above: passed upstream and builder advisor
  consultations, base metadata assertion, and Monty-absent assertion.
- `uv run pytest --no-cov -vv -m 'not live' tests/test_upstream_advisor.py tests/test_upstream_advisor_lifecycle.py tests/test_upstream_advisor_ui.py`:
  all 57 named parametrized cases passed in 3.13s.
- `validate_checks.py upstream-advisor`: 0 errors, only the known C26 standalone
  smoke-selector warning. `check_commit.py` accepted the Conventional message.
- `git diff HEAD --check`: passed. `pyproject.toml`, `uv.lock`,
  `quality-baseline.json`, `.specs/LESSONS.md`, and `.specs/lessons.json` are unchanged.

S1–S4 are complete locally. Independent light-profile verification of the complete
new base-to-HEAD diff remains required; the old PASS report does not settle it.
The orchestrator owns complementary matrix results, independent verification,
and the user-requested PR/babysitting. The builder did not push or open a PR.
