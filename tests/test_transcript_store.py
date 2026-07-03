from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart

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


def _msgs_v2():
    return [ModelRequest(parts=[UserPromptPart(content="hi")])]


def _meta(sid: str, status: str = "running") -> dict:
    return {"stream_id": sid, "type": "general", "task": "t", "status": status,
            "model": None, "mcp": None, "depth": 1, "max_output_chars": None,
            "isolation": None}


def test_v2_envelope_round_trip(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs_v2(), 2000, meta=_meta("sg1"))
    assert ts.read("sg1") is not None            # messages come back
    meta = ts.read_meta("sg1")
    assert meta is not None and meta["status"] == "running"


def test_v1_bare_list_still_reads_and_has_no_meta(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs_v2(), 2000)               # no meta → v1 bare list on disk
    import json
    raw = json.loads((ts._dir / "t-sg1.json").read_text())
    assert isinstance(raw, list)                 # on-disk format unchanged for v1
    assert ts.read("sg1") is not None
    assert ts.read_meta("sg1") is None


def test_scan_meta_maps_stream_ids_and_skips_junk(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs_v2(), 2000, meta=_meta("sg1"))
    ts.write("sg2", _msgs_v2(), 2000, meta=_meta("sg2", "finished"))
    ts.write("sg3", _msgs_v2(), 2000)               # v1: no meta → not scanned
    (ts._dir / "t-corrupt.json").write_text("{not json")
    metas = ts.scan_meta()
    assert set(metas) == {"sg1", "sg2"}
    assert metas["sg1"]["status"] == "running"


def test_write_stamps_updated_timestamp(tmp_path):
    ts = TranscriptStore(tmp_path / "s.json", "sid")
    ts.write("sg1", _msgs_v2(), 2000, meta=_meta("sg1"))
    assert ts.read_meta("sg1")["updated"]        # non-empty ISO stamp
