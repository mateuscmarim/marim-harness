# Upstream tool-output limits checks

Profile: light
Plan: `.specs/features/upstream-output-limits/plan.md`

20 checks in 3 slices · 2 one-way doors · 0 open questions.
Build status: C1–C20 closed; independent verification PASS at `d6be53e0`.
Proof commands use the repository pytest runner. New selectors below are executable
obligations to implement before claiming their checks closed.

## Checks

### S1 — Ordinary returns

**C1** - Main and native child Agent runs reduce eligible ordinary tool returns (AC 1).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_native_agents_reduce`

**C2** - Empty and 9,999-character text reach the model unchanged (AC 2).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_text_boundaries`

**C3** - 10,000-character and larger text spill with complete retrievable payloads (AC 3).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_text_boundaries`

**C4** - Oversized structured MCP output stores indented JSON decoding to the input (AC 4).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_structured_mcp_return`

**C5** - OSError during spill produces a result of at most 4,000 characters (AC 5).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_storage_failure`

**C6** - Reduction adds zero model requests to a two-request function-tool run (AC 6).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_native_agents_reduce`

### S2 — Owning session

**C7** - A fresh capability for the same session reads a saved handle after compaction and JSON history restoration (AC 7).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_resume_after_compaction`

**C8** - A removed spill returns missing-result text without executing its producer (AC 8).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_retrieval`

**C9** - Parallel distinct call IDs and separate runs reusing an ID retrieve their respective payloads (AC 9).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_call_identity`

**C10** - Switching the live session leaves in-flight main runs, child runs and detached report destinations in the captured original root (AC 10).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_session_capture`

**C11** - Outside-root symlinks reveal no target content; retrieval validates offsets and provides bounded pages and literal search (AC 11 and Surface).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_retrieval`

**C12** - Old absolute pointers retain missing-file annotation and live-file behavior (AC 12).
Proof: `uv run pytest --no-cov tests/test_compaction.py::test_revalidate_annotates_dangling_handle_and_keeps_preview tests/test_compaction.py::test_revalidate_leaves_live_handle_untouched`

### S3 — Execution contracts

**C13** - Direct, mixed and ToolReturn-wrapped media reach the model with the original bytes (AC 13).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_media_preserved`

**C14** - Shell collection and HTTP body collection remain bounded before reduction (AC 14).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_producer_bounds`

**C15** - Auto, ask and plan retain execution/denial decisions before output reduction (AC 15).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_approval_modes`

**C16** - Native agents expose one read_tool_result, including with tool deferral; duplicate user registration raises an error (AC 16).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_retrieval_registration`

**C17** - Upstream overflow metadata and tool-call/result IDs survive JSON history serialization; model and stream observers see preview content (AC 17).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_history_and_stream`

**C18** - Explicit subagent report budgets and workflow final-result caps still return within-budget pointers to the full report (AC 18).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_report_budgets`
Proof: `uv run pytest --no-cov tests/test_subagent_tool.py::test_cap_over_budget_spills_full_and_returns_pointer_within_budget`

**C19** - Covered fs/search/tree/glob, completed shell, fetch, skill body/resource and MCP paths contain no active legacy general writer (AC 19).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_legacy_writers_removed`

**C20** - Spilled command output retains its exit-status and timeout indicators (AC 20).
Proof: `uv run pytest --no-cov tests/test_output_limits.py::test_command_status`

## Coverage

| Set (size) | Member -> proof | Unproven |
| --- | --- | --- |
| Native assemblies (2) | main C1 · child C1 | - |
| Text bands (4) | empty C2 · 9999 C2 · 10000 C3 · 20000 C3 | - |
| Storage lifecycle (4) | same-session resume C7 · removed C8 · session switch C10 · sessionless C10 | - |
| Retrieval outcomes (4) | bounded page C11 · literal search C11 · invalid offset C11 · missing C8 | - |
| Media shapes (3) | direct C13 · mixed C13 · wrapped C13 | - |
| Permission modes (3) | auto C15 · ask C15 · plan C15 | - |
| Producers (7) | fs C19 · search C19 · shell C19 · fetch C19 · skill body C19 · skill resource C19 · MCP C4 | - |
| Report boundaries (2) | subagent C18 · workflow C18 | - |
| Landing doors (2) | handle/metadata C17 · session-root resolution C7 | - |
| Relations (4) | session owns root C10 · many calls C9 · persisted payload C7 · legacy coexistence C12 | - |

## Swept

- validation: C2, C3, C4, C11, C14
- failure modes: C5, C8, C20
- idempotency: C9
- authorization: C11, C15
- concurrency: C9, C10
- data lifecycle: C7, C8, C12
- dependency failure: C5; existing - pinned dependency qualification and repository CI matrix
- state transitions: C7, C10, C17
- observability: C1, C17, C20

## Handoff

Single implementation batch: focused runtime/tool changes and associated tests,
approximately 90 KB of directly relevant source (~24k tokens); use narrow reads.
Independent verifier follows the final implementation commit over all 20 checks.
Compaction branch integration remains a pre-merge obligation if that branch has not landed.

- **Boundary:** C1–C20 closed in the implementation commit containing this update.
  All 31 parameterized proof cases passed. Full non-live suite: 4,995 passed,
  7 skipped, 95.38% coverage on Python 3.13.14. Ruff lint/format, pyright and
  wheel/sdist build passed. Supported-version qualification continues separately.
- **Settled mid-build:** user approved the full proposed plan/defaults. No scope
  changes. The new retrieval-response test initially guessed error wording;
  corrected it to the approved upstream response without changing missing-file
  or no-disclosure assertions. An initial custom-tool fixture lacked RunContext;
  corrected the fixture to the builder's documented tool signature.
- **Abandoned:** none. Existing tests tied solely to deleted ordinary-output
  preview writers were replaced by producer and shared-capability assertions,
  as approved. Existing instruction-registration tests now unwrap core 2.43's
  SourcedInstruction recipe; their registration assertions remain intact. This
  is test-only private introspection, with no production private API dependency.

Independent verification round 1 at `89067d37`: 19/20 proven, C11 had an
insufficiently discriminating literal-filter assertion. The follow-up proof adds
nonmatching rows that a regex would match, and checks filtering before offsets.
No production change was needed; re-verification is required at the follow-up commit.

Round 2 at `d6be53e0`: independent PASS, 20/20 checks proven and all 32 proof
cases passed. The completion-artifact validator passed with zero errors/warnings.
See [verification.md](verification.md) and [qualification.md](qualification.md)
for the evidence, full supported-Python matrix, integration snapshot and limits.
