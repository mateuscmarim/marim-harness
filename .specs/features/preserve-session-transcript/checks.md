# Preserve session transcript checks

Profile: light
Plan: `.specs/features/preserve-session-transcript/plan.md`

24 checks in one builder batch · 5 one-way doors · 24 closed, 0 open, 0 blocked

## Checks

**C1** - CLI context measurement is the report used value (including zero), for both providers; aggregate usage is ignored (AC 1).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_cli_current_context`

**C2** - Missing or malformed newest CLI reports yield None without falling back to older usage (AC 2).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_cli_unknown_context`

**C3** - Native response request usage retains its existing positive-usage fallback (AC 3).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_native_context`

**C4** - The 2,642,890/119,343 incident makes zero reductions below the 150,000 trigger (AC 4).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_cli_gate`

**C5** - CLI aggregate input usage stays 2,642,890 in the recorded ledger (AC 5).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_cli_ledger`

**C6** - Summary, trim and clear reductions preserve pre-reduction content and order (AC 6).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_reduction_preserves_transcript`

**C7** - A clean post-reduction turn adds each new message once and no synthetic summary (AC 7).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_post_reduction_turn`

**C8** - Multiple reductions preserve each original message once in chronology (AC 8).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_repeated_reductions`

**C9** - A post-reduction model invocation receives only reduced context (AC 9).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_post_reduction_turn`

**C10** - Persist/resume restores exactly the pre-restart transcript (AC 10).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_transcript_resume`

**C11** - Legacy files without transcript replay only available saved messages (AC 11).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_legacy_transcript`

**C12** - HTTP pages partition the transcript and report its length (AC 12).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_http_transcript_pages`

**C13** - Local and attached TUI replay expose the same transcript as HTTP (AC 13).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_tui_transcript`

**C14** - Rewind truncates at the checkpoint transcript boundary, including tool-only clears (AC 14).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_checkpoint_transcript`

**C15** - Undo restores the complete pre-rewind transcript (AC 15).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_checkpoint_transcript`

**C16** - Clear and new session yield an empty transcript (AC 16).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_transcript_reset`

**C17** - Successful switch exposes only the target transcript (AC 17).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_transcript_switch`

**C18** - Failed switch leaves the active context and transcript unchanged (AC 18).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_transcript_switch_failure`

**C19** - A failure before atomic replacement leaves the previous context/transcript pair intact (AC 19).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_transcript_atomic_failure`

**C20** - Pending approvals and cancelled flushes persist only the committed resumable baseline (AC 20).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_transcript_approval_cancel`

**C21** - Malformed transcript fails loading with SessionLoadError and HTTP with 500 unreadable (AC 21).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_malformed_transcript`

**C22** - Repeated/delayed saves retain one coherent newest context/transcript generation without duplicates (AC 22).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_transcript_delayed_save`

