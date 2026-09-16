# Preserve session transcript verification

**Verdict**: PASS
**Profile**: light
**Diff range**: fb558301..ad0b7b6299d6e9ffa97cc6888ac4392d0dfeb36c
**Round**: 2 - scoped
**Verifier**: independent sub-agent (author != verifier), dispatched by the whole-feature orchestrator

All C1–C24 proofs were independently rerun at `ad0b7b6299d6e9ffa97cc6888ac4392d0dfeb36c`. This round reviewed fix diff `838f9ca3..ad0b7b6299d6e9ffa97cc6888ac4392d0dfeb36c`, updated plan/checks, persistence signatures and atomicity, and every touched test spy. The whole-feature review of unchanged implementation is carried from `f1a1aea957d9e86c2fb3b21748637e997eef22e8`. The actual CI failure missed in round 1 is recorded below and is now resolved. No outstanding actionable findings remain. Source and tests were not edited.

## Proof run

Verified at `ad0b7b6299d6e9ffa97cc6888ac4392d0dfeb36c`. One batched invocation, run after lint and type checking:

`UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync pytest --no-cov -vv tests/test_session_transcript.py tests/test_recovery.py::test_orphaned_flush_persist_does_not_clobber_newer_write tests/test_session.py::test_persist_snapshots_tasks_and_jobs_with_history tests/test_session_persist_cache.py::test_persist_hands_store_a_snapshot_not_the_live_history`

Exit 0: **61 passed in 2.05s**, Python 3.13.14, 12 workers. Every named proof appeared individually as PASSED, including every parameterized instance. The transcript file contributes 58 passing cases: the 22 distinct named proofs serving C1–C24 account for 48 instances; ten additional instances cover part slicing, repaired context, provider cancellation, media/metadata round trips, unchanged HTTP media references, and paired-save compatibility. Three existing tests whose spies changed also passed in the same invocation. One upstream deprecation warning reports that there is no current event loop; no tests failed or skipped.

## Checks

