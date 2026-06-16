from marim_harness.tasks import Task, TaskList, render_tasks, summarize


def test_replace_sets_items_and_fires_on_change():
    fired = []
    tl = TaskList(on_change=lambda: fired.append(True))
    tl.replace([Task("a", "done"), Task("b", "in_progress")])
    assert [t.text for t in tl.items] == ["a", "b"]
    assert [t.status for t in tl.items] == ["done", "in_progress"]
    assert fired == [True]


def test_replace_accepts_dicts_and_drops_blank_text():
    tl = TaskList()
    tl.replace([{"text": "keep", "status": "pending"}, {"text": "  ", "status": "done"}])
    assert [t.text for t in tl.items] == ["keep"]


def test_replace_clamps_unknown_status_to_pending():
    tl = TaskList()
    tl.replace([Task("x", "bogus")])
    assert tl.items[0].status == "pending"


def test_replace_strips_text():
    tl = TaskList()
    tl.replace([{"text": "  spaced  "}])
    assert tl.items[0].text == "spaced"


def test_status_defaults_to_pending():
    assert Task("x").status == "pending"
    tl = TaskList()
    tl.replace([{"text": "no status given"}])
    assert tl.items[0].status == "pending"


def test_clear_empties_without_firing():
    fired = []
    tl = TaskList(on_change=lambda: fired.append(True))
    tl.replace([Task("a")])
    fired.clear()
    tl.clear()
    assert tl.items == []
    assert fired == []  # lifecycle resets are handled by the caller, not on_change


def test_load_restores_without_firing():
    fired = []
    tl = TaskList(on_change=lambda: fired.append(True))
    tl.load([{"text": "restored", "status": "in_progress"}])
    assert [t.text for t in tl.items] == ["restored"]
    assert tl.items[0].status == "in_progress"
    assert fired == []


def test_to_payload_round_trips():
    tl = TaskList()
    tl.replace([Task("a", "done"), Task("b", "pending")])
    payload = tl.to_payload()
    assert payload == [
        {"text": "a", "status": "done"},
        {"text": "b", "status": "pending"},
    ]
    other = TaskList()
    other.load(payload)
    assert other.to_payload() == payload


def test_load_tolerates_garbage():
    tl = TaskList()
    tl.load([{"text": "", "status": "x"}, {"text": "ok", "status": "weird"}])
    assert [t.text for t in tl.items] == ["ok"]
    assert tl.items[0].status == "pending"


def test_render_uses_status_symbols():
    out = render_tasks([Task("done one", "done"), Task("now", "in_progress"),
                        Task("later", "pending")])
    assert out == "✔ done one\n▸ now\n○ later"


def test_render_empty_is_empty_string():
    assert render_tasks([]) == ""


def test_summarize_counts_by_status():
    s = summarize([Task("a", "done"), Task("b", "in_progress"),
                   Task("c", "pending"), Task("d", "pending")])
    assert s == "4 tasks: 1 done, 1 in progress, 2 pending"


def test_summarize_empty():
    assert summarize([]) == "no tasks"
