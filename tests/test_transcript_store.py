from pathlib import Path

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from marim_harness.session import TranscriptStore


def _msgs():
    return [ModelResponse(parts=[
        TextPart(content="working"),
        ToolCallPart(tool_name="read_file", args={"path": "x"}, tool_call_id="c1"),
    ])]


def _store(tmp_path: Path) -> TranscriptStore:
    return TranscriptStore(tmp_path / "sessions" / "abc.json", "abc")


def test_write_then_read_roundtrips(tmp_path):
    s = _store(tmp_path)
    s.write("toolu_99", _msgs(), cap=2000)
    loaded = s.read("toolu_99")
    assert loaded is not None
    assert loaded[0].parts[0].content == "working"
    assert loaded[0].parts[1].tool_name == "read_file"


def test_read_missing_returns_none(tmp_path):
    assert _store(tmp_path).read("nope") is None


def test_write_sanitizes_id_into_filename(tmp_path):
    s = _store(tmp_path)
    s.write("call/abc.123:x", _msgs(), cap=2000)
    files = list((tmp_path / "sessions" / "abc.subagents").glob("*.json"))
    assert len(files) == 1
    assert all(c.isalnum() or c in "-_." for c in files[0].name)


def test_delete_all_removes_dir(tmp_path):
    s = _store(tmp_path)
    s.write("a", _msgs(), cap=2000)
    assert (tmp_path / "sessions" / "abc.subagents").exists()
    s.delete_all()
    assert not (tmp_path / "sessions" / "abc.subagents").exists()


def test_read_corrupt_returns_none(tmp_path):
    s = _store(tmp_path)
    d = tmp_path / "sessions" / "abc.subagents"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{not json")
    assert s.read("bad") is None