Each proof below is `tests/test_session_transcript.py::<name>` in the batched command above. Every assertion location was re-read at `ad0b7b6299d6e9ffa97cc6888ac4392d0dfeb36c`; the existing proof line numbers did not shift. Expected snapshots are captured in the same test before the operation. C1–C24 obligation meanings are unchanged; the new compatibility proof below is additive evidence.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | Both CLI providers use current context, including zero | `test_cli_current_context` (4 passed) | `tests/test_session_transcript.py:56` — `assert last_request_input_tokens([response]) == used`; parameters at lines 47–48 explicitly name both providers and `[119_343, 0]` | PASS |
| C2 | Missing/malformed newest CLI report does not reuse older usage | `test_cli_unknown_context` (10 passed) | `tests/test_session_transcript.py:70` — `assert last_request_input_tokens([older, newest]) is None` | PASS |
| C3 | Native positive usage and fallback stay intact | `test_native_context` (1 passed) | `tests/test_session_transcript.py:76` — `assert last_request_input_tokens([older, newest]) == 234`; line 77 asserts the zero-usage response falls back to 100 | PASS |
| C4 | Incident values do not trigger reduction | `test_cli_gate` (1 passed) | `tests/test_session_transcript.py:100` — `assert await controller.maybe_compact() is False`; line 101 — `assert calls == []`; configured threshold is 150,000 at line 36 | PASS |
| C5 | Ledger retains aggregate usage | `test_cli_ledger` (1 passed) | `tests/test_session_transcript.py:114` — `assert recorded == [2_642_890]`; line 115 — `assert controller.usage.input_tokens == 2_642_890` | PASS |
| C6 | Summary, trim, and clear preserve original content/order | `test_reduction_preserves_transcript` (3 passed) | `tests/test_session_transcript.py:154` — `assert controller.transcript == expected`, where line 149 deep-copies the original; line 157 repeats equality after context mutation | PASS |
| C7 | Next turn appends each message once without synthetic summaries | `test_post_reduction_turn` (1 passed) | `tests/test_session_transcript.py:194` — `assert controller.transcript[: len(original)] == original`; line 195 — `assert len(controller.transcript) == len(original) + 2`; lines 196–197 assert the new question and answer | PASS |
| C8 | Repeated reductions retain chronology without duplication | `test_repeated_reductions` (1 passed) | `tests/test_session_transcript.py:211` — `assert controller.transcript == expected`; lines 205–209 append fourth, fifth, and sixth turns to the expected original sequence | PASS |
| C9 | Actual model invocation receives only reduced context | `test_post_reduction_turn` (1 passed) | `tests/test_session_transcript.py:192` — `assert received_parts[:-1] == reduced_parts`; line 193 — `assert received_parts[-1].content.endswith("new question")` | PASS |
| C10 | Restart restores the exact transcript | `test_transcript_resume` (1 passed) | `tests/test_session_transcript.py:225` — `assert controller.transcript == expected`; line 226 — `assert controller.history == reduced` | PASS |
| C11 | Legacy replay contains only saved content | `test_legacy_transcript` (1 passed) | `tests/test_session_transcript.py:238` — `assert "transcript" not in json.loads(controller.store.path.read_text())`; line 240 — `assert controller.transcript == original` | PASS |
| C12 | HTTP pagination partitions transcript and reports its size | `test_http_transcript_pages` (1 passed) | `tests/test_session_transcript.py:490` — `assert payload["message_count"] == len(original)`; line 494 — `assert ModelMessagesTypeAdapter.validate_python(pages) == original` | PASS |
| C13 | Local and attached replay equal HTTP transcript | `test_tui_transcript` (1 passed) | `tests/test_session_transcript.py:563` — `assert local_history.messages == remote_history.messages == original`; lines 567–568 assert both app replay accessors equal original | PASS |
| C14 | Rewind truncates at the persisted transcript boundary | `test_checkpoint_transcript` (3 passed) | `tests/test_session_transcript.py:354` — `assert controller.transcript == original`; line 355 also asserts persisted transcript length; includes summary, clear, and legacy sidecars | PASS |
| C15 | Undo restores the entire pre-rewind transcript | `test_checkpoint_transcript` (3 passed) | `tests/test_session_transcript.py:356` — `assert checkpoints.undo_rewind()`; line 357 — `assert controller.transcript == before` | PASS |
| C16 | Clear/new session empty both views | `test_transcript_reset` (2 passed) | `tests/test_session_transcript.py:267` — `assert controller.history == []`; line 268 — `assert controller.transcript == []`; line 270 checks the saved empty transcript | PASS |
| C17 | Switching selects only target transcript | `test_transcript_switch` (1 passed) | `tests/test_session_transcript.py:284` — `assert controller.transcript == target_messages`; line 286 asserts switching back restores original | PASS |
| C18 | Failed switch preserves active context/transcript | `test_transcript_switch_failure` (2 passed) | `tests/test_session_transcript.py:306` — `assert controller.store is active`; line 307 — `assert (controller.history, controller.transcript) == before` | PASS |
| C19 | Atomic-write failure leaves previous disk pair intact | `test_transcript_atomic_failure` (1 passed) | `tests/test_session_transcript.py:323` — `with pytest.raises(OSError, match="injected")`; line 325 — `assert controller.store.path.read_bytes() == before` | PASS |
| C20 | Pending approval/cancellation persist only committed baseline | `test_transcript_approval_cancel` (2 passed) | `tests/test_session_transcript.py:410` — `assert len(payload["transcript"]) == len(original)` inside approval; line 442 — `assert controller.transcript == original` after cancellation; lines 447–449 compare saved and committed lengths | PASS |
| C21 | Malformed transcript raises load error and HTTP unreadable | `test_malformed_transcript` (6 passed) | `tests/test_session_transcript.py:513` — `with pytest.raises(SessionLoadError)`; line 520 — `assert response.status_code == 500`; line 521 — `assert response.json()["error"]["code"] == "unreadable"` | PASS |
| C22 | Delayed/repeated saves retain newest coherent generation | `test_transcript_delayed_save` (1 passed) | `tests/test_session_transcript.py:389` — `assert snapshots[1][1] == original + new`; lines 392–393 assert both persisted views end with `"newer generation"`; line 387 asserts exactly two writes | PASS |
| C23 | Optional context estimate handles zero and old-server fallback | `test_remote_context_tokens` (3 passed) | `tests/test_session_transcript.py:597` — `assert remote.info.history_tokens == (estimate_tokens(messages) if reported is None else reported)`; parameters are None, 0, 123 | PASS |
| C24 | Normalized checkpoint restores matching context/transcript | `test_checkpoint_normalized_context` (1 passed) | `tests/test_session_transcript.py:702` — `assert [part for message in controller.history for part in message.parts] == before_parts`; line 703 — `assert controller.transcript == original` | PASS |

