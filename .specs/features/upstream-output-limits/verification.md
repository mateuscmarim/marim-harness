# Upstream tool-output limits verification

**Verdict**: PASS
**Profile**: light
**Diff range**: 281630a73b602d12a7f1ad9e2ac4f2af68b733cb..d6be53e0d2d629789c50aba0eebfa41eb1dca7cd
**Round**: 2 - scoped
**Verifier**: independent sub-agent (author != verifier), fresh context, read-only code review

All 20 checks are proven at the approved light level. The complete proof batch passed at d6be53e0. C11 now distinguishes literal matching from regex matching and ignored patterns, and verifies filtering before pagination.

## Scoped review and provenance

Reviewed fix diff `89067d37..d6be53e0`: only the retrieval-test payload/assertions and verification-handoff prose changed. No production code, shared fixture, dependency or configuration changed. C11 and the affected C8 retrieval assertions were rejudged at d6be53e0; other check judgments carry from independently reviewed 89067d37. All named proofs reran at d6be53e0, and line citations below were refreshed in the changed test file.

## Ranked gaps

None. Round 1's C11 evidence gap is closed by `tests/test_output_limits.py:150`–153: the result includes literal rows 0 and 2, excludes regex-only row 1, and an offset of one returns row 2 rather than row 0. No production change was required.

## Proof run

At HEAD, ran the complete named set in one pytest invocation, plus the located complementary MCP media test:

```bash
uv run pytest --no-cov -vv tests/test_output_limits.py tests/test_compaction.py::test_revalidate_annotates_dangling_handle_and_keeps_preview tests/test_compaction.py::test_revalidate_leaves_live_handle_untouched tests/test_subagent_tool.py::test_cap_over_budget_spills_full_and_returns_pointer_within_budget tests/test_mcp.py::test_hook_preserves_mixed_media_result
```

Exit 0: **32 passed in 1.75s**, Python 3.13.14, 12 xdist workers. Every parameterized case appeared individually as PASSED. The single round-2 invocation used `-vv` to overcome repository quiet settings and included the same complementary MCP case as round 1. No live-provider tests ran.

All proof names were located with `rg -n` and their assertions inspected. The new output-limit tests and MCP media test are in the feature diff. The unchanged compaction-pointer and report-cap tests are intentionally preservation proofs rather than claims to exercise new implementation.

## Checks

