import dataclasses

import pytest


def test_turn_outcome_fields_and_defaults():
    from marim_harness.runtime.outcome import TurnOutcome

    ok = TurnOutcome(subtype="success", result="hello", structured_output=None, errors=None)
    assert ok.subtype == "success"
    assert ok.result == "hello"
    assert ok.structured_output is None
    assert ok.errors is None
    # `usage` defaults to an empty RunUsage so hand-built outcomes (tests,
    # adapters) need not supply one; the controller always does.
    assert ok.usage.requests == 0
    assert ok.usage.total_tokens == 0


def test_turn_outcome_is_frozen():
    from marim_harness.runtime.outcome import TurnOutcome

    ok = TurnOutcome(subtype="success", result="x", structured_output=None, errors=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ok.result = "y"


def test_turn_outcome_subtypes():
    from marim_harness.runtime.outcome import TurnOutcome

    # The full subtype vocabulary (error_during_execution is reserved for
    # Claude parity; v1 never emits it).
    for subtype in ("success", "error_max_structured_output_retries", "error_during_execution"):
        TurnOutcome(subtype=subtype, result=None, structured_output=None, errors=None)


def test_turn_outcome_lazy_top_level_export():
    import marim_harness
    from marim_harness.runtime.outcome import TurnOutcome

    assert marim_harness.TurnOutcome is TurnOutcome
    assert "TurnOutcome" in dir(marim_harness)
