# Codex control test readiness verification

**Verdict**: PASS
**Profile**: light
**Diff range**: bb117263..a67ce6f10201cdb75d138b7185da9a75fe65b96a
**Round**: 1 - full
**Verifier**: independent sub-agent cancel_verify (author != verifier; author: root)

## Checks

Verified at `a67ce6f10201cdb75d138b7185da9a75fe65b96a`. Both named proofs were
batched into the command below; every parameterized case appeared individually
and passed under Python 3.10.20.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | Active-turn cancellation sends interrupt, propagates CancelledError, and permits a subsequent `Hi there` response at both startup delays | `test_cancel_interrupts_the_turn[normal-start]` and `[slow-start]`, batched command exit 0 | `tests/test_codex_cli_model.py:394` — `with pytest.raises(asyncio.CancelledError)`; `tests/test_codex_cli_model.py:399` — `assert any(r["method"] == "turn/interrupt" for r in read_request_log(tmp_path))`; `tests/test_codex_cli_model.py:402` — `assert resp.parts[0].content == "Hi there"` | PASS |
| C2 | Steering returns False while idle, True when ready, and sends exactly `also check tests` at both startup delays | `test_steer_forwards_to_active_turn_only[normal-start]` and `[slow-start]`, batched command exit 0 | `tests/test_codex_cli_model.py:411` — `assert m.steer("nothing running") is False`; `tests/test_codex_cli_model.py:415` — `assert m.steer("also check tests") is True`; `tests/test_codex_cli_model.py:423` — `assert steer["params"]["input"][0]["text"] == "also check tests"` | PASS |

Startup preconditions are explicit at `tests/test_codex_cli_model.py:367`
(`params=[0, 0.5]`) and readiness is observed at lines 392 and 414 before the
control calls. Both proofs changed in the feature diff.

## Level and sampling

The proofs cross the model-to-subprocess protocol boundary using the scripted
fake app server and inspect its recorded requests. That level matches these
test-readiness obligations. Both specified startup delays ran; no sampling gap
within C1-C2 was found. Real Codex CLI behavior, arbitrary scheduling delays,
and production startup cancellation are outside these claims.

Profile light: no fault injection or independent coverage-join recomputation
was performed. No separate plan, binding source, or Test policy section exists
for this small test-only feature.

## Swept existing

Re-read at the verified commit:

- Data lifecycle: both tests use pytest's `tmp_path`; their finalizers cancel
  and gather the request task and call `m.aclose()` at
  `tests/test_codex_cli_model.py:403` and `tests/test_codex_cli_model.py:424`.
- Dependency failure: unchanged `test_server_crash_raises_with_stderr_tail`
  at `tests/test_codex_cli_model.py:358` asserts
  `pytest.raises(CliModelError, match="exited mid-turn")` and closes the model.
  This existing constraint was inspected, not added to the proof batch.
- Observability: the assertions cited for C1-C2 inspect actual recorded
  methods and payloads. `tests/fakes/__init__.py:36` reads the fake's request
  log; readiness polling is bounded by `asyncio.wait_for(poll(), timeout=5)`
  at `tests/test_codex_cli_model.py:384`.

## Additional PR review

Reviewed the full `fb558301..a67ce6f10201cdb75d138b7185da9a75fe65b96a`
diff, including the Claude cancellation helper and its regression. No concrete
correctness issue was found in the changed code. The helper covers all three
first-object reads and delegates interruption to the existing process cleanup
contract. The added Claude regression independently passed in the same batch.
This review is independent human-style agent review, not the automated review
bot's result.

## Gate

Independent proof command:

```bash
UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync pytest --no-cov -n 0 tests/test_codex_cli_model.py::test_cancel_interrupts_the_turn tests/test_codex_cli_model.py::test_steer_forwards_to_active_turn_only tests/test_claude_cli_model.py::test_cancel_before_first_stream_object_interrupts_the_open_turn -vv
```

Exit 0: 5 passed in 1.97 seconds, comprising four required proof cases and the
additional Claude regression. `git diff --check fb558301..HEAD` also exited 0.

Completion validator:
`UV_CACHE_DIR=/tmp/marim-harness-uv-cache uv run --no-sync python /home/mateuscmarim/.config/marim/skills/tlc-spec-lean/scripts/validate_verification.py codex-control-test-readiness`
— exit 0, 0 errors, 0 warnings.
