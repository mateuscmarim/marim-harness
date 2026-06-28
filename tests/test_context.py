from marim_harness.runtime.context import plan_mode_preamble


def test_plan_mode_preamble_mentions_present_plan_and_read_only():
    text = plan_mode_preamble()
    assert "present_plan" in text
    assert "PLAN MODE" in text
