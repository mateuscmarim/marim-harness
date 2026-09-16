# Upstream Advisor verification

**Verdict**: PASS
**Profile**: light
**Diff range**: fb558301d2def1f3c90011dbeec0ec262c6ec888..96daf5b02a7d97e815e27e5b22415cf308719ed8
**Round**: 1 - full port verification
**Verifier**: independent sub-agent `/root/advisor_port_verifier` (author != verifier)

31 of 31 checks have located evidence and fresh passing proofs. No grounded
failure was found. This report verifies the complete advisor-only port onto
current master; no verdict or proof result is carried from the original branch.

## Scope and sources

Verified the full 43-file base-to-HEAD diff, including production removals, new
integration, new and migrated tests, documentation, and feature records. Read the
approved plan, every frozen check, AGENTS.md and coding-guidelines.md. The approved
C26 adjustment preserves Harness `==0.31.0`, core `>=2.43,<3`, and targets the
current 0.11.0 wheel. No other obligation adjustment was observed.

All repository reads and proof/build commands used
`/tmp/marim-1000/marim-harness-309990f67170/20260916-213829-4a7aee/scratchpad/advisor-pr.Cq1uDS`.
HEAD was `96daf5b02a7d97e815e27e5b22415cf308719ed8`; its status was clean before
and after proof execution. The only verifier edit is this report.

