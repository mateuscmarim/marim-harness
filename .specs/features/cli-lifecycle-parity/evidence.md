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

## T3 Codex adapter

Gate: 246 passed (Codex lifecycle/translator/model/server/turn plus Claude
lifecycle/model/process/demux); Ruff scoped PASS; full Pyright 0 errors.
The prior started-compaction assertion was corrected to require no success
at start AND an exact completion notice; no tests were removed or skipped.

| Obligation | Evidence | Outcome |
| --- | --- | --- |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:23 `assert event("item/started", "c1") == []` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:25 `assert first[0].message == "Codex compacted its context"` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:26 `assert first[0].kind == "compaction"` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:27 `assert t.translate("thread/compacted", {"turnId": "t"}) == []` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:28 `assert event("item/completed", "c1") == []` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:30 `assert len(second) == 1 and second[0].id != first[0].id` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:31 `assert t.translate("thread/compacted", {"turnId": "t"}) == []` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:33 `assert len(other.translate("thread/compacted", {"turnId": "t"})) == 1` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:34 `assert (` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:50 `assert t.translate(method, params)[0].message == "warning"` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:51 `assert t.translate(method, {"message": {}, "summary": None}) == []` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:53 `assert note.data == {"fromModel": "a", "toModel": "b", "reason": "future"}` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:54 `assert note.message == "Codex model rerouted: a → b"` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:55 `assert t.translate("model/rerouted", {"toModel": "b"}) == []` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:61 `assert _outputs("thread/compacted", {"turnId": "old"}, state, "current") == []` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:62 `assert state.context == ContextReport(900, 1000)` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:64 `assert notes[0].message == "Codex is retrying: busy"` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:65 `assert state.done is None` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:67 `assert seen == [None] and state.context is None and state.done is None` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:69 `assert state.done.status == "completed"` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:84 `assert handle.events.get_nowait() == ("configWarning", {"summary": "bad config"})` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:85 `assert handle.events.empty()` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:86 `assert remote.events.empty()` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:113 `assert isinstance(seen[0], BackendNotice)` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:114 `assert seen[0].message == "Codex model rerouted: a → b"` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:117 `assert [part.content for part in parts if isinstance(part, TextPart)] == ["before", "", "after"]` | PASS |
| LIFE-01/02/03/04/12/16/26/28/31/34 component outcomes | tests/test_codex_lifecycle.py:118 `assert notice_from_part(parts[1])["id"] == seen[0].id` | PASS |

All new assertions exercise the listed notice, ordering, context, retry or routing obligations.
Full transport-to-client proof remains T4/T6/T8.

## T4

Fourteen lifecycle delivery tests pass, including both fake backends through Harness, host, event bus and disk history. Final 93 targeted and earlier 187 regression tests passed. Owned Ruff passes and full Pyright 0 errors. No existing tests changed.