## Level and sampling judgment

Carried from `f1a1aea957d9e86c2fb3b21748637e997eef22e8`, with the persistence-boundary assessment rechecked at `ad0b7b6299d6e9ffa97cc6888ac4392d0dfeb36c`. The new compatibility proof crosses the actual on-disk save boundary, and all three changed spies still assert snapshot isolation and ordering.

The proof levels match their claims: C1–C3 exercise the measurement function; C4 reaches the automatic gate; C5 reaches the recorder callback; C6/C8 exercise upstream reductions through the session controller; C7/C9/C20/C24 run actual Harness turns with deterministic models; C10–C11 and C14–C22 cross persistence/lifecycle boundaries; C12/C21 use the ASGI endpoint; C13 covers both history adapters and app accessors; C23 exercises the remote client's HTTP decoding. No claim names a boundary that its proof never reaches.

Sampling is explicit: C4 uses the Codex incident while both CLI names are covered at the reader; C7/C9 use a summary before a real turn, while C6 covers all three reduction stages; repeated reductions use three summary cycles; C18 samples malformed JSON and invalid tasks; C19 injects a pre-replacement failure; C22 exercises one deterministic delayed-writer order. These are focused regressions, not exhaustive provider, scheduler, filesystem, or payload exploration. No uncovered case was concretely identified in the checked claims. TUI verification covers replay data and accessors, not terminal rendering or human visual judgment; this feature changes neither screen layout nor interaction design.

## Swept existing

Authorization, event wiring, and watermark findings are carried from `f1a1aea957d9e86c2fb3b21748637e997eef22e8`; those files were not changed by the fix. The touched controller was re-read at `ad0b7b6299d6e9ffa97cc6888ac4392d0dfeb36c`, with moved citations refreshed below.

- Authorization is still present: `src/marim_harness/server/http.py:103` defines the bearer check and `src/marim_harness/server/http.py:1007` calls it before workspace/session lookup. The C12 proof also asserts 401 without a token at `tests/test_session_transcript.py:495`.
- Dependency failures still cannot install a partial reduction: `src/marim_harness/session/ctrl.py:911` awaits `reduce_history` before `_commit_reduction` at line 928. The finally block only banks usage. The upstream reduction integration was not replaced in this diff.
- Compaction start/finished events remain wired at `src/marim_harness/server/host.py:151` and `src/marim_harness/server/host.py:162`; `src/marim_harness/session/ctrl.py:959` guards start and its finally block at line 999 guards completion, including failure paths.
- The history watermark still comes from the existing bus at `src/marim_harness/server/http.py:1036` and is returned at line 1044. `tests/test_session_transcript.py:491` asserts the no-live-bus value is zero.

The remaining Swept rows resolve to checks in the evidence table. No cited existing constraint was found absent.

## Profile limits

The profile assessment is carried from `f1a1aea957d9e86c2fb3b21748637e997eef22e8`; updated checks still declare light. The approved light profile does not require recomputing the Coverage join, fault injection, or comparing external binding sources and screen composition. Those steps were not performed. There are no Test policy rows in checks.md. This report does not claim exhaustive set-member coverage or mutation resistance. The grounded CI failure below is handed to the orchestrator for a lesson; this verifier's permitted write scope is only this report.

