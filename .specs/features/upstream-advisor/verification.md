# Upstream Advisor verification

> Historical report for the original feature branch only. The advisor-only port
> onto fb558301d2def1f3c90011dbeec0ec262c6ec888 requires fresh independent verification.
> No PASS below applies to the transplanted tree. Its lesson identifiers belong
> to the original branch; master's existing lesson registry is preserved unchanged.

**Verdict**: PASS
**Profile**: light
**Diff range**: 1eb9a8e6c26b1a68cb26d4a8ec54df065440512c..35cae596b6c1f250651250a276147ff81778a1d8
**Round**: 2 - scoped
**Verifier**: independent sub-agent `/root/advisor_verifier` (author != verifier)

31 of 31 checks have sufficient located evidence. C12 now proves the selected
advisor's distinct response on the next turn, as well as unchanged Agent identity
and absence of advice after disabling.

## Scope and resolved finding

Verified fix diff `4b95039f8a281e09c3bff43375434d062e3a24ad..35cae596b6c1f250651250a276147ff81778a1d8`:
only C12's test and the checks artifact's build-evidence section changed.
Production, fixtures, and approved obligations did not change. The original C12
finding was a proof gap: tool counts alone did not identify the consulted model.
The strengthened proof invokes consultations and asserts `advice from old` /
`advice from new` for the selected turns.

All code reads and proof/build commands in this round used the clean detached
checkout
`/tmp/marim-1000/marim-harness-309990f67170/20260916-213829-4a7aee/scratchpad/advisor-verify.iHpLES`.
Its HEAD was `35cae596b6c1f250651250a276147ff81778a1d8` and its status was clean
both before and after execution. Concurrent unrelated Claude CLI edits in the main
workspace are excluded from this feature and this report.

## Proof execution

Verified at `35cae596`. All named proofs were rerun independently in one batched
pytest invocation, with every one of the 57 parametrized cases individually shown
as `PASSED`. Tests used Python **3.12.13**, core **2.43.0**, Harness **0.31.0**,
and the repository's default 12 xdist workers. The exact base-wheel smoke selected
Python **3.13.14** in its separate isolated environment. No live or paid provider
calls were made.

```text
uv run --isolated --locked --python 3.12 pytest --no-cov -vv -m 'not live' tests/test_upstream_advisor.py tests/test_upstream_advisor_lifecycle.py tests/test_upstream_advisor_ui.py
57 passed, exit 0 (4.71s)

uv build
Built dist/marim_harness-0.9.1.tar.gz and dist/marim_harness-0.9.1-py3-none-any.whl, exit 0

uv run --isolated --no-project --with ./dist/marim_harness-0.9.1-py3-none-any.whl python .specs/features/upstream-advisor/base_install_smoke.py
Base-only wheel Advisor smoke passed; Monty absent, exit 0
```

Carried execution history from round 1 at `4b95039f`: Python 3.12.3, the 142-case
proof/regression batch passed (one existing event-loop deprecation warning), the
57-proof batch passed, and build/base-wheel smoke passed. These earlier results
do not substitute for the fresh proof runs above. Orchestrator full-suite and
other Python-version results are not represented as this verifier's execution.

## Checks