| Assertion evidence | Result |
| --- | --- |
| tests/test_lifecycle_delivery.py:31 `assert isinstance(wire, SessionNotice)` | PASS |
| tests/test_lifecycle_delivery.py:32 `assert wire.id == "notice-1"` | PASS |
| tests/test_lifecycle_delivery.py:33 `assert wire.message == notice.message` | PASS |
| tests/test_lifecycle_delivery.py:35 `assert isinstance(old, SessionNotice)` | PASS |
| tests/test_lifecycle_delivery.py:36 `assert old.id is None` | PASS |
| tests/test_lifecycle_delivery.py:55 `assert event.data == notice.to_payload()` | PASS |
| tests/test_lifecycle_delivery.py:56 `assert event.seq < terminal.seq` | PASS |
| tests/test_lifecycle_delivery.py:57 `assert not any(e.type == "compaction.finished" for e in events)` | PASS |
| tests/test_lifecycle_delivery.py:58 `assert terminal.data["output"] == "beforeafter"` | PASS |
| tests/test_lifecycle_delivery.py:72 `assert await run_headless(harness, "go", output_format, out=out, err=err) == 0` | PASS |
| tests/test_lifecycle_delivery.py:73 `assert err.getvalue() == "notice only\n"` | PASS |
| tests/test_lifecycle_delivery.py:74 `assert "notice only" not in out.getvalue()` | PASS |
| tests/test_lifecycle_delivery.py:75 `assert "answer" in out.getvalue()` | PASS |
| tests/test_lifecycle_delivery.py:91 `assert [type(w) for w in log.children] == [` | PASS |
| tests/test_lifecycle_delivery.py:100 `assert len(log.query(NoticeMessage)) == 2` | PASS |
| tests/test_lifecycle_delivery.py:113 `assert order_response_parts(parts) == parts` | PASS |
| tests/test_lifecycle_delivery.py:118 `assert isinstance(mounted[1], NoticeMessage)` | PASS |
| tests/test_lifecycle_delivery.py:124 `assert isinstance(mounted[-1], NoticeMessage)` | PASS |
| tests/test_lifecycle_delivery.py:126 `assert len(mounted) == 4` | PASS |
| tests/test_lifecycle_delivery.py:140 `assert len(app.stream.backend_tasks) == 1` | PASS |
| tests/test_lifecycle_delivery.py:142 `assert widget.status == "failed"` | PASS |
| tests/test_lifecycle_delivery.py:143 `assert widget.result_text == "testing (interrupted)"` | PASS |
| tests/test_lifecycle_delivery.py:144 `assert widget.args == {"description": "testing"}` | PASS |
| tests/test_lifecycle_delivery.py:145 `assert app.stream.subagents == []` | PASS |
| tests/test_lifecycle_delivery.py:161 `assert remainder is None` | PASS |
| tests/test_lifecycle_delivery.py:163 `assert routed.stream_id == "child"` | PASS |
| tests/test_lifecycle_delivery.py:164 `assert isinstance(routed.event, BackendNotice)` | PASS |
| tests/test_lifecycle_delivery.py:166 `assert isinstance(message, ModelResponse)` | PASS |
| tests/test_lifecycle_delivery.py:167 `assert message.parts[0].content == ""` | PASS |
| tests/test_lifecycle_delivery.py:168 `assert notice_from_part(message.parts[0])["id"] == routed.event.id` | PASS |
| tests/test_lifecycle_delivery.py:169 `assert demux.route(obj) == ([], None)` | PASS |
| tests/test_lifecycle_delivery.py:185 `assert sid == "child"` | PASS |
| tests/test_lifecycle_delivery.py:186 `assert isinstance(notice, BackendNotice)` | PASS |
| tests/test_lifecycle_delivery.py:188 `assert notice_from_part(message.parts[0])["id"] == notice.id == "same-id"` | PASS |
| tests/test_lifecycle_delivery.py:203 `assert host.pending_asks() == before` | PASS |
| tests/test_lifecycle_delivery.py:204 `assert host.pending_asks()[0]["id"] == ask.id` | PASS |
| tests/test_lifecycle_delivery.py:205 `assert not ask.future.done()` | PASS |
| tests/test_lifecycle_delivery.py:206 `assert not any(e.type in ("ask.resolved", "turn.finished") for e in events)` | PASS |
| tests/test_lifecycle_delivery.py:224 `assert notice_from_part(message.parts[0])["id"] == "private-id"` | PASS |
| tests/test_lifecycle_delivery.py:225 `assert message.parts[1].content == "still working"` | PASS |
| tests/test_lifecycle_delivery.py:226 `assert "codex-cli" in caplog.text` | PASS |
| tests/test_lifecycle_delivery.py:227 `assert "private" not in caplog.text` | PASS |
| tests/test_lifecycle_delivery.py:268 `assert notice.seq < terminal.seq` | PASS |
| tests/test_lifecycle_delivery.py:269 `assert notice.data["backend"] == f"{backend}-cli"` | PASS |
| tests/test_lifecycle_delivery.py:270 `assert notice.data["message"] not in terminal.data["output"]` | PASS |
| tests/test_lifecycle_delivery.py:271 `assert terminal.data["output"].replace("\n", "") == "beforeafter"` | PASS |
| tests/test_lifecycle_delivery.py:272 `assert not any(e.type == "compaction.finished" for e in events)` | PASS |
| tests/test_lifecycle_delivery.py:275 `assert persisted == [notice.data]` | PASS |
| tests/test_lifecycle_delivery.py:278 `assert any(` | PASS |
| tests/test_lifecycle_delivery.py:282 `assert any(` | PASS |

Adequacy: assertions target this task’s specified behavior; no unrelated tests added.
Feature-wide AC mapping and adversarial checks follow in validation.md.
