# Phase 3 verification evidence

## T1 — ordered notice component

Gate: ruff check src tests PASS; pytest --no-cov -n 0
 tests/test_cli_lifecycle.py tests/test_cli_activity_history.py: 16 passed.
Red checks failed first for the missing component and then for a notice between
parallel results. Fixed the implementation; no assertion was weakened.

| Requirement / done condition | Assertion evidence | Expected outcome | Verdict |
| --- | --- | --- | --- |
| LIFE-26 ordered display metadata | tests/test_cli_lifecycle.py:25 `[p.content for p in parts] == ["before", "", "", "after"]` | Notices occupy display positions without prose | PASS |
| LIFE-28 occurrence identity | tests/test_cli_lifecycle.py:27 `first.id != second.id`; :28 expansion idempotence | Same-text occurrences stay distinct | PASS |
| LIFE-32 callback failures | tests/test_cli_lifecycle.py:38-41 backend/type present, private strings absent | Turn continues with safe diagnostics | PASS |
| LIFE-06 stderr foundation | tests/test_cli_lifecycle.py:48 `captured.out == ""`; :49 exact stderr | No assistant stdout contamination | PASS |
| LIFE-31 malformed metadata | tests/test_cli_lifecycle.py:54 `notice_from_part(part) is None` | Ignore invalid display metadata | PASS |
| Parallel-result edge | tests/test_cli_lifecycle.py:69 real contents `["first", "second"]`; :70 note metadata | Notice does not synthesize unfinished returns | PASS |

Reverse mapping: all five new tests map to the rows above; no speculative tests.
Adequacy: T1 foundation outcomes covered. End-to-end delivery and client replay
remain pending their integration tasks; no feature-level AC is declared fully
verified solely from these unit tests. Project AGENTS.md conventions followed.

Design refinement: notices between parallel results use application-only
ToolReturnPart.metadata.backend_notices_after. This preserves exact result order
without sealing the parallel result batch. Other notices use blank TextPart
provider metadata. Both representations remain outside model-facing prose.

## T2 — Claude lifecycle adapter

Gate: ruff and pyright PASS; 135 focused and existing tests passed (Claude
lifecycle/model/process and CLI demux). Existing tests unchanged. Public
compaction, refusal fallback, notifications and permission denials are parsed;
thinking/state/VCS observations, shell cards and init inventory are kept by the
owning process. Result details use ModelResponse.metadata.backend_result.

| Criterion | Evidence assertion | Outcome |
| --- | --- | --- |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:22 `assert isinstance(notice, BackendNotice)` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:23 `assert notice.kind == "compaction"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:24 `assert notice.data == {"pre_tokens": 900, "post_tokens": 200, "trigger": "auto"}` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:25 `assert notice.id == "claude:session:event"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:26 `assert state.consume(obj) == []` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:31 `assert state.consume({"subtype": "model_fallback", "content": "private"}) == []` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:32 `assert state.consume({"subtype": "compact_boundary", "compact_metadata": []}) == []` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:33 `assert state.consume({"subtype": "unknown"}) == []` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:34 `assert state.consume({"subtype": "notification", "text": {"secret": 1}}) == []` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:46 `assert notice.message == "Claude model fallback: one → two"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:47 `assert notice.kind == "model_refusal_fallback"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:52 `assert state.consume({"subtype": "thinking_tokens", "estimated_tokens": 42}) == []` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:53 `assert state.thinking_tokens == 42` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:54 `assert state.consume({"subtype": "thinking_tokens", "estimated_tokens": True}) == []` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:55 `assert state.thinking_tokens == 42` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:57 `assert state.backend_state == "requires_action"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:66 `assert state.inventory["tools"] == ["Read"]` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:67 `assert state.inventory["mcp_servers"] == [{"name": "docs", "status": "connected"}]` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:69 `assert state.inventory["tools"] == ["Bash"]` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:70 `assert state.inventory["slash_commands"] == []` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:75 `assert (` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:88 `assert started.task_id == "claude:s:shell" and started.status == "running"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:92 `assert progress.task_id == started.task_id and progress.status == "running"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:101 `assert finished.task_id == started.task_id and finished.status == "completed"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:102 `assert (` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:108 `assert closed.status == "interrupted"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:123 `assert state.result_details == {` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:166 `assert seen == [("Claude compacted its context", ContextReport(100, None))]` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:169 `assert [p.content for p in parts] == ["before", "", "after"]` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:170 `assert notice_from_part(parts[1])["id"] == "claude:s:compact"` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:171 `assert response.metadata["backend_result"]["num_turns"] == 1` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:194 `assert model.context_report is None` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:195 `assert current_context_report(model, history) is None` | PASS |
| Claude parser/stream ACs (LIFE-01/02/11/12/13/14/18-23/29/31) | tests/test_claude_lifecycle.py:233 `assert [(e.task_id, e.status) for e in seen] == [` | PASS |

Reverse mapping: parser tests cover field values and malformed/private inputs;
fake-stream test covers live order, context and persisted marker identity;
closure test covers the same shell task changing from running to interrupted.
No speculative tests; existing background-agent and process tests still pass.
LIFE-33 child-stream integration remains explicitly pending T4; no root notice
is emitted for a child-tagged event. UI inventory rendering remains T5.