All proof runs below are verified at `35cae596`. C12's assertion verdict is newly
verified at `35cae596`; C1–C11 and C13–C31 assertion verdicts are carried from
`4b95039f`. Citations in the touched lifecycle test file were refreshed at
`35cae596`; unchanged-file citations remain valid. File names are repository-relative.
Adjacent literal parametrizations supply expected values where indicated; no
expected value requires tracing an external fixture.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | Upstream consultation returns the required advice | `test_runtime_uses_upstream_advisor` | `tests/test_upstream_advisor.py:34` — `assert tool.parameters_json_schema["required"] == ["prompt"]`; `:43` — `assert seen[-1].content == "Check the rollback."`; construction directly imports/returns upstream Advisor in `src/marim_harness/runtime/harness.py:1161` | PASS |
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
| C26 | Base dependency and installed wheel work without Monty | `test_base_dependency_contract`; exact wheel smoke | `tests/test_upstream_advisor.py:323` and `:324` assert base Harness/core bounds; `.specs/features/upstream-advisor/base_install_smoke.py:33` rejects Monty, `:42` — `assert (await agent.run("go")).output == "done"`, `:52` — `assert (await harness.run_turn("go")).result == "done"` | PASS |
| C27 | Import aliases, defaults and retired options | `test_legacy_imports_are_upstream_aliases` | `tests/test_upstream_advisor.py:50` — `assert package_alias is module_alias is Advisor`; `:52`–`:53` assert upstream defaults; `:55` — `with pytest.raises(TypeError)` for literal retired names at `:54` | PASS |
| C28 | Custom consultation engine removed | `test_no_legacy_advisor_engine` | `tests/test_upstream_advisor.py:339` — `assert retired not in text` over all production Python and explicit retired engine/counter/trailer strings at `:330`–`:338`; removal corroborated by complete feature diff | PASS |
| C29 | Live and replay render advice outside tool groups | `test_advice_live_and_replay_standalone` (2 cases) | `tests/test_upstream_advisor_ui.py:86` — `assert advice[0].result_text == "Check the rollback."`; `:90` — `assert all(advice[0] not in g.walk_children() for g in groups)`, with nonempty groups and one advice widget asserted | PASS |
| C30 | Documentation contains all migrated behaviors | `test_advisor_documentation_contract` | `tests/test_upstream_advisor_ui.py:113` — `assert phrase in docs` over seven adjacent literal behaviors at `:104`–`:111`; `:114` asserts upstream import; migrated documentation diff also inspected | PASS |
| C31 | Settings label and minimum output bound | `test_settings_advisor_bounds_and_labels` | `tests/test_upstream_advisor_ui.py:126` — `assert ENV_INT_INPUTS["advisor-max-uses"][1] == "Advisor calls per model request"`; `:134` — `assert saves == [] and "1024" in notices[-1]`; `:136` — `assert saves == [("MARIM_ADVISOR_MAX_TOKENS", "1024")]`; actual Label matches in `src/marim_harness/interfaces/tui/settings_sections.py:261` | PASS |

## Level, sampling and existing constraints

Carried from `4b95039f`, with C12's corrected boundary evidence verified at `35cae596`.

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
centralized at `src/marim_harness/session/ctrl.py:44`; metadata-only selection at
`:456` uses `save_meta()` for an existing session. Abort repair/persist remains at
`src/marim_harness/runtime/controller.py:701`. Attached TUI mutations continue
through `src/marim_harness/interfaces/tui/app.py:934` (`require_local`). No existing
constraint cited by the sweep was found absent.

## Profile limits

Carried from `4b95039f`; unchanged light-profile scope at `35cae596`.

Light profile was retained. No mutation testing, Coverage join recomputation, or
UI binding-source audit was performed. The author's Coverage table was read, not
independently recomputed. No Test policy section exists in the approved checks.
No human visual walkthrough was needed for the deterministic copy, grouping and
settings assertions; typography/layout redesign is outside this migration.

## Gate

`uv run python /home/mateuscmarim/.config/marim/skills/tlc-spec-lean/scripts/validate_verification.py upstream-advisor --root /home/mateuscmarim/Projects/marim.dev/marim-harness`

Exited **0**:

```text
validate_verification: 0 error(s), 0 warning(s) across [upstream-advisor]
```

## Lessons and ownership

Round 1 recorded **L-003**, candidate, grounded in C12:
“When verifying model selection changes, assert the selected model identity or its
distinct response in addition to tool availability.” The fix addresses that gap.
Round 2 produced no new grounded failure, so no new lesson was recorded.
No production or test files were edited, staged or committed by the verifier.
