import os
from pathlib import Path

import pytest

from marim_harness.interfaces.history import PromptHistory, default_history_path

_ROOT = os.geteuid() == 0 if hasattr(os, "geteuid") else False


def test_add_keeps_order_oldest_to_newest():
    h = PromptHistory()  # in-memory
    h.add("first")
    h.add("second")
    assert h.entries == ["first", "second"]


def test_blank_and_whitespace_are_ignored():
    h = PromptHistory()
    h.add("")
    h.add("   ")
    h.add("real")
    assert h.entries == ["real"]


def test_consecutive_duplicates_are_collapsed():
    h = PromptHistory()
    h.add("same")
    h.add("same")
    h.add("other")
    h.add("same")  # not consecutive -> kept
    assert h.entries == ["same", "other", "same"]


def test_persists_across_reload(tmp_path: Path):
    path = tmp_path / "prompt_history.jsonl"
    first = PromptHistory(path)
    first.add("alpha")
    first.add("beta")
    # A brand-new instance on the same file sees the saved entries.
    second = PromptHistory(path)
    assert second.entries == ["alpha", "beta"]


def test_multiline_prompt_round_trips(tmp_path: Path):
    path = tmp_path / "prompt_history.jsonl"
    multiline = "line one\nline two\n  indented"
    PromptHistory(path).add(multiline)
    assert PromptHistory(path).entries == [multiline]


def test_cap_keeps_only_the_last_n(tmp_path: Path):
    path = tmp_path / "prompt_history.jsonl"
    h = PromptHistory(path, max_entries=3)
    for i in range(5):
        h.add(f"p{i}")
    assert h.entries == ["p2", "p3", "p4"]
    # The cap also holds after a reload.
    assert PromptHistory(path, max_entries=3).entries == ["p2", "p3", "p4"]


def test_in_memory_mode_writes_no_file(tmp_path: Path):
    path = tmp_path / "prompt_history.jsonl"
    h = PromptHistory()  # no path -> nothing touches disk
    h.add("x")
    assert not path.exists()


def test_missing_file_loads_empty(tmp_path: Path):
    assert PromptHistory(tmp_path / "nope.jsonl").entries == []


def test_default_history_path_under_data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    p = default_history_path()
    assert p == tmp_path / "data" / "marim-harness" / "prompt_history.jsonl"


@pytest.mark.skipif(_ROOT, reason="root ignores permission bits")
def test_add_survives_an_unwritable_history_dir(tmp_path: Path):
    """A write failure must not raise: add() is called from the TUI's key handler
    before the prompt is routed, so an escaping OSError kills the app AND eats the
    message the user just typed."""
    target = tmp_path / "ro" / "prompt_history.jsonl"
    target.parent.mkdir()
    target.parent.chmod(0o500)
    try:
        h = PromptHistory(target)
        h.add("hello")  # must not raise
        assert h.entries == ["hello"]  # in-memory history still works
    finally:
        target.parent.chmod(0o700)


def test_load_survives_a_non_utf8_history_file(tmp_path: Path):
    """A corrupt file must not stop marim from launching — PromptHistory is
    constructed during CLI startup."""
    p = tmp_path / "prompt_history.jsonl"
    p.write_bytes(b'"ok"\n\xff\xfe not utf-8\n')
    assert PromptHistory(p).entries == []


@pytest.mark.skipif(_ROOT, reason="root ignores permission bits")
def test_load_survives_an_unreadable_history_file(tmp_path: Path):
    p = tmp_path / "prompt_history.jsonl"
    p.write_text('"ok"\n', encoding="utf-8")
    p.chmod(0o000)
    try:
        assert PromptHistory(p).entries == []
    finally:
        p.chmod(0o600)
