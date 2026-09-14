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
