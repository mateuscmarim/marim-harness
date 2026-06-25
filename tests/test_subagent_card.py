from marim_harness.interfaces.tui.widgets.subagent import SubAgentWidget


def test_card_has_no_body_and_tolerates_no_pane():
    w = SubAgentWidget("research", "Map the codebase. Then summarize.", "sonnet")
    # The transcript body no longer lives on the card.
    assert not hasattr(w, "body")
    assert w.pane is None
    # Scalar updates must not blow up before a pane is attached.
    w.set_usage(1000, "$0.01", "in 0.8k · out 0.2k")
    assert w.tokens == 1000 and w.cost_text == "$0.01"
    w.finish("done report", status="done")
    assert w.status == "done"


def test_finish_failure_appends_to_pane_when_present():
    class _Pane:
        def __init__(self):
            self.usage = None
            self.errors = []

        def set_usage_line(self, d):
            self.usage = d

        def append_error(self, r):
            self.errors.append(r)

    w = SubAgentWidget("research", "task", "sonnet")
    w.pane = _Pane()
    w.set_usage(10, None, "in 10")
    assert w.pane.usage == "in 10"
    w.finish("Sub-agent 'x' failed: boom", status="failed")
    assert w.pane.errors == ["Sub-agent 'x' failed: boom"]
    assert w._fail_reason == "boom"
