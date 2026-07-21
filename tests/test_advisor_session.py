"""Session persistence of the advisor model, mirroring the ``model`` field:
save/save_meta/store() round-trip, create()-inherits-latest, and the "off"
sentinel treated as an ordinary persisted string."""

from pydantic_ai.usage import RunUsage

from marim_harness.advisor import ADVISOR_OFF
from marim_harness.session import SessionManager


def test_advisor_model_round_trips_through_save(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.advisor_model = "openrouter:anthropic/claude-opus-4.8"
    store.save([], RunUsage())
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.advisor_model == "openrouter:anthropic/claude-opus-4.8"


def test_save_meta_patches_advisor_without_touching_messages(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.save([], RunUsage())
    store.advisor_model = ADVISOR_OFF
    store.save_meta()
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.advisor_model == ADVISOR_OFF


def test_create_inherits_latest_advisor_model(tmp_path):
    manager = SessionManager(tmp_path)
    first = manager.create()
    first.advisor_model = "openrouter:opus"
    first.save([], RunUsage())
    fresh = manager.create()
    assert fresh.advisor_model == "openrouter:opus"


def test_create_without_history_has_no_advisor(tmp_path):
    manager = SessionManager(tmp_path)
    assert manager.create().advisor_model is None


def test_old_session_files_load_with_none(tmp_path):
    # A pre-advisor session file simply has no key.
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.save([], RunUsage())
    import json
    data = json.loads(store.path.read_text())
    data.pop("advisor_model", None)
    store.path.write_text(json.dumps(data))
    reopened = SessionManager(tmp_path).store(store.session_id)
    assert reopened.advisor_model is None