**C23** - Remote context tokens use the optional context_tokens value including zero, with legacy-server estimation fallback (door 4).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_remote_context_tokens`

**C24** - A checkpoint after compaction rewinds model context and transcript to the same conversation despite upstream envelope normalization (Relations, door 5).
Proof: `uv run pytest --no-cov tests/test_session_transcript.py::test_checkpoint_normalized_context`

## Coverage

| Set (size) | Member -> proof | Unproven |
| --- | --- | --- |
| CLI providers (2) | claude-cli C1 · codex-cli C1 | - |
| CLI report states (4) | positive C1 · zero C1 · absent C2 · malformed C2 | - |
| Reduction stages (3) | summary C6 · trim C6 · clear C6 | - |
| Replay adapters (2) | local C13 · attached C13 | - |
| Lifecycle operations (5) | resume C10 · rewind C14 · undo C15 · reset C16 · switch C17 | - |
| Persistence failure surfaces (3) | load C18 · atomic replace C19 · delayed writer C22 | - |
| GET /v1/workspaces/{ws}/sessions/{sid}/history statuses (5) | 200 C12 · 400 C12 · 401 C12 · 404 C12 · 500 C21 | - |
| Landing doors (5) | transcript JSON C10 · history semantics C12 · checkpoint transcript_len C14 · context_tokens C23 · context_parts C24 | - |
| Relations entities (4) | session C17 · context C9 · transcript C8 · checkpoint C14 | - |

## Swept

- validation: C2, C21
- failure modes: C18, C19
- idempotency: C7, C22
- authorization: C12, existing bearer authentication
- concurrency: C22
- data lifecycle: C10, C11, C14, C15, C16, C17
- dependency failure: C19; upstream reduction failures remain non-committing
- state transitions: C6, C7, C8, C20
- observability: existing compaction start/finished events and history_seq stay unchanged; C12 asserts the response field

## Handoff

One builder: estimated reading under 600 KB / 4 = 150k token budget before code. Final measured changed source/test files total 555,666 bytes / 4 = 138,917 tokens; no slice handoff needed.

- **Boundary:** C1–C24 closed in implementation commit `f1a1aea9`. All 57 parameterized/focused cases in `tests/test_session_transcript.py` passed, including every named proof selector. Independent light-profile verification passed at that commit; see `verification.md` for per-check evidence and limits.
- **Settled mid-build:** Added optional context_tokens for remote gauge parity and context_parts for normalization-stable checkpoint rewind. Preserved public upstream reduction and documented why result.new_messages alone cannot cover failure capture/approval rollback.
- **Abandoned:** A message-count archive cursor failed the actual post-summary Harness turn test because upstream merges adjacent request envelopes; replaced with part-position slicing and explicit rebasing for context-only repair.

Validation on 2026-09-16: ruff lint and format checks pass; pyright reports zero errors; full pytest passes (5128 passed, 9 skipped, 1 xfailed; coverage 95.47%); uv build produces sdist and wheel. The existing checkpoint/store test doubles were updated for the extended interfaces without weakening their assertions.

Focused red/green evidence: aggregate CLI measurement (15 initial failures); missing transcript API (reduction cases); reset/switch retention failures; restart losing archive; post-summary append lost at normalization; tool-only checkpoint archive truncation; normalized model-context rewind; HTTP transcript pagination and malformed handling; cached media wire-ref mutation and nested malformed-parts error. All were rerun green.

### Quality-gate follow-up

PR 151 exposed a missed CI-specific check: normal lint passed, but the quality gate's explicitly selected PLR0913 rule counted the added sixth `SessionStore.save` argument, increasing source violations from 57 to 58. The fix passes a typed `SessionMessages(context, transcript)` pair through the existing first argument, retaining the five-argument legacy list call and the same atomic writer. No suppression, threshold change, untyped argument hiding, or assertion weakening was used.

Additional compatibility proof for the existing persistence obligations: `uv run pytest --no-cov tests/test_session_transcript.py::test_paired_save_preserves_legacy_contract`. It failed before the fix on the extra parameter and now passes, asserting the exact five-argument signature, a legacy five-positional-argument save, and the paired save's equivalent context and metadata plus transcript.

The exact quality rule selection was rerun: `uv run ruff check --select C901,PLR0911,PLR0912,PLR0913,PLR0915 --no-cache --output-format=json src tests`. Against an exported `fb558301` baseline, normalized by relative path, rule, and message, source diagnostics are 57 → 57 and source-plus-test diagnostics are 91 → 91, with no new diagnostics. Ruff exits 1 for these pre-existing diagnostics; the comparison reports zero added violations.

Final source validation after the fix: normal ruff lint and format check pass, pyright reports zero errors, and full pytest passes (5129 passed, 9 skipped, 1 xfailed; coverage 95.47%). C1–C24 proofs remain intact; the focused transcript file now has 58 passing cases. Independent scoped re-verification is delegated to the orchestrator after this implementation commit.