All proof names below refer to the round-2 complete invocation at d6be53e0 and passed. C8/C11 were rejudged at d6be53e0; all other judgments carry from 89067d37, with current line references. `OL` means `tests/test_output_limits.py`; evidence uses full paths for unambiguous locations.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | Main and native child reduce ordinary returns | OL::test_native_agents_reduce[False,True] | `tests/test_output_limits.py:77` — `assert "read_tool_result" in part.content`; line 80 — `assert await store.read(handle) == payload.encode()` | PASS |
| C2 | Empty and 9,999-character text unchanged | OL::test_text_boundaries[0,9999] | `tests/test_output_limits.py:550` — `assert model_return.content == text`; sizes are the local parameterization at line 527 | PASS |
| C3 | 10,000 and 20,000 characters spill completely | OL::test_text_boundaries[10000,20000] | `tests/test_output_limits.py:552` — `assert "read_tool_result" in model_return.content`; line 553 — `assert await store.read(model_return.metadata["overflow_handle"]) == text.encode()` | PASS |
| C4 | Structured MCP data stores multiline JSON | OL::test_structured_mcp_return | `tests/test_output_limits.py:110` — `assert json.loads(raw) == payload`; line 111 — `assert raw.count(b"\n") > 200` | PASS |
| C5 | OSError fallback at most 4,000 characters | OL::test_storage_failure | `tests/test_output_limits.py:567` — `assert len(result) <= 4000`; OSError injection at line 561 | PASS |
| C6 | Zero additional model requests | OL::test_native_agents_reduce[False,True] | `tests/test_output_limits.py:81` — `assert result.usage.requests == 2` | PASS |
| C7 | Restore saved handle after compaction | OL::test_resume_after_compaction | `tests/test_output_limits.py:264` — `assert changed and len(compacted) < len(history)`; line 296 — `assert "restored payload" in seen[-1]` after JSON restoration and a fresh session-matched Agent | PASS |
| C8 | Removed file gives missing result without producer rerun | OL::test_retrieval | `tests/test_output_limits.py:164` — `assert "No stored tool result" in missing`; line 165 — `assert not (tmp_path / "store" / handle).exists()`; invocation is the read-only retrieval tool without an available producer callback | PASS |
| C9 | Distinct parallel/reused IDs preserve payloads | OL::test_call_identity | `tests/test_output_limits.py:126` — `assert len(set(handles)) == 3`; line 128 — `assert await store.read(handle) == (char * 20000).encode()` for three locally specified run/call identities | PASS |
| C10 | Session changes preserve captured roots | OL::test_session_capture | `tests/test_output_limits.py:219` — `assert await a.capability().store.read(part.metadata["overflow_handle"])`; line 220 — `assert str(tmp_path / "A" / "subagent-output") in report_result`; line 221 excludes B writes; lines 225,228,229 assert fallback/sessionless identity | PASS |
| C11 | Safe retrieval, pages, validation and literal filtering | OL::test_retrieval | `tests/test_output_limits.py:143` asserts rows 10/11 and excludes 12; lines 150–151 — `assert "row 0:" in filtered and "row 2:" in filtered`, `assert "row 1:" not in filtered`; line 153 — `assert "row 2:" in filtered_offset and "row 0:" not in filtered_offset`; lines 154–155 require ModelRetry for negative offset; line 160 — `assert "SECRET-DATA" not in denied` | PASS |
| C12 | Legacy pointer revalidation preserved | Both named test_compaction proofs | `tests/test_compaction.py:772` — `assert content == h + OFFLOAD_GONE_NOTE`; line 782 — `assert new_history is history` | PASS |
| C13 | Direct, mixed, wrapped media bytes preserved | OL::test_media_preserved[direct,mixed,wrapped]; test_mcp::test_hook_preserves_mixed_media_result | `tests/test_output_limits.py:603` — `assert any(item.data == binary.data for item in binaries)` after message JSON round-trip; `tests/test_mcp.py:694` — `assert result is payload`; line 695 — `assert result[1] is img` | PASS |
| C14 | Producer collection bounds retained | OL::test_producer_bounds | `tests/test_output_limits.py:449` — `assert len(head) + len(tail) == 1000`; line 450 — `assert buf.dropped == 99000`; lines 455–456 assert the 1,000-byte fetch notice and exclude 1,001 payload characters | PASS |
| C15 | Auto/ask/plan execution decisions preserved | OL::test_approval_modes[auto-False-True,ask-True-True,ask-False-False,plan-True-False] | `tests/test_output_limits.py:333` — `assert bool(calls) is executed`; line 335 — `assert any(p.is_file() for p in files) is executed`; expected decisions are local parameters | PASS |
| C16 | One retrieval tool with deferral and collision rejection | OL::test_retrieval_registration[False-False,True-False,False-True] | `tests/test_output_limits.py:366` — `assert "registered payload" in messages[-1].parts[0].content`; line 387 — `assert names.count("read_tool_result") == 2`; line 393 requires UserError matching the duplicate name | PASS |
| C17 | Metadata/IDs survive serialization and preview streams | OL::test_history_and_stream | `tests/test_output_limits.py:427` — `assert restored_part.metadata == part.metadata`; lines 428–430 assert IDs and both overflow metadata keys; line 434 — `assert "read_tool_result" in rendered`; line 436 excludes ToolReturn repr | PASS |
| C18 | Explicit report budgets retain full reports and pointers | OL::test_report_budgets; named test_subagent_tool proof | `tests/test_output_limits.py:466` — `assert len(capped) <= 1000`; line 467 checks exact report file; lines 470–472 check workflow limit, full JSON spill and pointer; `tests/test_subagent_tool.py:109` — `assert spill == out`; lines 111–112 check limit and pointer | PASS |
| C19 | Retired general writers removed from covered paths | OL::test_legacy_writers_removed | `tests/test_output_limits.py:493` — `assert retired not in source, (relative, retired)` over the five local source modules and four retired symbols; source diff also confirms removal from fs/search/tree/glob, shell, fetch, skills and MCP | PASS |
| C20 | Command exit/timeout indicators remain in stored payload | OL::test_command_status[False,True] | `tests/test_output_limits.py:506` — `assert "timed out" in raw.lower() if timeout else "7" in raw`; line 509 — `assert await store.read(reduced.metadata["overflow_handle"]) == raw.encode()` | PASS |