The plan names no additional source explicitly as binding and contains no visual
design contract. Its cited architecture decision AD-001 and relevant current
source/documentation paths were inspected. The cited
[upstream Advisor documentation](https://pydantic.dev/docs/ai/harness/advisor/)
and [capabilities overview](https://pydantic.dev/docs/ai/capabilities/overview/)
were opened. The installed Harness 0.31.0 `advisor/_capability.py` and core 2.43.0
public per-run capability signature/composition were also inspected. The local
implementation delegates consultation, forwarding, caps and nested usage to those
APIs; its lifecycle wrapper owns cancellation/drain and cost marking only.

## Proof execution

Verified at `96daf5b0`. All named proofs were rerun independently in one batched
pytest invocation, with every one of the 57 parametrized cases individually shown
as `PASSED`. Tests used Python **3.13.14**, core **2.43.0**, Harness **0.31.0**,
and the repository's default 12 xdist workers. The exact base-wheel smoke selected
Python **3.13.14** in its separate isolated environment. No live or paid provider
calls were made.

```text
uv run ruff check src tests
All checks passed, exit 0

uv run ruff format --check src tests
594 files already formatted, exit 0

uv run pyright
0 errors, 0 warnings, 0 informations, exit 0

uv run pytest --no-cov -vv -m 'not live' tests/test_upstream_advisor.py tests/test_upstream_advisor_lifecycle.py tests/test_upstream_advisor_ui.py
57 passed, exit 0 (3.08s)

uv build
Built dist/marim_harness-0.11.0.tar.gz and dist/marim_harness-0.11.0-py3-none-any.whl, exit 0

uv run --isolated --no-project --with ./dist/marim_harness-0.11.0-py3-none-any.whl python .specs/features/upstream-advisor/base_install_smoke.py
Base-only wheel Advisor smoke passed; Monty absent, exit 0

git diff --check fb558301d2def1f3c90011dbeec0ec262c6ec888..96daf5b02a7d97e815e27e5b22415cf308719ed8
exit 0
```

The initial uv invocation could not write its default cache inside the sandbox;
the same commands then ran successfully with approved cache access. Pyright
printed an update-available notice. Neither is a product failure.

The orchestrator separately reported Python 3.12 full offline results: 5104 passed,
7 skipped, 1 xfailed; line coverage 95.4353%, branch coverage 89.6746%. Those are
attributed context, not this verifier's execution or a substitute for these proofs.

## Checks

All proof runs and assertion verdicts below are newly verified at `96daf5b0`.
Every named test was located with `rg -n` plus its assertion context and observed
as PASSED in the single batch above. All three proof files are added in this
feature diff. File names are repository-relative; abbreviated test names refer
to that batch, exit 0, including every listed parametrized case.
Adjacent literal parametrizations supply expected values where indicated; no
expected value requires tracing an external fixture.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | Upstream consultation returns the required advice | `test_runtime_uses_upstream_advisor` | `tests/test_upstream_advisor.py:34` — `assert tool.parameters_json_schema["required"] == ["prompt"]`; `:43` — `assert seen[-1].content == "Check the rollback."`; construction directly imports/returns upstream Advisor in `src/marim_harness/runtime/harness.py:1159` | PASS |
| C2 | Non-native executor obtains one local result | `test_non_native_executor_uses_local_advisor` | `tests/test_upstream_advisor.py:88` — `assert len(calls) == 1`; `:89` — `assert returns(h.session.history)[0].content == "local advice"` | PASS |
| C3 | Bare/qualified source model identity is retained | `test_runtime_preserves_model_source` (2 cases) | `tests/test_upstream_advisor.py:105` — `assert cap.model is advisor and cap.mode == "local"`; `:107` — `assert source.build.call_args.args == (model_id,)`; both literal IDs at `:93` | PASS |
| C4 | Completed history and prompt, no unresolved calls | `test_runtime_forwards_completed_history` | `tests/test_upstream_advisor.py:129` and `:130` assert `prior question`/`prior answer`; `:131` — `assert any(getattr(p, "content", None) == "Review the migration" for p in parts)`; `:132` — `assert not any(isinstance(p, ToolCallPart) for p in parts)` | PASS |
| C5 | Disabled means no tool or guidance | `test_disabled_advisor_has_no_tool_or_guidance` | `tests/test_upstream_advisor.py:140` — `assert "advisor" not in [t.name for t in info.function_tools]`; `:141` — `assert ADVISOR_GUIDANCE not in (info.instructions or "")` | PASS |
| C6 | Both CLI advisor clones isolated and cleaned | `test_cli_advisor_isolation` (2 cases) | `tests/test_upstream_advisor.py:161` — `assert self is not parent and self.ephemeral`; `:162`–`:164` assert workspace cwd, `"plan"` mode, absent session reference; `:174` — `assert len(used) == 1 and closed == used and parent not in closed` | PASS |
| C7 | Both CLI executors omit runtime advisor | `test_cli_executor_omits_runtime_advisor` (2 cases) | `tests/test_upstream_advisor.py:186` — `assert "advisor" not in [t.name for t in model_request_parameters.function_tools]`; `:193` — `h._build_advisor_model.assert_not_called()` | PASS |
| C8 | Explicit SDK local/native/fallback has one advisor | `test_explicit_sdk_advisor_composition` (3 cases) | `tests/test_upstream_advisor.py:238` — `assert len([t for t in params.native_tools if isinstance(t, AdvisorTool)]) == 1`, with no function advisor at `:239`; local/fallback `:241`–`:242` assert exactly one return containing `"advice"` | PASS |
| C9 | Conflicting advisors rejected before requests | `test_duplicate_advisor_rejected_before_request` (2 cases) | `tests/test_upstream_advisor.py:256` — `with pytest.raises(ValueError, match="either")`; `:258` — `execute.assert_not_called()` | PASS |
| C10 | Saved model/off/default precedence | `test_session_selection_precedence` (3 cases) | `tests/test_upstream_advisor_lifecycle.py:52` — `assert h.advisor_model_id == expected`; literal saved/expected pairs `("saved", "saved"), ("off", None), (None, "default")` at `:34` | PASS |
| C11 | Active turn keeps its choice over all continuations | `test_selection_frozen_across_turn_rounds` (6 cases) | `tests/test_upstream_advisor_lifecycle.py:105` — `assert calls == ["old"]`; `:106` — `assert [r.content for r in returns(h.session.history) if r.tool_name == "advisor"] == ["old"]`; approval/retry/dictionary and new/off parameters at `:56`–`:57` | PASS |
| C12 | Next turn uses new advisor without Agent rebuild | `test_selection_applies_next_turn_without_rebuild` (3 cases) | `tests/test_upstream_advisor_lifecycle.py:142` — `assert h.agent is original`; `:143` preserves exact tool-presence counts; `:144` — `assert first_advice == ([f"advice from {before}"] if before is not None else [])`; `:145` — `assert second_advice == ([f"advice from {after}"] if after is not None else [])`; literal off/new, old/new and old/off pairs at `:111` | PASS |
| C13 | Selection saves only metadata over dirty history | `test_selection_persists_metadata_only` | `tests/test_upstream_advisor_lifecycle.py:154` — `assert h.session.store.load()[0] == baseline`; `:156` — `assert reopened.advisor_model == "new"` | PASS |
| C14 | Selection notices state timing/unavailability, bare opens picker | `test_advisor_selection_notices` | `tests/test_upstream_advisor_ui.py:29` — `assert "next turn" in app.posted[-1]`; `:31` — `assert app.picker_opened`; `:34` asserts unavailable; `:46` and `:48` — `assert "next turn" in notices[-1] and "unavailable" in notices[-1]` | PASS |
| C15 | Historical no-arg calls/trailers survive round trip | `test_historical_advisor_exchange_round_trips` | `tests/test_upstream_advisor_lifecycle.py:169` — `assert store.load()[0] == messages`; `:170` — `assert loaded[0].parts[0].args == {}`; `:171` asserts the adjacent literal historical result at `:161` | PASS |
| C16 | Parallel calls obey one-use cap | `test_parallel_consultations_obey_request_cap` | `tests/test_upstream_advisor.py:273` — `assert len(calls) == 1`; `:276` — `assert sum("limit reached" in r for r in results) == 1` | PASS |
| C17 | Allowance resets for next executor response | `test_cap_resets_each_model_request` | `tests/test_upstream_advisor.py:289` — `assert calls == [1, 1]`; `:290` — `assert [r.content for r in returns(h.session.history)] == ["advice", "advice"]` | PASS |
| C18 | Aggregate usage banked once, with/without approval | `test_advisor_usage_banked_once` (2 cases) | `tests/test_upstream_advisor_lifecycle.py:202` — `assert (usage.requests, usage.input_tokens, usage.output_tokens) == (5, 47, 21)` over outcome, session and persisted usage at `:201` | PASS |
| C19 | Child shares remaining request limit | `test_advisor_shares_remaining_turn_limits` (2 cases) | `tests/test_upstream_advisor_lifecycle.py:226` — `with pytest.raises(UsageLimitExceeded)`; `:228` — `assert calls == []`; `:229` — `assert h.session.usage.requests == (2 if approval else 1)` | PASS |
| C20 | Failure/cancellation banks already-reported usage once | `test_failed_or_cancelled_advice_banks_usage_once` (2 cases) | `tests/test_upstream_advisor_lifecycle.py:264` — `assert (usage.input_tokens, usage.output_tokens) == (29, 13)`; `:265` and `:266` assert 29 input tokens in turn and persisted totals | PASS |
| C21 | Provider errors propagate; history resumes | `test_advisor_failure_leaves_resumable_history` | `tests/test_upstream_advisor_lifecycle.py:281` — `with pytest.raises(ModelHTTPError)`; helper at `:272` — `assert calls <= results`, invoked on saved history at `:283`; `:286` — `assert (await h.run_turn("continue")).result == "recovered"` | PASS |
| C22 | Repeated interruption retains ownership through cleanup | `test_advisor_cancellation_retains_claim_until_cleanup` | `tests/test_upstream_advisor_lifecycle.py:323` — `assert try_acquire(path, kind="headless") is None`; `:327` — `assert cleaned.is_set()`; `:329` — `assert claim is not None` after both cancellations and release | PASS |
| C23 | Output/use bounds enforced | `test_advisor_bounds` (5 cases) | `tests/test_upstream_advisor.py:296` — `with pytest.raises(ValueError, match="1024")`; accepted construction `:299`; `:301` — `with pytest.raises(ValueError, match="at least 1")`; `:304` — `assert Advisor(TestModel(), max_uses=uses).max_uses == uses` for literal `(1, None)` | PASS |
| C24 | Env unset/zero/two mapping | `test_environment_use_cap` (3 cases) | `tests/test_upstream_advisor.py:316` — `assert build_harness(tmp_path)._advisor_max_uses == expected`; literal pairs `(None, None), ("0", None), ("2", 2)` at `:307` | PASS |
| C25 | Mixed costs remain estimated/unknown with persistence | `test_mixed_advisor_cost_is_not_falsely_exact` (2 cases) | `tests/test_upstream_advisor_lifecycle.py:389` and `:390` — `assert resolve_cost(..., "anthropic:claude-sonnet-4-6") == expected`; adjacent `:350` fixes expected to `(float(cost) if cost is not None else None, False)` for `None` and `Decimal("0.0123")`; wire round trip at `:357` | PASS |
| C26 | Base dependency and installed wheel work without Monty | `test_base_dependency_contract`; exact 0.11.0 wheel smoke | `tests/test_upstream_advisor.py:323` — `assert any("pydantic-ai-harness==0.31.0" in r and "extra" not in r for r in requirements)`; `:324` asserts core `"<3,>=2.43"`; `.specs/features/upstream-advisor/base_install_smoke.py:33` rejects Monty, `:42` — `assert (await agent.run("go")).output == "done"`, `:52` — `assert (await harness.run_turn("go")).result == "done"` | PASS |
| C27 | Import aliases, defaults and retired options | `test_legacy_imports_are_upstream_aliases` | `tests/test_upstream_advisor.py:50` — `assert package_alias is module_alias is Advisor`; `:52`–`:53` assert upstream defaults; `:55` — `with pytest.raises(TypeError)` for literal retired names at `:54` | PASS |
| C28 | Custom consultation engine removed | `test_no_legacy_advisor_engine` | `tests/test_upstream_advisor.py:339` — `assert retired not in text` over all production Python and explicit retired engine/counter/trailer strings at `:330`–`:338`; removal corroborated by complete feature diff | PASS |
| C29 | Live and replay render advice outside tool groups | `test_advice_live_and_replay_standalone` (2 cases) | `tests/test_upstream_advisor_ui.py:86` — `assert advice[0].result_text == "Check the rollback."`; `:90` — `assert all(advice[0] not in g.walk_children() for g in groups)`, with nonempty groups and one advice widget asserted | PASS |
| C30 | Documentation contains all migrated behaviors | `test_advisor_documentation_contract` | `tests/test_upstream_advisor_ui.py:113` — `assert phrase in docs` over seven adjacent literal behaviors at `:104`–`:111`; `:114` asserts upstream import; migrated documentation diff also inspected | PASS |
| C31 | Settings label and minimum output bound | `test_settings_advisor_bounds_and_labels` | `tests/test_upstream_advisor_ui.py:126` — `assert ENV_INT_INPUTS["advisor-max-uses"][1] == "Advisor calls per model request"`; `:134` — `assert saves == [] and "1024" in notices[-1]`; `:136` — `assert saves == [("MARIM_ADVISOR_MAX_TOKENS", "1024")]`; actual Label matches in `src/marim_harness/interfaces/tui/settings_sections.py:254` | PASS |

## Level, sampling and existing constraints

Verified at `96daf5b0`.

The runtime checks drive real Harness/Builder/controller and upstream capability
boundaries with deterministic providers. CLI isolation replaces transport requests,
so it proves clone settings and ownership, not live subscription/provider behavior.
SDK native mode proves native-tool composition through a supported mock, not a
remote provider's native execution. These are the approved offline boundaries.
UI C29 mounts the Textual app for both live and replay. Selection copy uses the
real dispatch/picker/startup methods with a stub app; settings use the real form
commit/parser boundary and the visible label's source. C30 checks phrases across
the four active documents and the migration diff corroborates their context; it
does not independently certify every documentation example. C12 now drives two
real turns and distinguishes selected advisors by their response text while
retaining Agent identity and availability assertions. No value gap remains.
Bounds, continuation, CLI and failure parametrizations are individually present
and executed; no named proof was missing or deselected.

The `Swept` authorization row's existing constraints were re-read:
`src/marim_harness/runtime/permissions.py:235` preserves mode-aware approval,
`src/marim_harness/session/claim.py:125` preserves exclusive nonblocking flock,
and `src/marim_harness/runtime/backend_jobs.py:22` drains cleanup despite repeated
interrupts. The advisor hook reuses that drain (`runtime/advisor_lifecycle.py:28`),
and C22 exercises actual claim contention. Existing auxiliary isolation is still
centralized at `src/marim_harness/session/ctrl.py:40`; metadata-only selection at
`:455` uses `save_meta()` for an existing session. Abort repair/persist remains at
`src/marim_harness/runtime/controller.py:706`. Attached TUI mutations continue
through `src/marim_harness/interfaces/tui/app.py:940` (`require_local`). No existing
constraint cited by the sweep was found absent.

## Compatibility with current master

Verified at `96daf5b0`. `git diff --name-only` over the full range confirms no
changes to `pyproject.toml`, `uv.lock`, `quality-baseline.json`, the session and
workflow packages, `runtime/output_limits.py`, or master's lesson registry.
The prior underlying workflow migration is therefore not republished by this port.
`runtime/harness.py:395` still composes `session_output_limits()` alongside the
new `AdvisorLifecycle()`. `interfaces/tui/session_view.py:400` still renders
SystemPromptPart compaction summaries; advisor standalone replay is an additional
branch at `:406`. The rest of the compaction and output-limit implementations are
unchanged. Server changes are confined to serializing Decimal usage cost at
`src/marim_harness/server/host.py:67`; C25 proves its JSON round trip and cost
meaning. There is no added server endpoint or mutation permission. AD-001 remains
intact, and the changelog places this feature under Unreleased.

## Profile limits

Verified at `96daf5b0`.

Light profile was retained. No mutation testing, Coverage join recomputation, or
UI binding-source audit was performed. The author's Coverage table was read, not
independently recomputed. No Test policy section exists in the approved checks.
No human visual walkthrough was needed for the deterministic copy, grouping and
settings assertions; typography/layout redesign is outside this migration.

## Gate

`uv run python /home/mateuscmarim/.config/marim/skills/tlc-spec-lean/scripts/validate_verification.py upstream-advisor`

Run in the port checkout named above; exited **0**:

```text
validate_verification: 0 error(s), 0 warning(s) across [upstream-advisor]
```

## Lessons and ownership

This full port verification produced no grounded failure, so no lesson was added.
Historical feature-branch lesson identifiers are not imported into master's registry.
No production or test files were edited, staged or committed by the verifier.
