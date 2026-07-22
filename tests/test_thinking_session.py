"""Session persistence of the thinking level, mirroring the ``model`` field:
save/save_meta/store() round-trip, create()-inherits-latest, and old files
(no key) loading as None."""

from pydantic_ai.usage import RunUsage

from marim_harness.session import SessionManager


def test_thinking_round_trips_through_save(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.thinking = "high"
    store.save([], RunUsage())
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.thinking == "high"


def test_save_meta_patches_thinking_without_touching_messages(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.save([], RunUsage())
    store.thinking = "off"
    store.save_meta()
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.thinking == "off"


def test_create_inherits_latest_thinking(tmp_path):
    manager = SessionManager(tmp_path)
    first = manager.create()
    first.thinking = "medium"
    first.save([], RunUsage())
    fresh = manager.create()
    assert fresh.thinking == "medium"


def test_create_without_history_has_no_thinking(tmp_path):
    manager = SessionManager(tmp_path)
    assert manager.create().thinking is None


def test_old_session_files_load_with_none(tmp_path):
    # A pre-thinking session file simply has no key → None (behaves as before).
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.save([], RunUsage())
    import json
    data = json.loads(store.path.read_text())
    data.pop("thinking", None)
    store.path.write_text(json.dumps(data))
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.thinking is None
