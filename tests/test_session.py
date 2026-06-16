import json
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from marim_harness.session import SessionManager, SessionStore


def _history() -> list:
    return Agent(TestModel(), instructions="x").run_sync("hi").all_messages()


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "ws", base_dir=tmp_path / "data")


def _write_raw(mgr: SessionManager, session_id: str, *, name=None, updated="",
               messages=None, tokens=None) -> None:
    mgr.dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": session_id,
        "name": name or session_id,
        "updated": updated,
        "tokens": tokens or {"input": 0, "output": 0},
        "messages": messages or [],
    }
    (mgr.dir / f"{session_id}.json").write_text(json.dumps(payload))


def test_create_save_and_load_roundtrip(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("My Work")
    history = _history()
    store.save(history, RunUsage(input_tokens=12, output_tokens=8))

    # A fresh store for the same id loads the saved conversation.
    again = mgr.store(store.session_id)
    messages, usage, tasks = again.load()
    assert len(messages) == len(history)
    assert type(messages[0]).__name__ == type(history[0]).__name__
    assert usage.total_tokens == 20
    assert tasks == []


def test_load_missing_returns_empty(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create()
    messages, usage, tasks = store.load()
    assert messages == []
    assert usage.total_tokens == 0
    assert tasks == []


def test_tasks_round_trip(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("With Tasks")
    tasks = [
        {"text": "first", "status": "done"},
        {"text": "second", "status": "in_progress"},
    ]
    store.save(_history(), RunUsage(), tasks)
    _, _, loaded = mgr.store(store.session_id).load()
    assert loaded == tasks


def test_legacy_file_without_tasks_loads_empty(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Legacy")
    # Save then strip the tasks key to mimic a pre-task-tracking file.
    store.save(_history(), RunUsage())
    data = json.loads(store.path.read_text())
    del data["tasks"]
    store.path.write_text(json.dumps(data))
    _, _, loaded = mgr.store(store.session_id).load()
    assert loaded == []


def test_dir_is_workspace_specific(tmp_path: Path):
    base = tmp_path / "data"
    a = SessionManager(tmp_path / "ws-a", base_dir=base)
    b = SessionManager(tmp_path / "ws-b", base_dir=base)
    a_again = SessionManager(tmp_path / "ws-a", base_dir=base)
    assert a.dir != b.dir
    assert a.dir == a_again.dir  # stable per workspace


def test_create_slugifies_name(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("Fix the Parser!")
    assert store.session_id == "fix-the-parser"
    assert store.name == "Fix the Parser!"


def test_create_makes_unique_ids(tmp_path: Path):
    mgr = _manager(tmp_path)
    a = mgr.create("dup")
    b = mgr.create("dup")  # same name, even before either is saved
    assert a.session_id != b.session_id


def test_list_sorted_by_recency(tmp_path: Path):
    mgr = _manager(tmp_path)
    _write_raw(mgr, "old", updated="2026-01-01T00:00:00+00:00")
    _write_raw(mgr, "new", updated="2026-06-01T00:00:00+00:00")
    _write_raw(mgr, "mid", updated="2026-03-01T00:00:00+00:00")
    ids = [info.id for info in mgr.list()]
    assert ids == ["new", "mid", "old"]


def test_list_reports_counts_and_tokens(tmp_path: Path):
    mgr = _manager(tmp_path)
    _write_raw(
        mgr, "s1", name="Session One", updated="2026-01-01T00:00:00+00:00",
        messages=[{}, {}, {}], tokens={"input": 30, "output": 70},
    )
    info = mgr.list()[0]
    assert info.name == "Session One"
    assert info.message_count == 3
    assert info.tokens == 100


def test_list_empty_when_no_sessions(tmp_path: Path):
    assert _manager(tmp_path).list() == []


def test_latest_returns_most_recent(tmp_path: Path):
    mgr = _manager(tmp_path)
    _write_raw(mgr, "old", updated="2026-01-01T00:00:00+00:00")
    _write_raw(mgr, "new", updated="2026-06-01T00:00:00+00:00")
    assert mgr.latest().id == "new"


def test_latest_none_when_empty(tmp_path: Path):
    assert _manager(tmp_path).latest() is None


def test_store_recovers_name_from_file(tmp_path: Path):
    mgr = _manager(tmp_path)
    created = mgr.create("Recover Me")
    created.save(_history(), RunUsage())
    reopened = mgr.store(created.session_id)  # no name passed
    assert reopened.name == "Recover Me"


def test_delete_removes_session(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create("doomed")
    store.save(_history(), RunUsage())
    assert store.path.exists()
    mgr.delete(store.session_id)
    assert not store.path.exists()
    assert mgr.list() == []


def test_clear_removes_file(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create()
    store.save(_history(), RunUsage())
    assert store.path.exists()
    store.clear()
    assert not store.path.exists()
    store.clear()  # idempotent


def test_store_is_a_session_store(tmp_path: Path):
    mgr = _manager(tmp_path)
    assert isinstance(mgr.create(), SessionStore)


def test_unnamed_session_is_auto_named(tmp_path: Path):
    assert _manager(tmp_path).create().auto_named is True


def test_named_session_is_not_auto_named(tmp_path: Path):
    assert _manager(tmp_path).create("My Work").auto_named is False


def test_auto_flag_persists_and_recovers(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create()  # auto_named True
    store.save(_history(), RunUsage())
    assert mgr.store(store.session_id).auto_named is True

    # Once titled, the flag is off and stays off across reopens.
    store.name = "Picked a title"
    store.auto_named = False
    store.save(_history(), RunUsage())
    assert mgr.store(store.session_id).auto_named is False
    assert mgr.store(store.session_id).name == "Picked a title"


def test_model_persists_and_recovers(tmp_path: Path):
    mgr = _manager(tmp_path)
    store = mgr.create()
    assert store.model is None  # defaults to the env model
    store.model = "openai/gpt-5.2"
    store.save(_history(), RunUsage())
    assert mgr.store(store.session_id).model == "openai/gpt-5.2"
