# Codex control test readiness

Profile: light

## Intent

PR #149's Python 3.10 CI failed two pre-existing Codex control tests because
they use a 300 ms sleep as evidence of an active turn. Test the same control
contract after observing readiness, with both normal and 500 ms delayed turn
startup. No production change or new persisted/API contract.

## Checks

**C1** - Cancelling an active turn records `turn/interrupt`, propagates CancelledError,
and a subsequent request returns `Hi there`, with both 0 and 0.5-second startup delay.
Proof: `uv run pytest --no-cov tests/test_codex_cli_model.py::test_cancel_interrupts_the_turn -v`

**C2** - Steering returns False while idle and True after turn readiness, and the
recorded `turn/steer` input equals `also check tests`, with both startup delays.
Proof: `uv run pytest --no-cov tests/test_codex_cli_model.py::test_steer_forwards_to_active_turn_only -v`

## Coverage

| Set (size) | Member -> proof | Unproven |
| --- | --- | --- |
| Startup delay (2) | 0 C1 C2; 0.5 C1 C2 | - |
| Control outcome (3) | interrupt C1; idle steering C2; active steering C2 | - |

## Swept

- validation: C1, C2
- failure modes: C1; bounded readiness waits fail on timeout
- idempotency: n/a - no production behavior changes
- authorization: n/a - fake-process tests only
- concurrency: C1, C2
- data lifecycle: existing - pytest temporary directories and model close cleanup
- dependency failure: existing - server failure coverage remains unchanged
- state transitions: C1, C2
- observability: existing - assertions inspect the fake's request log, not elapsed time

## Out of scope

Production Codex startup/cancellation behavior, unrelated tests, transcript preservation.

## Handoff

One builder: 100,662 bytes in the model, test, and fake source / 4 = 25,166 tokens,
below the 150k budget. Author: root. Independent verifier required after commit.

- **Boundary:** C1-C2 closed: both delayed cases reproduced the CI failures before
  the readiness fix; all four cases and 143 Claude/Codex model tests pass after it.
  Ruff lint/format and pyright pass. Full Python 3.10 suite is running separately.
- **Settled mid-build:** no product changes or assertion weakening; wait for the
  model's turn handle and fake-server log, bounded at five seconds.
- **Abandoned:** fixed startup sleeps; elapsed time does not prove turn readiness.
