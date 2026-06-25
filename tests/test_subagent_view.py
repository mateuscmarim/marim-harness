from marim_harness.interfaces.tui.subagent_view import SubAgentViewer, spend_tag


def test_spend_tag_empty_when_no_tokens():
    assert spend_tag(0, 100_000) == ""


def test_spend_tag_tokens_only_when_no_max_ctx():
    # human_tokens(1500) -> "1.5k"; no percentage without a context size.
    assert spend_tag(1500, 0) == "1.5k"


def test_spend_tag_includes_percentage_when_max_ctx_known():
    # 1500 / 150000 = 1% share.
    assert spend_tag(1500, 150_000) == "1.5k (1%)"


def test_viewer_defaults():
    v = SubAgentViewer()
    assert v.open is False
    assert v.index == 0


def test_clamp_pins_into_range_and_returns_index():
    v = SubAgentViewer()
    v.index = 9
    assert v.clamp(3) == 2  # last valid index for 3 items
    assert v.index == 2


def test_clamp_floors_at_zero():
    v = SubAgentViewer()
    v.index = -5
    assert v.clamp(4) == 0
    assert v.index == 0


def test_prev_next_step_the_cursor():
    v = SubAgentViewer()
    v.next()
    v.next()
    assert v.index == 2
    v.prev()
    assert v.index == 1
