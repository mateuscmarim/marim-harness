import io
import json

import pytest
from pydantic_ai.usage import RunUsage

from marim_harness.cli import sessions
from marim_harness.session import SessionManager


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace dir whose session storage is redirected into tmp_path so no
    test ever touches the real home dir."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _make_session(ws, name, *, input_tokens=0, output_tokens=0):
    m = SessionManager(ws.resolve())
    store = m.create(name)
    store.save([], RunUsage(input_tokens=input_tokens, output_tokens=output_tokens))
    return store


def test_no_subcommand_returns_2_and_prints_usage():
    err = io.StringIO()
    code = sessions.main([], out=io.StringIO(), err=err)
    assert code == 2
    assert "usage" in err.getvalue().lower()


def test_list_empty_text(workspace):
    out = io.StringIO()
    code = sessions.main(["list", str(workspace)], out=out, err=io.StringIO())
    assert code == 0
    assert "no sessions" in out.getvalue().lower()


def test_list_empty_json(workspace):
    out = io.StringIO()
    code = sessions.main(["list", str(workspace), "--json"], out=out, err=io.StringIO())
    assert code == 0
    assert json.loads(out.getvalue()) == []


def test_list_text_has_header_and_rows(workspace):
    _make_session(workspace, "alpha", input_tokens=10, output_tokens=5)
    out = io.StringIO()
    code = sessions.main(["list", str(workspace)], out=out, err=io.StringIO())
    assert code == 0
    text = out.getvalue()
    assert "ID" in text and "NAME" in text and "UPDATED" in text
    assert "MESSAGES" in text and "TOKENS" in text
    assert "alpha" in text
    assert "15" in text  # 10 + 5 tokens


def test_list_json_objects(workspace):
    _make_session(workspace, "alpha", input_tokens=10, output_tokens=5)
    _make_session(workspace, "beta")
    out = io.StringIO()
    code = sessions.main(["list", str(workspace), "--json"], out=out, err=io.StringIO())
    assert code == 0
    data = json.loads(out.getvalue())
    assert isinstance(data, list) and len(data) == 2
    keys = {"id", "name", "updated", "message_count", "tokens"}
    for obj in data:
        assert set(obj.keys()) == keys
    names = {obj["name"] for obj in data}
    assert names == {"alpha", "beta"}
    alpha = next(o for o in data if o["name"] == "alpha")
    assert alpha["tokens"] == 15


def test_list_defaults_to_cwd(workspace, monkeypatch):
    _make_session(workspace, "alpha")
    monkeypatch.chdir(workspace)
    out = io.StringIO()
    code = sessions.main(["list", "--json"], out=out, err=io.StringIO())
    assert code == 0
    data = json.loads(out.getvalue())
    assert {o["name"] for o in data} == {"alpha"}


def test_delete_existing(workspace):
    store = _make_session(workspace, "alpha")
    out, err = io.StringIO(), io.StringIO()
    code = sessions.main(["delete", store.session_id, str(workspace)], out=out, err=err)
    assert code == 0
    assert store.session_id in out.getvalue()
    # gone now
    m = SessionManager(workspace.resolve())
    assert m.list() == []


def test_delete_missing_returns_1(workspace):
    out, err = io.StringIO(), io.StringIO()
    code = sessions.main(["delete", "does-not-exist", str(workspace)], out=out, err=err)
    assert code == 1
    assert "does-not-exist" in err.getvalue()
    assert err.getvalue().strip() != ""


def test_delete_defaults_to_cwd(workspace, monkeypatch):
    store = _make_session(workspace, "alpha")
    monkeypatch.chdir(workspace)
    out, err = io.StringIO(), io.StringIO()
    code = sessions.main(["delete", store.session_id], out=out, err=err)
    assert code == 0
    m = SessionManager(workspace.resolve())
    assert m.list() == []
