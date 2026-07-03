import marim_harness.interfaces.tui.widgets.subagent as subagent_mod
from marim_harness.interfaces.tui.widgets.subagent import SubAgentWidget


def test_display_title_is_derived_once_and_cached(monkeypatch):
    """_paint_header runs on every spinner tick (10 Hz, ×N running agents) and asks
    for display_title() to redraw one glyph. agent_task is fixed at construction, so
    the (whitespace-collapsing, separator-scanning) derivation must happen once and
    be cached — not re-condense the whole spawn prompt per frame."""
    calls = {"n": 0}
    real = subagent_mod.derive_title

    def counting(task):
        calls["n"] += 1
        return real(task)

    monkeypatch.setattr(subagent_mod, "derive_title", counting)

    w = SubAgentWidget("research", "Map the codebase. Then summarize.", "sonnet")
    assert w.display_title() == "Map the codebase"
    w.display_title()
    w._paint_header()  # the per-tick caller
    assert calls["n"] == 1


def test_description_titles_card_without_displacing_full_task():
    """A short ``description`` is the title hint, not a replacement for the prompt.
    The header derives from ``description`` (so several same-type spawns stay
    distinguishable), but ``agent_task`` must keep the full prompt verbatim — that's
    what the pane's "▸ task" disclosure reveals. Regression: the card used to store
    ``description or task`` in one field, so a labelled spawn dropped its real prompt
    and the disclosure echoed the title."""
    full = "Research Codex/Agents SDK context. Map every entry point and report back."
    w = SubAgentWidget("research", full, "sonnet", description="Research Codex SDK")
    assert w.display_title() == "Research Codex SDK"
    assert w.agent_task == full


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


def test_detached_card_shows_bg_marker_in_header():
    w = SubAgentWidget("research", "Map it", "sonnet")
    w.detached = True
    w._paint_header()
    assert "bg" in str(w._header.render())


def test_finished_detached_card_shows_real_tool_tally():
    # Phase 2: a background agent streams its steps, so a finished card shows the
    # real tally rather than "ran in background".
    w = SubAgentWidget("research", "Map it", "sonnet")
    w.detached = True
    w.tool_count = 5
    w.finish("ok", status="done")
    line = str(w._activity.render())
    assert "5 toolcall" in line
    assert "ran in background" not in line


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


def test_waiting_card_shows_hourglass_after_tag_and_waiting_line():
    w = SubAgentWidget("merge", "Combine the reports", "sonnet")
    w.detached = True
    w.after_ids = ["job-3", "job-4"]
    w.set_waiting(True)
    header = str(w._header.render())
    assert "⧗" in header                      # static hourglass, not the spinner
    assert "after job-3, job-4" in header     # dim prerequisite tag
    assert "bg" in header                     # existing marker preserved
    assert "waiting on job-3, job-4" in str(w._activity.render())


def test_set_waiting_flip_restores_running_rendering():
    w = SubAgentWidget("merge", "Combine the reports", "sonnet")
    w.after_ids = ["job-3"]
    w.set_waiting(True)
    w.set_waiting(False)
    header = str(w._header.render())
    assert "⧗" not in header and "after" not in header
    assert "working…" in str(w._activity.render())


def test_blocked_card_names_the_culprit_in_header():
    w = SubAgentWidget("merge", "Combine the reports", "sonnet")
    w.after_ids = ["job-3"]
    w.blocked_by = "job-3"
    w.finish("PrerequisiteFailed: prerequisite job-3 failed — boom", status="failed")
    header = str(w._header.render())
    assert "blocked by job-3" in header
    assert "✕" in header


def test_finish_clears_stale_waiting_state():
    w = SubAgentWidget("merge", "Combine the reports", "sonnet")
    w.after_ids = ["job-3"]
    w.set_waiting(True)
    w.finish("report", status="done")
    assert w.waiting is False
    header = str(w._header.render())
    assert "⧗" not in header and "after" not in header
