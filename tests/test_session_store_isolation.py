"""The test suite must never write session data into the developer's real
``~/.local/share/marim-harness/sessions``. A ``SessionManager`` built without an
explicit ``base_dir`` resolves its store root from ``$XDG_DATA_HOME`` at
construction time, so tests that forget ``base_dir=`` used to leak per-workspace
dirs into the real store. An autouse conftest guard isolates ``XDG_DATA_HOME``;
this test proves the guard is in force for the whole suite."""

from pathlib import Path

from marim_harness.session.store import SessionManager, _default_base_dir


def test_default_session_store_stays_out_of_real_home(tmp_path):
    """A default (no ``base_dir``) SessionManager must resolve its store root
    under the isolated XDG data home, never the real ~/.local/share."""
    manager = SessionManager(tmp_path)
    store = manager.create("leak-canary")

    real = Path.home() / ".local" / "share" / "marim-harness" / "sessions"
    assert real not in store.path.parents
    assert _default_base_dir() in store.path.parents
