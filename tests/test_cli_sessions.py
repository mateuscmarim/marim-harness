import io
from pathlib import Path

from marim_harness.interfaces.cli.sessions import main
from marim_harness.session import SessionManager
from marim_harness.session.claim import try_acquire


def test_delete_refuses_claimed_session_and_names_holder(tmp_path: Path):
    manager = SessionManager(tmp_path)
    store = manager.create()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{}")  # create() does not persist; list() needs the file
    outsider = try_acquire(store.path, kind="daemon", endpoint="http://127.0.0.1:8643")
    assert outsider is not None
    out, err = io.StringIO(), io.StringIO()
    try:
        rc = main(["delete", store.session_id, str(tmp_path)], out=out, err=err)
    finally:
        outsider.release()
    assert rc == 2
    assert store.path.exists()
    assert "owned by daemon" in err.getvalue()
    assert "http://127.0.0.1:8643" in err.getvalue()