## Resolved CI finding and fix evidence

Verified at `ad0b7b6299d6e9ffa97cc6888ac4392d0dfeb36c`.

The orchestrator reported PR 151 quality-gate run **4572**, job **10390**, failed because `complexity_violations` rose from **57 to 58**. Round 1 ran normal Ruff selection, which omits the separate PLR rules selected by `.gitea/workflows/quality-gate.yml:175`. This was a missed required CI check, not a flaky test or a baseline to relax. Independently piping `git show f1a1aea9:src/marim_harness/session/store.py` into the explicit rule set reproduces **PLR0913 at line 248: six arguments, maximum five**, alongside the existing constructor diagnostic. The remote CI identifiers are orchestrator-provided; the source-level failure was reproduced directly.

The fix introduces `SessionMessages(context, transcript)` at `src/marim_harness/session/store.py:203` and passes that typed pair through the existing history parameter at `src/marim_harness/session/ctrl.py:423`. Both snapshots are still captured from the same `_HistoryState` at lines 408–410 under the existing persistence lock. `SessionStore.save` unpacks the pair before building the same single JSON payload and retains its existing lock/atomic replacement at `src/marim_harness/session/store.py:324`. Legacy list callers keep their five-argument call shape. No new writer, suppression, threshold change, or assertion weakening was introduced.

The additional proof `tests/test_session_transcript.py::test_paired_save_preserves_legacy_contract` passed. `tests/test_session_transcript.py:738` asserts the exact parameters `["history", "usage", "tasks", "duration_seconds", "jobs"]`; line 751 asserts a legacy positional save has no transcript field; lines 755–759 assert paired saves preserve context, usage, tasks, jobs, and duration while recording `"recorded conversation"` in the transcript.

Changed spies were reviewed without accepting weaker assertions: `tests/test_recovery.py:374` reads the paired context and still checks stale/newer write order; `tests/test_session.py:1883` retains task snapshot isolation through the unchanged positional signature; `tests/test_session_persist_cache.py:264` examines the paired context object and still checks identity and length before/after concurrent append; `tests/test_session_transcript.py:375` records both pair members and retains coherent-generation assertions. All four corresponding tests passed in the single proof run.

Independent complexity comparison used the exact selection `C901,PLR0911,PLR0912,PLR0913,PLR0915`, `--no-cache --output-format=json`, over both `src` and `tests`. Before using the exported baseline at `scratchpad/complexity-baseline.xJCJy9`, every one of its **611** source/test/config blobs was hashed against `git ls-tree -r fb558301`, and the export was checked for extra Python files. Each Ruff invocation used that tree's `pyproject.toml`. Diagnostics were compared as a multiset of relative path, rule, and message, ignoring shifted line numbers.

- Source diagnostics: **57 → 57**.
- Source and test diagnostics: **91 → 91**.
- Added diagnostics: **0**; removed diagnostics: **0**.

Both explicitly selected Ruff invocations exit **1** because the existing diagnostics remain. The comparison exits **0** because no diagnostic was added; it does not reinterpret the existing nonzero diagnostic count as clean lint. This resolves the observed regression without changing the baseline.

## Gate

Verified at `ad0b7b6299d6e9ffa97cc6888ac4392d0dfeb36c`:

- `UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync ruff check src tests` — exit 0.
- `UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync pyright` — exit 0, zero errors/warnings/informations.
- Batched named-proof command above — exit 0, 61 passed (58 transcript cases plus three changed-spy tests), zero failed/skipped.
- Explicit CI complexity selection compared against verified `fb558301` export — comparison exit 0, zero added diagnostics; existing counts remain 57 source and 91 total.
- `git diff --check fb558301..HEAD` — exit 0.
- `UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync python /home/mateuscmarim/.config/marim/skills/tlc-spec-lean/scripts/validate_verification.py .specs/features/preserve-session-transcript` — exit 0, zero errors and zero warnings.

Full-suite and packaging results in checks.md are the builder's evidence, not independently rerun here. This verification covers the complete obligation set at HEAD.
