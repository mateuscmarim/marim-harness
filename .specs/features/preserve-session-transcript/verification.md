# Preserve session transcript verification

**Verdict**: PASS
**Profile**: light
**Diff range**: fb558301..f1a1aea957d9e86c2fb3b21748637e997eef22e8
**Round**: 1 - full
**Verifier**: independent sub-agent (author != verifier), dispatched by the whole-feature orchestrator

All C1–C24 were independently verified at the stated HEAD. The complete implementation diff was reviewed, including existing test-double adjustments and documentation. No concrete correctness findings were identified. Source and tests were not edited.

## Proof run

One batched invocation, run after lint and type checking:

`UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync pytest --no-cov -vv tests/test_session_transcript.py`

Exit 0: **57 passed in 2.16s**, Python 3.13.14, 12 workers. Every named proof appeared individually as PASSED, including every parameterized instance. The 22 distinct named proofs serving C1–C24 account for 48 instances; nine additional instances cover part slicing, repaired context, provider cancellation, media/metadata round trips, and unchanged HTTP media references. All named proofs are newly added in the reviewed diff.

## Checks

Each proof below is `tests/test_session_transcript.py::<name>` in the batched command above. Locations are assertion lines at the verified HEAD; expected snapshots are captured in the same test before the operation.

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

The proof levels match their claims: C1–C3 exercise the measurement function; C4 reaches the automatic gate; C5 reaches the recorder callback; C6/C8 exercise upstream reductions through the session controller; C7/C9/C20/C24 run actual Harness turns with deterministic models; C10–C11 and C14–C22 cross persistence/lifecycle boundaries; C12/C21 use the ASGI endpoint; C13 covers both history adapters and app accessors; C23 exercises the remote client's HTTP decoding. No claim names a boundary that its proof never reaches.

Sampling is explicit: C4 uses the Codex incident while both CLI names are covered at the reader; C7/C9 use a summary before a real turn, while C6 covers all three reduction stages; repeated reductions use three summary cycles; C18 samples malformed JSON and invalid tasks; C19 injects a pre-replacement failure; C22 exercises one deterministic delayed-writer order. These are focused regressions, not exhaustive provider, scheduler, filesystem, or payload exploration. No uncovered case was concretely identified in the checked claims. TUI verification covers replay data and accessors, not terminal rendering or human visual judgment; this feature changes neither screen layout nor interaction design.

## Swept existing

- Authorization is still present: `src/marim_harness/server/http.py:103` defines the bearer check and `src/marim_harness/server/http.py:1007` calls it before workspace/session lookup. The C12 proof also asserts 401 without a token at `tests/test_session_transcript.py:495`.
- Dependency failures still cannot install a partial reduction: `src/marim_harness/session/ctrl.py:912` awaits `reduce_history` before `_commit_reduction` at line 929. The finally block only banks usage. The upstream reduction integration was not replaced in this diff.
- Compaction start/finished events remain wired at `src/marim_harness/server/host.py:151` and `src/marim_harness/server/host.py:162`; `src/marim_harness/session/ctrl.py:960` emits start and its finally block at line 1000 emits completion, including failure paths.
- The history watermark still comes from the existing bus at `src/marim_harness/server/http.py:1036` and is returned at line 1044. `tests/test_session_transcript.py:491` asserts the no-live-bus value is zero.

The remaining Swept rows resolve to checks in the evidence table. No cited existing constraint was found absent.

## Profile limits

The approved light profile does not require recomputing the Coverage join, fault injection, or comparing external binding sources and screen composition. Those steps were not performed. There are no Test policy rows in checks.md. This report does not claim exhaustive set-member coverage or mutation resistance. No grounded failure was found, so no lesson was recorded.

## Gate

- `UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync ruff check src tests` — exit 0.
- `UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync pyright` — exit 0, zero errors/warnings/informations.
- Batched named-proof command above — exit 0, 57 passed, zero failed/skipped.
- `git diff --check fb558301..HEAD` — exit 0.
- `UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync python /home/mateuscmarim/.config/marim/skills/tlc-spec-lean/scripts/validate_verification.py .specs/features/preserve-session-transcript` — exit 0, zero errors and zero warnings.

Full-suite and packaging results in checks.md are the builder's evidence, not independently rerun here. This verification covers the complete obligation set at HEAD.