## Level and sampling judgment

Carried from 89067d37 except C8/C11, rejudged at d6be53e0 after the retrieval-test change.

- C1–C4, C6–C7, C13, C15–C17 exercise actual deterministic Agent runs or Harness.run_turn; no paid provider is necessary for their message/tool contracts. C4 additionally traverses the real MCP approval hook with a stand-in server call. C13's supplementary MCP seam test is lower-level, combined with the three Agent/media cases; it does not claim a live MCP transport/provider test.
- C5, C8–C9 and C11 use the public capability/toolset/store boundaries directly. This is the owned integration boundary and appropriate to the storage/retrieval claims. C11's safety, paging, negative-offset validation and discriminating literal filtering are proven at d6be53e0.
- C10 places the main run and report job in flight, and builds the child before switching sessions before running that child. Capturing a child at construction and retaining that destination even when its run starts later is a valid light-profile check of its fixed root. This does not exercise every scheduling interleaving.
- C14 tests shell collection at its actual buffer and fetch through its collection function. C18 tests report shaping and storage at their application boundaries. C20 uses a real local command/timeout and the actual upstream storage boundary.
- C16 samples main, child, and main with deferred loading; the child-plus-deferral cross-product is not separately exercised. The same upstream capability supplies retrieval in both native assemblies. This light-profile sampling is accepted, without claiming every cross-product.
- C12 is an unchanged compatibility regression check. C19 is a structural absence assertion, appropriate because its claim is the retirement of named active writers. No new behavior is claimed solely from these preexisting compatibility proofs.
- Coverage-join recomputation and fault injection were not performed, as specified by the approved light profile. No Test policy section exists in checks.md. These limits are explicit; a green run alone is not mutation evidence.

## Swept existing and upstream-first review

Carried from 89067d37: the fix changed no source, dependency, configuration or CI constraint.

The only `Swept` entry delegated to existing constraints is dependency qualification/CI. `pyproject.toml:33–34` requires core >=2.43,<3 and Harness ==0.31.0; `uv.lock:1548–1563` locks Harness 0.31.0 and core 2.43.0. Installed Harness metadata declares Python >=3.10 and core >=2.40.0. `.gitea/workflows/ci.yml` actually contains the 3.10/3.12/3.14 matrix, lint, formatting, pyright, pytest and the 3.12 build job. This verifies the constraint exists, not that this verifier ran the matrix. The parent owns current matrix evidence.

The production change delegates serialization, limits, clipping, storage and retrieval to public upstream APIs. The 85-line Marim output module owns media exemption and session-root selection; main-agent DynamicCapability and explicit child capability attachment preserve the intended construction seam. Report budgets and collection bounds remain by approved scope. Legacy pointer readers remain. Source diff review found no new production dependence on an upstream private API.

Read the supplied upstream-first AGENTS instructions, worktree AGENTS.md, plan.md, checks.md and verify.md. The [upstream output-limits documentation](https://pydantic.dev/docs/ai/harness/tool-output-limits/) opened successfully. The plan's three GitHub pinned-source URLs returned tool errors, so the installed 0.31.0 implementation and distribution metadata were inspected locally instead; no claim is made that those GitHub pages opened. This is not a UI-profile binding-source enumeration.

## Gate and remaining obligations

Proof runner at d6be53e0: **32 passed, 0 failed**. Overall light-profile verification: PASS, 20/20 checks.

The orchestrator requested this report only in scratchpad and will copy it to the feature directory. The deterministic completion script reads only `.specs/features/<name>/verification.md`; it was therefore not invoked against a nonexistent feature report, and no gate success is claimed. Run the deterministic completion gate after copying this report; its result is a separate artifact check, not a substitute for the independent proof run.

No tracked production/test files were edited. The worktree was clean at inspected HEAD. Supported-Python qualification and compaction-branch integration evidence remain owned and documented by the orchestrator. The parent reports those runs green; this verifier did not execute them and makes no independent matrix/integration success claim. The grounded round-1 lesson remains: a literal-search test needs both a positive literal match and a regex-only/nonmatching counterexample.
